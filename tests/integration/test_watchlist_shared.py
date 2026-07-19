"""The SHARED watchlist: one list, two apps, and the migration off localStorage.

Membership moved to the broker because a watchlist is user state and two apps
cannot share user state through one app's localStorage. These tests cover the
two things that decision costs:

  * the desk now DEPENDS on a service it used to work without. Every failure
    of that dependency has to come back as available=False and a 200, and the
    caller's instruction on reading it is to change nothing. That contract is
    the reason the rail keeps working with the whole minibank estate deleted.

  * a browser arriving with a hundred and twenty tickers has to hand them over
    exactly once. Twice is not a duplicate (the broker's import is additive
    and ON CONFLICT DO NOTHING), it is a RESURRECTION: symbols another browser
    deliberately removed coming back on every load, forever.

No network is touched. httpx.AsyncClient is replaced with a fake that routes
by URL, because this flow makes two different GETs and a POST and a fake that
answers everything the same way would let a wrong call order pass.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from web.server import app

    return TestClient(app)


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeBroker:
    """Routes on the path, so the ORDER and TARGET of calls are assertable."""

    def __init__(self, customer=10, watchlist=None, after_import=None,
                 link_status=200, watch_status=200, import_status=200, raise_on=None):
        self.customer = customer
        self.watchlist = watchlist if watchlist is not None else []
        # what the read-back returns once the import has run · defaults to
        # "whatever was imported", which is what the real broker does
        self.after_import = after_import
        self.link_status = link_status
        self.watch_status = watch_status
        self.import_status = import_status
        # "link" | "watchlist" | "import" | "bind" · which call blows up
        self.raise_on = raise_on
        self.calls = []
        self.imported = False

    def __call__(self, *_a, **_kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, params=None):
        self.calls.append(("GET", _path(url), params))
        if url.endswith("/api/link"):
            if self.raise_on == "link":
                raise ConnectionError("connection refused")
            return _Resp(self.link_status, {"customer": self.customer})
        if url.endswith("/api/watchlist"):
            if self.raise_on == "watchlist":
                raise ConnectionError("connection refused")
            rows = self.after_import if (self.imported and self.after_import is not None) \
                else self.watchlist
            return _Resp(self.watch_status, rows)
        raise AssertionError("unexpected GET " + url)

    async def post(self, url, json=None):
        self.calls.append(("POST", _path(url), json))
        if url.endswith("/api/link"):
            if self.raise_on == "bind":
                raise ConnectionError("connection refused")
            return _Resp(self.link_status, {"result": "ok", "customer": json.get("customer")})
        if url.endswith("/api/watchlist"):
            if self.raise_on == "import":
                raise TimeoutError("broker timed out")
            if json.get("action") == "import":
                self.imported = True
                if self.after_import is None:
                    self.after_import = [{"symbol": s["symbol"], "price": None,
                                          "tradable": False, "name": None, "exchange": None}
                                         for s in json["symbols"]]
            return _Resp(self.import_status, {"result": "ok"})
        raise AssertionError("unexpected POST " + url)


def _path(url):
    return "/" + url.split("/", 3)[3] if url.count("/") >= 3 else url


@pytest.fixture()
def fake_broker(monkeypatch):
    import web.server as server

    def install(**kwargs):
        fake = _FakeBroker(**kwargs)
        monkeypatch.setattr(server.httpx, "AsyncClient", fake)
        return fake

    return install


def _boot(client, local=None, migrated=False, session="sess-abc"):
    return client.post("/api/watchlist/bootstrap",
                       json={"session": session, "local": local or [], "migrated": migrated})


# ------------------------------------------------------------- degradation

def test_unreachable_broker_leaves_the_desk_working(client, fake_broker):
    """THE ONE THAT MATTERS. The rail is a local-first feature that worked
    before the broker existed. Connection refused must be a 200 saying so,
    never a 502 and never an exception out of the handler — the page reads
    available=False and changes nothing at all."""
    fake_broker(raise_on="link")
    r = _boot(client, local=["SPY", "QQQ"])

    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["symbols"] == [] and body["imported"] == 0
    assert body["reason"] == "ConnectionError"


def test_broker_error_on_the_read_is_not_an_exception(client, fake_broker):
    fake_broker(watch_status=500)
    body = _boot(client, local=["SPY"]).json()
    assert body["available"] is False and body["reason"] == "watchlist 500"


def test_a_failed_import_is_not_reported_as_a_successful_one(client, fake_broker):
    """The dangerous failure: answering available=True after the write was
    lost would let the browser set its migrated flag and never offer the list
    again. A hundred and twenty tickers would be gone."""
    fake_broker(import_status=500)
    body = _boot(client, local=["SPY", "QQQ"]).json()

    assert body["available"] is False, "the browser must NOT mark itself migrated"
    assert body["imported"] == 0
    assert body["reason"] == "import 500"


def test_import_that_times_out_mid_flight(client, fake_broker):
    fake_broker(raise_on="import")
    body = _boot(client, local=["SPY"]).json()
    assert body["available"] is False and body["reason"] == "TimeoutError"


# ---------------------------------------------------------------- identity

def test_unlinked_session_reads_nothing_and_writes_nothing(client, fake_broker):
    """Reachable, but this browser has claimed no book. We do not guess a
    customer id: the ids are small integers, so the guess that works is +1,
    and it would both leak and overwrite a stranger's list."""
    fake = fake_broker(customer=None)
    body = _boot(client, local=["SPY"]).json()

    assert body == {"available": True, "linked": False, "symbols": [], "imported": 0}
    assert [c[0] for c in fake.calls] == ["GET"], "asked who, then stopped"


