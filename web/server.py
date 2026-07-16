"""The desk's web app: one conversation UI for all six agents, text and voice.

Endpoints
---------
GET  /                          the app
GET  /agents                    agent + persona catalog for the sidebar
POST /chat/{agent_id}           NDJSON stream: node progress, tool chips,
                                tokens, HITL interrupts, final answer
POST /chat/analyst/resume       continue the desk analyst after approve/revise/reject
POST /session/{persona}         ephemeral Realtime secret for a voice-native persona
POST /session/bridge/{agent_id} ephemeral secret for the VOICE BRIDGE: a Realtime
                                session whose only tool is the chosen text agent
POST /tool/{persona}            server-side execution of voice function calls
POST /tool/bridge/{agent_id}    ditto for the bridge (runs the text agent)

Observability: with LANGSMITH_TRACING=true every chat run, every graph node
and every voice tool call lands in LangSmith under LANGSMITH_PROJECT.

Run:  uvicorn web.server:app --reload   (from the repo root)
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents" / "06_voice"))

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from personas import PERSONAS, run_tool  # noqa: E402
from web import registry  # noqa: E402

try:
    from langsmith import traceable
except ImportError:  # pragma: no cover
    def traceable(**_kw):
        return lambda f: f

load_dotenv()
app = FastAPI(title="ai-trading-desk")
STATIC = Path(__file__).resolve().parent / "static"
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime-2.1")


def AUDIO_CONFIG(voice: str) -> dict:
    """Voice out + SEMANTIC turn detection in: the model waits for end-of-thought,
    not just end-of-silence, so mid-sentence pauses don't get talked over."""
    return {
        "output": {"voice": voice},
        "input": {"turn_detection": {"type": "semantic_vad", "eagerness": "medium"}},
    }


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/agents")
def agents():
    return {
        "text_agents": registry.AGENT_META,
        "voice_personas": [
            {"id": pid, "label": p["label"], "tagline": p["tagline"]}
            for pid, p in PERSONAS.items()
        ],
        "realtime_model": REALTIME_MODEL,
    }


# ------------------------------------------------------------------- chat ----

class ChatIn(BaseModel):
    message: str
    session: str


def _ndjson(gen):
    def body():
        for event in gen:
            yield json.dumps(event, default=str) + "\n"
    return StreamingResponse(body(), media_type="application/x-ndjson")


@app.post("/chat/{agent_id}")
def chat(agent_id: str, body: ChatIn):
    if agent_id not in {a["id"] for a in registry.AGENT_META}:
        raise HTTPException(404, f"unknown agent '{agent_id}'")
    return _ndjson(registry.stream_chat(agent_id, body.message, body.session))


class ResumeIn(BaseModel):
    session: str
    action: str            # approve | revise | reject
    notes: str = ""


@app.post("/chat/analyst/resume")
def chat_resume(body: ResumeIn):
    return _ndjson(registry.resume_analyst(body.session, body.action, body.notes))


# ------------------------------------------------------------------ voice ----

async def _mint_secret(session_payload: dict) -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(500, "OPENAI_API_KEY not configured on the server")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"session": session_payload},
        )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"OpenAI error: {resp.text[:300]}")
    return resp.json()


@app.post("/session/bridge/{agent_id}")
async def create_bridge_session(agent_id: str):
    """Voice bridge: talk to any TEXT agent through a Realtime session."""
    meta = next((a for a in registry.AGENT_META if a["id"] == agent_id), None)
    if meta is None:
        raise HTTPException(404, f"unknown agent '{agent_id}'")

    tools = [{
        "type": "function", "name": "ask_agent",
        "description": f"Send the caller's request to the {meta['name']} agent and get its "
                       "full answer. Takes time for the complex agents — tell the caller "
                       "you're running it before calling.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "the caller's request, complete and specific"}},
            "required": ["question"]},
    }]
    extra = ""
    if agent_id == "analyst":
        tools.append({
            "type": "function", "name": "resolve_approval",
            "description": "After reading a memo awaiting approval to the caller, submit their decision.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["approve", "revise", "reject"]},
                "notes": {"type": "string", "description": "revision notes, if any"}},
                "required": ["action"]},
        })
        extra = (" If ask_agent returns APPROVAL_REQUIRED, summarize the memo aloud "
                 "(bias, conviction, thesis, the exact trade) and ask the caller to "
                 "approve, revise or reject; then call resolve_approval with their words.")

    data = await _mint_secret({
        "type": "realtime", "model": REALTIME_MODEL,
        "instructions": (
            f"You are the voice interface to '{meta['name']}', an AI agent: {meta['desc']} "
            "You sound natural and human, never read JSON or markdown syntax aloud. "
            "For EVERY substantive request: restate it crisply, say you're on it, call "
            "ask_agent, then deliver the answer conversationally — numbers rounded the "
            "way a person would say them, structure summarized not recited." + extra +
            " This is a demo on synthetic data; say so if the caller asks about real money."),
        "tools": tools, "tool_choice": "auto",
        "audio": AUDIO_CONFIG("alloy"),
    })
    return {"client_secret": data["value"], "label": f"{meta['name']} (voice bridge)",
            "model": REALTIME_MODEL}


@app.post("/session/{persona}")
async def create_session(persona: str):
    if persona not in PERSONAS:
        raise HTTPException(404, f"unknown persona '{persona}'")
    p = PERSONAS[persona]
    data = await _mint_secret({
        "type": "realtime", "model": REALTIME_MODEL,
        "instructions": p["instructions"],
        "tools": p["tools"], "tool_choice": "auto",
        "audio": AUDIO_CONFIG(p["voice"]),
    })
    return {"client_secret": data["value"], "label": p["label"], "model": REALTIME_MODEL}


class ToolCall(BaseModel):
    name: str
    arguments: dict
    session: str = "voice"


@app.post("/tool/bridge/{agent_id}")
@traceable(name="voice_bridge_tool", run_type="tool")
def execute_bridge_tool(agent_id: str, call: ToolCall):
    if call.name == "ask_agent":
        out = registry.run_for_voice(agent_id, call.arguments.get("question", ""), call.session)
    elif call.name == "resolve_approval":
        out = registry.resolve_voice_approval(
            call.session, call.arguments.get("action", "reject"), call.arguments.get("notes", ""))
    else:
        out = {"error": f"unknown bridge tool {call.name}"}
    return {"output": json.dumps(out, default=str)}


@app.post("/tool/{persona}")
def execute_tool(persona: str, call: ToolCall):
    if persona not in PERSONAS:
        raise HTTPException(404, f"unknown persona '{persona}'")
    return {"output": run_tool(persona, call.name, call.arguments)}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
