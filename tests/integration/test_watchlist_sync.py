"""The desk watchlist's write-through to the minibank broker.

The whole point of this route is that it is OPTIONAL. The watchlist is a
local-first feature that worked before the broker existed; it has to keep
working when the broker is down, unbuilt, or not deployed at all. So the
tests that matter most here are the failure ones: every one of them must be
a 200 with synced=False, never a 5xx, never an exception out of the handler.

No network is touched. httpx.AsyncClient is replaced with a fake whose
behaviour each test dictates.
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


class _FakeClient:
    """Stands in for httpx.AsyncClient. Records calls, replays scripted answers."""

    def __init__(self, link=None, post=None, raise_on=None):
        self.link = link if link is not None else _Resp(200, {"customer": 10})
        self.post_resp = post if post is not None else _Resp(200, {"result": "ok"})
        self.raise_on = raise_on          # "get" | "post" | None
        self.calls = []

    def __call__(self, *_a, **_kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        if self.raise_on == "get":
            raise ConnectionError("connection refused")
        return self.link

    async def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        if self.raise_on == "post":
            raise TimeoutError("broker timed out")
        return self.post_resp


@pytest.fixture()
def fake_broker(monkeypatch):
    """Install a fake httpx client into web.server; hand the test the handle."""
    import web.server as server

    holder = {}

    def install(**kwargs):
        fake = _FakeClient(**kwargs)
        monkeypatch.setattr(server.httpx, "AsyncClient", fake)
        holder["fake"] = fake
        return fake

    return install


def _sync(client, symbol="NVDA", action="add", session="sess-abc"):
    return client.post("/api/watchlist/sync",
                       json={"symbol": symbol, "action": action, "session": session})


# --------------------------------------------------------------- happy path

def test_add_reaches_the_broker_with_the_linked_customer(client, fake_broker):
    """A watched ticker becomes a row on the broker's watchlist for the
    customer this session is linked to — not for the id the browser claims,
    which the browser never sends."""
    fake = fake_broker()
    r = _sync(client, "NVDA", "add")

    assert r.status_code == 200
    assert r.json() == {"synced": True, "symbol": "NVDA", "action": "add"}

    get_call, post_call = fake.calls
    assert get_call[0] == "GET" and get_call[1].endswith("/api/link")
    assert get_call[2] == {"session": "sess-abc"}
    assert post_call[0] == "POST" and post_call[1].endswith("/api/watchlist")
    assert post_call[2] == {"customer": 10, "symbol": "NVDA", "action": "add"}


def test_symbol_is_normalised_and_action_is_constrained(client, fake_broker):
    """Accounts.watch upper-cases on the way in; do it here too so the desk's
    idea of a symbol and the broker's cannot drift. And anything that is not
    the literal string "remove" is an add — the broker's own rule, mirrored,
    so a typo can never silently delete a row."""
    fake = fake_broker()

    _sync(client, "  nvda  ", "add")
    assert fake.calls[-1][2]["symbol"] == "NVDA"

    _sync(client, "nvda", "remove")
    assert fake.calls[-1][2]["action"] == "remove"

    _sync(client, "nvda", "delete")          # not "remove"
    assert fake.calls[-1][2]["action"] == "add"


def test_remove_is_mirrored(client, fake_broker):
    fake = fake_broker()
    r = _sync(client, "TSLA", "remove")

    assert r.json()["synced"] is True
    assert fake.calls[-1][2] == {"customer": 10, "symbol": "TSLA", "action": "remove"}


# ------------------------------------------------ the broker is unavailable

def test_broker_unreachable_degrades_silently(client, fake_broker):
    """THE ONE THAT MATTERS. Connection refused on the link lookup — because
    the container is not running, the DNS name does not resolve, or the whole
    minibank estate was never deployed alongside this desk. The desk answers
    200 and says it did not sync. It does not 502, and it does not raise."""
    fake_broker(raise_on="get")
    r = _sync(client)

    assert r.status_code == 200
    assert r.json()["synced"] is False
    assert r.json()["reason"] == "ConnectionError"


def test_broker_times_out_on_the_write(client, fake_broker):
    """Reachable enough to answer the link, gone by the write. Same outcome:
    the local watchlist has already been saved by the browser, so a dropped
    mirror costs nothing that a later add/remove will not resend."""
    fake_broker(raise_on="post")
    r = _sync(client)

    assert r.status_code == 200
    assert r.json() == {"synced": False, "reason": "TimeoutError"}


def test_broker_error_status_is_not_an_exception(client, fake_broker):
    """A 500 out of the broker is still a 200 out of the desk."""
    fake_broker(link=_Resp(500, {}))
    assert _sync(client).json() == {"synced": False, "reason": "link 500"}

    fake_broker(post=_Resp(400, {"error": "need customer, symbol"}))
    assert _sync(client).json() == {"synced": False, "reason": "watchlist 400"}


def test_malformed_broker_response_is_caught(client, fake_broker):
    """Something answered on that port, but it is not the broker. .json()
    raising must not escape the handler either."""
    class _Junk(_Resp):
        def json(self):
            raise ValueError("not json")

    fake_broker(link=_Junk(200))
    r = _sync(client)
    assert r.status_code == 200
    assert r.json()["synced"] is False


# ------------------------------------------------------------- identity

def test_unlinked_session_writes_nothing(client, fake_broker):
    """No Accounts.link binding means we do not know whose book this is, and
    a guessed customer id writes into a stranger's watchlist. The correct
    number of requests to make in that case is zero."""
    fake = fake_broker(link=_Resp(200, {"customer": None}))
    r = _sync(client)

    assert r.json() == {"synced": False, "reason": "session not linked"}
    assert [c[0] for c in fake.calls] == ["GET"]          # never POSTed


def test_missing_session_or_symbol_never_calls_out(client, fake_broker):
    """The anonymous case. Most desk traffic will never hold an account, and
    it must not generate a broker round-trip per keystroke."""
    fake = fake_broker()

    assert _sync(client, session="").json()["synced"] is False
    assert _sync(client, symbol="  ").json()["synced"] is False
    assert fake.calls == []