def test_no_session_never_calls_out(client, fake_broker):
    """Most desk traffic will never hold an account and must not generate a
    broker round trip per page load."""
    fake = fake_broker()
    assert _boot(client, local=["SPY"], session="").json()["available"] is False
    assert fake.calls == []


def test_link_binds_this_browser_to_a_customer(client, fake_broker):
    fake = fake_broker()
    r = client.post("/api/watchlist/link", json={"session": "sess-abc", "customer": 11})

    assert r.json() == {"linked": True, "customer": 11}
    assert fake.calls == [("POST", "/api/link", {"session": "sess-abc", "customer": 11})]


def test_link_degrades_like_everything_else(client, fake_broker):
    """Binding is the only new WRITE this feature adds, so it gets the same
    contract as the rest: a 200 that says it did not happen."""
    fake_broker(raise_on="bind")
    r = client.post("/api/watchlist/link", json={"session": "s", "customer": 11})

    assert r.status_code == 200
    assert r.json() == {"linked": False, "reason": "ConnectionError"}

    fake_broker(link_status=500)
    assert client.post("/api/watchlist/link",
                       json={"session": "s", "customer": 11}).json() == \
        {"linked": False, "reason": "link 500"}


def test_link_refuses_a_half_specified_bind(client, fake_broker):
    fake = fake_broker()
    assert client.post("/api/watchlist/link",
                       json={"session": "", "customer": 11}).json()["linked"] is False
    assert client.post("/api/watchlist/link",
                       json={"session": "s"}).json()["linked"] is False
    assert fake.calls == []


# --------------------------------------------------------------- migration

def test_a_browsers_local_list_is_handed_over_once(client, fake_broker):
    """First load ever: the shared list is empty, this browser has a rail it
    has been curating for months, and none of it may be dropped on the floor."""
    fake = fake_broker(watchlist=[])
    body = _boot(client, local=["SPY", "QQQ", "NVDA"]).json()

    posts = [c for c in fake.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2] == {"customer": 10, "action": "import",
                           "symbols": [{"symbol": "SPY"}, {"symbol": "QQQ"}, {"symbol": "NVDA"}]}
    assert body["imported"] == 3
    assert [s["symbol"] for s in body["symbols"]] == ["SPY", "QQQ", "NVDA"]

    # read back, not echoed · what landed is the broker's answer, including
    # flags we never sent
    assert [c[1] for c in fake.calls] == \
        ["/api/link", "/api/watchlist", "/api/watchlist", "/api/watchlist"]


def test_the_migration_does_not_run_a_second_time(client, fake_broker):
    """IDEMPOTENCE, by the server's own guard rather than the browser's word.
    A shared list that already has rows is not an empty one, so there is
    nothing to migrate into and the import is never attempted."""
    fake = fake_broker(watchlist=[{"symbol": "SPY", "price": None, "tradable": False}])
    body = _boot(client, local=["SPY", "QQQ", "NVDA"], migrated=False).json()

    assert [c[0] for c in fake.calls] == ["GET", "GET"], "no import was attempted"
    assert body["imported"] == 0
    assert [s["symbol"] for s in body["symbols"]] == ["SPY"], \
        "the shared list wins · QQQ and NVDA were removed somewhere else"


