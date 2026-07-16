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


def test_index_serves_app(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"AI TRADING" in r.content


def test_agent_catalog(client):
    d = client.get("/agents").json()
    assert [a["id"] for a in d["text_agents"]] == ["brief", "sql", "repo", "research", "analyst"]
    assert {p["id"] for p in d["voice_personas"]} == {"riley", "quinn", "marcus"}
    for a in d["text_agents"]:
        assert a["name"] and a["desc"]


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
    assert out["ticker"] == "IWM" and out["legs"] and "disclaimer" in out


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
