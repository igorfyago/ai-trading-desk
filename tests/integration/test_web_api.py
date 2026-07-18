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