def test_a_migrated_browser_never_resurrects_its_cache(client, fake_broker):
    """The other half of idempotence, and the one the server cannot see. The
    shared list IS empty — someone cleared it on the portfolio screen — and
    this browser still holds yesterday's cache. Without the browser's flag,
    every load would put all of it back."""
    fake = fake_broker(watchlist=[])
    body = _boot(client, local=["SPY", "QQQ"], migrated=True).json()

    assert [c[0] for c in fake.calls] == ["GET", "GET"], "no import"
    assert body == {"available": True, "linked": True, "customer": 10,
                    "symbols": [], "imported": 0}


def test_an_empty_browser_offers_nothing_to_import(client, fake_broker):
    fake = fake_broker(watchlist=[])
    body = _boot(client, local=[]).json()
    assert [c[0] for c in fake.calls] == ["GET", "GET"]
    assert body["imported"] == 0


def test_local_symbols_are_normalised_before_they_are_offered(client, fake_broker):
    """The broker upper-cases on the way in. Doing it here too means the
    desk's idea of a symbol and the broker's cannot drift, and blanks never
    become rows."""
    fake = fake_broker(watchlist=[])
    _boot(client, local=["  spy ", "", "  ", "qqq"])

    posts = [c for c in fake.calls if c[0] == "POST"]
    assert posts[0][2]["symbols"] == [{"symbol": "SPY"}, {"symbol": "QQQ"}]


# ------------------------------------------------------------ watch != trade

def test_the_shared_list_carries_its_tradable_flags_through(client, fake_broker):
    """WATCHING IS NOT TRADING. The broker answers a per-symbol predicate and
    the desk is a pipe for it, not an editor: dropping the flag here would put
    the decision back in the page, where it would be re-derived and drift."""
    fake_broker(watchlist=[
        {"symbol": "SPY", "price": None, "tradable": False, "name": None, "exchange": None},
        {"symbol": "BTC", "price": "104200.00", "tradable": True,
         "name": "Bitcoin", "exchange": "CRYPTO"},
    ])
    rows = _boot(client, local=[]).json()["symbols"]

    assert rows[0]["tradable"] is False and rows[1]["tradable"] is True
    # and an unpriced row stays unpriced · zero is a price and it is the wrong one
    assert rows[0]["price"] is None
    assert "0" != rows[0]["price"]


# ------------------------------------------------- the guard is PER CUSTOMER

def test_switching_customer_still_imports_because_the_guard_is_per_book(client, fake_broker):
    """THE ONE THAT COST SOMEBODY THEIR RAIL.

    The migration guard used to be one global boolean in localStorage, set on
    the first successful bootstrap whatever customer that was for — and the
    link button's own tooltip invites you to change customers.

    So the SECOND link could never import. The server's guard is `not rows and
    local and not migrated`, migrated was already True, so no import ran, the
    shared list came back empty, and the client then ran adopt(WL, []) followed
    by wlSave(). adopt with an empty shared list drops every symbol from every
    section and keeps the sections, so eight curated sections, their order and
    their colour flags were persisted as empty and a reload could not undo it.
    Relinking to the original customer restored membership only: with no local
    section still holding them, all hundred-odd symbols landed in one inbox.

    Keyed per customer, linking a second book is just a first link for that
    book. The browser has migrated into 7 and not into 12, and says so.
    """
    fake = fake_broker(customer=12, watchlist=[])
    body = _boot(client, local=["SPY", "QQQ"], migrated=[7]).json()

    assert "POST" in [c[0] for c in fake.calls], \
        "the import MUST run · this browser has never handed its cache to customer 12"
    assert body["imported"] == 2
    assert [s["symbol"] for s in body["symbols"]] == ["SPY", "QQQ"], \
        "so the rail survives the switch instead of being adopted into emptiness"


def test_the_same_customer_is_still_only_migrated_once(client, fake_broker):
    """The other direction, unchanged: membership in the set means migrated.
    Without this the per-customer key would just be a global False and every
    load would resurrect symbols someone deliberately removed."""
    fake = fake_broker(customer=7, watchlist=[])
    body = _boot(client, local=["SPY", "QQQ"], migrated=[7]).json()

    assert [c[0] for c in fake.calls] == ["GET", "GET"], "no import for a book already handed over"
    assert body["imported"] == 0
    assert body["symbols"] == []


def test_a_bare_boolean_still_means_what_it_meant(client, fake_broker):
    """An older page that has not reloaded keeps sending `true`. It must not
    silently become un-migrated for every book at once, because that is the
    resurrection this guard exists to prevent."""
    fake = fake_broker(customer=7, watchlist=[])
    body = _boot(client, local=["SPY"], migrated=True).json()

    assert [c[0] for c in fake.calls] == ["GET", "GET"], "no import"
    assert body["imported"] == 0
