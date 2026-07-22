"""The execution mirror: a confirmed desk trade becomes a real broker order
for the linked customer — and NOTHING about the desk's own log depends on it.

The tests that matter most are, as with the watchlist write-through, the
failure ones: unlinked, unreachable and refused must all leave the trade
open on the desk with the reason recorded, never an exception, never a lost
entry. No network is touched: httpx.Client is replaced with a fake.
"""

import pytest

from common import broker, signals, trades
from common.db import get_connection


def _wipe_trades():
    conn = get_connection()
    conn.execute("DELETE FROM trades")
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def clean_trades():
    """Before AND after: the unmirrorable-row test plants a deliberately
    unusable expiry, and an open row like that must not leak into other
    modules' positions_snapshot calls."""
    _wipe_trades()
    yield
    _wipe_trades()


@pytest.fixture()
def captured_events(monkeypatch):
    events = []
    monkeypatch.setattr(trades.bus, "publish", events.append)
    return events


@pytest.fixture()
def quoted(captured_events):
    rec = signals.recommend_trade("SPY")
    assert "error" not in rec
    trade = trades.log_quote("sess-mirror", rec, source="marcus")
    assert trade is not None
    return trade


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeHttp:
    """Stands in for httpx.Client. Records calls, replays scripted answers."""

    def __init__(self, link=None, order=None, raise_on=None):
        self.link = link if link is not None else _Resp(200, {"customer": 7})
        self.order = order if order is not None else _Resp(
            200, {"result": "filled", "id": "ord-uuid-1", "side": "buy"})
        self.raise_on = raise_on          # "get" | "post" | None
        self.calls = []

    def __call__(self, *_a, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        if self.raise_on == "get":
            raise ConnectionError("connection refused")
        return self.link

    def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        if self.raise_on == "post":
            raise TimeoutError("broker timed out")
        return self.order


@pytest.fixture()
def fake_broker(monkeypatch):
    def install(**kwargs):
        fake = _FakeHttp(**kwargs)
        monkeypatch.setattr(broker.httpx, "Client", fake)
        return fake

    return install


# ------------------------------------------------------------ the symbol ----

def test_occ_symbol_encoding():
    assert broker.occ_symbol("XSP", "2026-07-24", "call", 622.0) == "XSP260724C00622000"
    assert broker.occ_symbol("XSP", "2026-07-24", "put", 622.5) == "XSP260724P00622500"
    assert broker.occ_symbol("aapl", "2026-08-21", "call", 210) == "AAPL260821C00210000"


def test_occ_symbol_refuses_rather_than_guesses():
    with pytest.raises(ValueError):
        broker.occ_symbol("XSP", "whenever", "call", 622.0)     # unusable expiry
    with pytest.raises(ValueError):
        broker.occ_symbol("NOT-A-ROOT!", "2026-07-24", "call", 622.0)
    with pytest.raises(ValueError):
        broker.occ_symbol("XSP", "2026-07-24", "call", 0)       # unpriced is unpriced


# ------------------------------------------------------------- happy path ----

def test_linked_entry_places_the_real_order(quoted, fake_broker, captured_events):
    """Linked session + broker up: the confirm places ONE order with the
    trade's own contract, its whole-contract qty, and its id as the
    idempotency key. The row records the broker's answer."""
    fake = fake_broker()
    t = trades.confirm_entry("sess-mirror", fill_price=3.00, contracts=4)

    assert t["status"] == "open"                       # desk log first, always
    assert t["broker_order_id"] == "ord-uuid-1"
    assert t["broker_status"] == "filled"
    assert t["broker_customer"] == 7
    assert t["broker_reason"] is None

    get_call, post_call = fake.calls
    assert get_call[1].endswith("/api/link")
    assert get_call[2] == {"session": "sess-mirror"}
    body = post_call[2]
    assert post_call[1].endswith("/api/orders")
    assert body["clientOrderId"] == f"desk-{t['id']}"
    assert body["customer"] == 7
    assert body["side"] == "buy"
    assert body["qty"] == "4"                          # whole contracts, as a string
    # the OCC contract is built from the row's own XSP plan
    assert body["symbol"] == t["broker_contract"]
    assert body["symbol"] == broker.occ_symbol(
        quoted["contract_ticker"], quoted["expiry"], quoted["kind"], quoted["strike"])
    # the event the UI hears carries the executed state
    assert captured_events[-1]["event"] == "opened"
    assert captured_events[-1]["trade"]["broker_order_id"] == "ord-uuid-1"


def test_take_it_button_path_mirrors_too(quoted, fake_broker):
    """The UI's first ADD is the same Allow as the spoken 'I'm in' — the
    mirror runs from the button path as well, using the row's own session."""
    fake = fake_broker()
    t = trades.adjust(quoted["id"], "add", qty=2, price=2.00)

    assert t["status"] == "open"
    assert t["broker_order_id"] == "ord-uuid-1"
    assert fake.calls[0][2] == {"session": "sess-mirror"}
    assert fake.calls[1][2]["qty"] == "2"

    # a LATER add on the open trade is position management, not the entry —
    # it must not fire a second broker order
    n = len(fake.calls)
    trades.adjust(quoted["id"], "add", qty=1, price=2.50)
    assert len(fake.calls) == n


# ------------------------------------------------------- degrade honestly ----

def test_unlinked_session_stays_local_with_the_reason(quoted, fake_broker):
    fake = fake_broker(link=_Resp(200, {"customer": None}))
    t = trades.confirm_entry("sess-mirror", fill_price=3.00, contracts=1)

    assert t["status"] == "open"                       # never blocked
    assert t["broker_order_id"] is None
    assert t["broker_status"] == "unlinked"
    assert "not linked" in t["broker_reason"]
    assert [c[0] for c in fake.calls] == ["GET"]       # no order was attempted


def test_broker_down_costs_the_mirror_never_the_trade(quoted, fake_broker):
    fake_broker(raise_on="get")
    t = trades.confirm_entry("sess-mirror", fill_price=3.00, contracts=1)

    assert t["status"] == "open"
    assert t["broker_order_id"] is None
    assert t["broker_status"] == "unreachable"
    assert t["broker_reason"] == "ConnectionError"


def test_timeout_on_the_order_is_unreachable_not_a_denial(quoted, fake_broker):
    """A timed-out POST may still have landed on the far side. The record
    says the CALL failed; it does not claim the account is empty."""
    fake_broker(raise_on="post")
    t = trades.confirm_entry("sess-mirror", fill_price=3.00, contracts=1)

    assert t["status"] == "open"
    assert t["broker_status"] == "unreachable"
    assert t["broker_reason"] == "TimeoutError"
    assert t["broker_customer"] == 7                   # the link DID resolve


def test_refused_order_is_a_recorded_refusal(quoted, fake_broker):
    fake_broker(order=_Resp(200, {"result": "rejected", "id": "ord-x",
                                  "error": "insufficient funds"}))
    t = trades.confirm_entry("sess-mirror", fill_price=3.00, contracts=1)

    assert t["status"] == "open"                       # the desk trade stands
    assert t["broker_order_id"] is None                # nothing executed
    assert t["broker_status"] == "rejected"
    assert t["broker_reason"] == "insufficient funds"


def test_listing_refusal_409_carries_the_brokers_reason(quoted, fake_broker):
    fake_broker(order=_Resp(409, {"result": "rejected",
                                  "error": "SPY is not an allowlisted underlying"}))
    t = trades.confirm_entry("sess-mirror", fill_price=3.00, contracts=1)
    assert t["broker_status"] == "rejected"
    assert "allowlisted" in t["broker_reason"]


# ----------------------------------------------------------- idempotency ----

def test_retry_reuses_the_trades_own_id_and_cannot_double_place(quoted, fake_broker):
    """The clientOrderId is derived from the trade id, so the broker's
    same-request-same-answer gate makes a desk retry a no-op; and once the
    row carries an order id the desk does not even call out again."""
    fake = fake_broker()
    t = trades.confirm_entry("sess-mirror", fill_price=3.00, contracts=1)
    first_key = fake.calls[1][2]["clientOrderId"]
    assert first_key == f"desk-{t['id']}"

    # the desk-side guard: an already-mirrored row is left alone
    assert broker.mirror_entry(t) is None
    assert len(fake.calls) == 2

    # the wire-level gate: a lost-response retry sends the SAME key and
    # records the broker's same answer — one order, either way
    unrecorded = {**t, "broker_order_id": None, "broker_status": None,
                  "broker_reason": None}
    out = broker.mirror_entry(unrecorded)
    assert fake.calls[3][2]["clientOrderId"] == first_key
    assert out["broker_order_id"] == "ord-uuid-1"


# ------------------------------------------------------------ say-so path ----

def test_underlying_only_record_uses_the_recommendation_and_says_so(
        captured_events, fake_broker):
    """A row with no execution plan (contract_ticker == underlying) still
    mirrors — as the recommendation's own underlying-terms contract — and the
    record SAYS the contract came from the recommendation."""
    fake = fake_broker()
    now = trades._now()
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO trades (created_at, updated_at, session, source, underlying,"
        " contract_ticker, kind, strike, strike_underlying, expiry,"
        " contracts_total, contracts_open, status, quoted_px)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, now, "sess-mirror", "marcus", "SPY", "SPY", "call",
         620.0, 620.0, "2026-07-24", 1, 1, "quoted", 2.50))
    conn.commit()
    conn.close()

    t = trades.confirm_entry("sess-mirror", fill_price=2.50)
    assert t["id"] == cur.lastrowid
    assert fake.calls[1][2]["symbol"] == "SPY260724C00620000"
    assert "recommendation" in (t["note"] or "")
    assert t["broker_order_id"] == "ord-uuid-1"


def test_unmirrorable_row_is_refused_with_the_reason(captured_events, fake_broker):
    """A row the desk cannot express as one whole OCC contract is not
    guessed at — nothing is sent and the reason is on the record."""
    fake = fake_broker()
    now = trades._now()
    conn = get_connection()
    conn.execute(
        "INSERT INTO trades (created_at, updated_at, session, source, underlying,"
        " contract_ticker, kind, strike, strike_underlying, expiry,"
        " contracts_total, contracts_open, status, quoted_px)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, now, "sess-mirror", "marcus", "SPY", "XSP", "call",
         620.0, 622.0, "some-day", 1, 1, "quoted", 2.50))
    conn.commit()
    conn.close()

    t = trades.confirm_entry("sess-mirror", fill_price=2.50)
    assert t["status"] == "open"
    assert t["broker_status"] == "unmirrorable"
    assert "expiry" in t["broker_reason"]
    assert [c[0] for c in fake.calls] == ["GET"]       # link only, no order
