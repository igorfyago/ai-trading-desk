"""Integration tests: the FastAPI surface end-to-end (no LLM, no network).

The conftest plants a dummy OPENAI_API_KEY, so anything that would reach
OpenAI fails fast and predictably — which is itself what we assert.
"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from web.server import app

    return TestClient(app)


def test_index_serves_minitrade_and_marcus_has_his_own_path(client):
    """This host IS the trading app now: / is minitrade, and the agent chat UI
    it embeds lives at /marcus so the cockpit can frame it same-origin."""
    r = client.get("/")
    assert r.status_code == 200
    assert b"minitrade" in r.content
    assert b'data-pane="dash"' in r.content        # the trade cockpit is the default

    m = client.get("/marcus")
    assert m.status_code == 200
    assert b"AI TRADING" in m.content


def test_agent_catalog_is_personas_only(client):
    """The chat agents and the builder moved to the observatory. What is left
    is the persona catalog, which doubles as the container healthcheck, so it
    must stay cheap: no LLM, no DB, no custom-persona lookup."""
    d = client.get("/agents").json()
    assert "text_agents" not in d and "builder" not in d
    assert {p["id"] for p in d["voice_personas"]} == {"marcus"}
    for p in d["voice_personas"]:
        assert p["label"] and p["tagline"]


def test_unknown_persona_404s(client):
    assert client.post("/session/nobody").status_code == 404
    assert client.post("/tool/nobody", json={"name": "x", "arguments": {}}).status_code == 404


def test_marcus_tool_runs_server_side_and_books_the_quote(client, db_conn):
    """The tool endpoint is what the Realtime session calls mid-conversation.
    It must run the engine server-side and leave the quote in the book, which
    is what lets the chart and a later call agree on what was said."""
    r = client.post("/tool/marcus", json={
        "name": "trade_recommendation", "arguments": {"ticker": "IWM"},
        "session": "integration-book"})
    assert r.status_code == 200
    out = json.loads(r.json()["output"])
    assert out["ticker"] == "IWM" and out["legs"] and "disclaimer" not in out


def test_the_chat_surface_is_gone_from_the_desk(client):
    """The five text agents and their NDJSON chat stream moved to the
    observatory. Leaving dead routes here is how you end up with two versions
    of the same thing, which is the whole point of the split."""
    for path in ("/chat/brief", "/chat/analyst/resume", "/tool/bridge/brief"):
        assert client.post(path, json={"message": "x", "session": "s"}).status_code == 404
    assert client.get("/api/atlas").status_code == 404


def test_session_mint_fails_clean_with_dummy_key(client):
    r = client.post("/session/marcus")
    assert r.status_code == 401  # OpenAI rejection is passed through, not a crash


def test_watch_endpoint_speaks_the_pitch_layer(client, monkeypatch):
    """/api/watch/{ticker} is the heartbeat's whole diet. Given the pitched
    trade back as query params it must answer with fingerprinted EVENTS, and
    keep the legacy top-level shape so a call that stayed open through a
    deploy keeps its heartbeat."""
    from common import market, signals, tape as tape_mod
    from web import server

    read = {"spot": 748.0, "bar_t": 1750073400, "capitulation": None,
            "day_shape": None, "bands_ok": True,
            "action": {"stance": "wait"}}
    monkeypatch.setattr(tape_mod, "get_tape_read", lambda t, interval="15m": dict(read))
    monkeypatch.setattr(signals, "recommend_trade",
                        lambda t, **k: {"ticker": "SPY", "spot": 748.0,
                                        "execution": {"kind": "call"},
                                        "tape": {"bands_ok": True,
                                                 "action": {"stance": "wait"}}})
    monkeypatch.setattr(market, "latest_snapshot",
                        lambda t, as_of=None: {"atm_iv": 0.12})
    monkeypatch.setattr(market, "days_to", lambda e, as_of=None: 1.0)
    # the pitched 746c bought at 2.00 now prices at 3.20: the trim event
    monkeypatch.setattr(market, "black_scholes",
                        lambda s, k, d, iv, kind: {"price": 3.20})

    r = client.get("/api/watch/SPY", params={
        "kind": "call", "strike": 746, "entry": 2.00,
        "expiry": "2026-07-21", "add": 747.0}).json()
    evs = {e["event"]: e for e in r["events"]}
    assert "trim" in evs and evs["trim"]["fingerprint"].startswith("SPY:trim:")
    assert "add" in evs                       # spot 748 is through the 747 add
    assert r["signal"] is None                # no tape setup: legacy field honest

    # no pitch params -> tape layer only, and never a crash
    r2 = client.get("/api/watch/SPY").json()
    assert r2["events"] == [] and r2["signal"] is None

    # a capitulation still speaks in BOTH shapes (legacy client + new client)
    read2 = {**read, "capitulation": {"why": "4.2x flush, RSI 22", "side": "long"}}
    monkeypatch.setattr(tape_mod, "get_tape_read", lambda t, interval="15m": dict(read2))
    r3 = client.get("/api/watch/SPY").json()
    assert r3["signal"] == "capitulation"                       # legacy
    assert r3["fingerprint"] == "SPY:capitulation:1750073400"   # legacy
    assert r3["events"][0]["event"] == "new_setup"              # v2
    assert r3["events"][0]["fingerprint"] == r3["fingerprint"]  # same identity
