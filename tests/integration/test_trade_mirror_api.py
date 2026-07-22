"""The TAKE IT path end-to-end: POST /api/trades/{id}/action with the first
ADD is the button-side Allow, and it must mirror the entry into the linked
broker book exactly like the spoken confirm — server-side hop, degrade
honest, response carrying the recorded outcome for the UI to render.
"""

import pytest
from fastapi.testclient import TestClient

from common import broker, signals, trades
from common.db import get_connection


@pytest.fixture(scope="module")
def client():
    from web.server import app

    return TestClient(app)


def _wipe_trades():
    conn = get_connection()
    conn.execute("DELETE FROM trades")
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def clean_trades():
    _wipe_trades()
    yield
    _wipe_trades()


@pytest.fixture()
def quoted(monkeypatch):
    monkeypatch.setattr(trades.bus, "publish", lambda ev: None)
    rec = signals.recommend_trade("SPY")
    assert "error" not in rec
    trade = trades.log_quote("sess-api", rec, source="marcus")
    assert trade is not None
    return trade


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, link, order):
        self.link, self.order, self.calls = link, order, []

    def __call__(self, *_a, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        return self.link

    def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        return self.order


def test_take_it_places_the_order_and_the_response_says_so(client, quoted, monkeypatch):
    fake = _FakeHttp(_Resp(200, {"customer": 11}),
                     _Resp(200, {"result": "filled", "id": "ord-api-1"}))
    monkeypatch.setattr(broker.httpx, "Client", fake)

    r = client.post(f"/api/trades/{quoted['id']}/action",
                    json={"action": "add", "qty": 1})
    assert r.status_code == 200
    t = r.json()
    assert t["status"] == "open"
    assert t["broker_order_id"] == "ord-api-1"
    assert t["broker_customer"] == 11
    # the row's own session authored the quote; the button sends none
    assert fake.calls[0][2] == {"session": "sess-api"}
    assert fake.calls[1][2]["clientOrderId"] == f"desk-{quoted['id']}"

    # /api/trades now serves the executed state to every viewer
    rows = client.get("/api/trades").json()["positions"]
    assert rows and rows[0]["broker_order_id"] == "ord-api-1"


def test_take_it_with_no_broker_is_still_a_take(client, quoted):
    """conftest points BROKER_URL at a port that refuses instantly — the
    real degrade path, no fake installed. The desk books the trade, records
    the miss, and answers 200."""
    r = client.post(f"/api/trades/{quoted['id']}/action",
                    json={"action": "add", "qty": 1})
    assert r.status_code == 200
    t = r.json()
    assert t["status"] == "open"
    assert t["broker_order_id"] is None
    assert t["broker_status"] == "unreachable"
    assert t["broker_reason"]                     # the why-not is on the record
