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
    assert {p["id"] for p in d["voice_personas"]} == {"riley", "quinn", "marcus"}
    for p in d["voice_personas"]:
        assert p["label"] and p["tagline"]


def test_unknown_persona_404s_without_the_custom_store(client):
    assert client.post("/session/nobody").status_code == 404
    assert client.post("/tool/nobody", json={"name": "x", "arguments": {}}).status_code == 404


def test_voice_tool_roundtrip_writes_db(client, db_conn):
    r = client.post("/tool/riley", json={
        "name": "book_appointment",
        "arguments": {"patient_name": "Integration Ida", "contact": "ida@x.com",
                      "service": "checkup", "slot": "Wed 11:00"}})
    assert r.status_code == 200
    assert json.loads(r.json()["output"])["status"] == "booked"
    n = db_conn.execute(
        "SELECT COUNT(*) FROM appointments WHERE patient_name='Integration Ida'").fetchone()[0]
    assert n == 1


def test_marcus_trade_recommendation_via_api(client):
    r = client.post("/tool/marcus", json={"name": "trade_recommendation",
                                          "arguments": {"ticker": "IWM"}})
    out = json.loads(r.json()["output"])
    assert out["ticker"] == "IWM" and out["legs"] and "disclaimer" not in out


def test_unknown_persona_and_agent_404(client):
    assert client.post("/tool/nobody", json={"name": "x", "arguments": {}}).status_code == 404
    assert client.post("/chat/nobody", json={"message": "hi", "session": "s"}).status_code == 404


def test_chat_streams_ndjson_and_fails_clean_without_real_key(client):
    r = client.post("/chat/brief", json={"message": "test", "session": "it-1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.strip().splitlines()]
    assert events, "stream must yield at least one event"
    assert all("type" in e for e in events)
    # dummy key → the run must surface as a structured error event, not a 500
    assert events[-1]["type"] == "error"


def test_resume_without_pending_memo_is_clean(client):
    r = client.post("/chat/analyst/resume",
                    json={"session": "never-ran", "action": "approve", "notes": ""})
    events = [json.loads(line) for line in r.text.strip().splitlines()]
    assert events[-1]["type"] == "error"
    assert "awaiting approval" in events[-1]["text"]


def test_session_mint_fails_clean_with_dummy_key(client):
    r = client.post("/session/marcus")
    assert r.status_code == 401  # OpenAI rejection is passed through, not a crash
