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
from fastapi import FastAPI, HTTPException, Request
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

# The landing portal (apex origin) calls /api/* from the browser.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://b4rruf3t.com", "https://www.b4rruf3t.com",
                   "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
STATIC = Path(__file__).resolve().parent / "static"
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime-2.1")


def AUDIO_CONFIG(voice: str) -> dict:
    """Voice out + SEMANTIC turn detection in: the model waits for end-of-thought,
    not just end-of-silence. Eagerness LOW = patient turn-taking, far fewer
    responses triggered by coughs/taps/background noise."""
    return {
        "output": {"voice": voice},
        "input": {
            "turn_detection": {"type": "semantic_vad", "eagerness": "low"},
            # transcribe the caller so their words render in the chat log
            "transcription": {"model": "gpt-4o-mini-transcribe"},
        },
    }


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/summary/{ticker}")
def ticker_summary(ticker: str):
    """Everything the dashboard needs for one ticker, in one call."""
    from common import market, news, signals, social

    snap = market.latest_snapshot(ticker)
    if snap is None:
        raise HTTPException(404, f"no data for '{ticker}' — covered: SPY, QQQ, IWM")
    return {
        "snapshot": snap,
        "trade": signals.recommend_trade(ticker),
        "headlines": news.fetch_news(ticker),
        "buzz": social.fetch_buzz(ticker),
        "trending": social.fetch_trending(),
        "ta_signals": latest_ta_signals(ticker),
    }


@app.post("/webhook/openai")
async def openai_webhook(request: Request):
    """Incoming phone calls (Realtime SIP): verify, accept as Riley, and run
    her tools server-side for the duration of the call. See docs/PHONE.md."""
    from web import phone

    if not phone.webhook_secret():
        raise HTTPException(503, "phone line not configured (OPENAI_WEBHOOK_SECRET)")
    event = phone.verify_and_parse(await request.body(), request.headers)
    if event is None:
        raise HTTPException(400, "invalid webhook signature")
    if event.get("type") == "realtime.call.incoming":
        return phone.handle_incoming(event)
    return {"handled": False, "type": event.get("type")}


@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request, token: str = ""):
    """Receives TradingView alert webhooks :
    custom indicators fire -> the desk knows within seconds. Token-gated."""
    expected = os.getenv("TV_WEBHOOK_TOKEN")
    if not expected or token != expected:
        raise HTTPException(403, "bad token")
    raw = (await request.body()).decode("utf-8", errors="ignore")[:2000]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"signal": raw}
    from datetime import datetime, timezone

    from common.db import get_connection

    conn = get_connection()
    conn.execute(
        "INSERT INTO ta_signals (created_at, ticker, signal, price, interval, payload)"
        " VALUES (?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(),
         str(data.get("ticker", "?")).upper()[:12],
         str(data.get("signal", raw))[:200],
         data.get("price"), str(data.get("interval", ""))[:12], raw),
    )
    conn.commit()
    conn.close()
    return {"stored": True}


def latest_ta_signals(ticker: str, limit: int = 5) -> list[dict]:
    from common.db import get_connection

    conn = get_connection()
    rows = conn.execute(
        "SELECT created_at, signal, price, interval FROM ta_signals"
        " WHERE ticker = ? ORDER BY id DESC LIMIT ?", (ticker.upper(), limit)).fetchall()
    conn.close()
    return [{"created_at": r[0], "signal": r[1], "price": r[2], "interval": r[3]} for r in rows]


@app.get("/api/spot/{ticker}")
def spot(ticker: str):
    """Lightweight spot for always-on price chips (SPY + the XSP estimate)."""
    from common import market, signals

    snap = market.latest_snapshot(ticker)
    if snap is None:
        raise HTTPException(404, f"no data for '{ticker}'")
    out = {"ticker": snap["ticker"], "spot": snap["spot"], "as_of": snap["captured_at"]}
    if ticker.upper() in ("SPY", "XSP"):
        out["xsp_est"] = round(snap["spot"] + signals.XSP_OFFSET, 2)
    return out


@app.get("/api/xpulse/{ticker}")
def x_pulse(ticker: str):
    """X chatter summary via Grok x_search — separate endpoint because the
    live search takes seconds; the dashboard loads it async."""
    from common import xpulse

    if not xpulse.available():
        return {"available": False}
    p = xpulse.pulse(ticker)
    return {"available": True, "pulse": p}


PERSONA_CATEGORY = {"marcus": "finance", "riley": "agency", "quinn": "agency"}


@app.get("/agents")
def agents():
    from web import personas_store

    return {
        "text_agents": registry.AGENT_META,
        "voice_personas": [
            {"id": pid, "label": p["label"], "tagline": p["tagline"],
             "category": PERSONA_CATEGORY.get(pid, "agency")}
            for pid, p in PERSONAS.items()
        ] + personas_store.list_customs(),
        "realtime_model": REALTIME_MODEL,
        "builder": {"voices": personas_store.VOICES, "tools": personas_store.TOOL_ALLOWLIST},
    }


class PersonaIn(BaseModel):
    label: str
    tagline: str = ""
    voice: str
    instructions: str
    tools: list[str] = []


@app.post("/api/personas")
def create_persona(body: PersonaIn, request: Request):
    """The agent builder. Admin-gated: requires the admin token to mint new agents."""
    from web import personas_store

    admin = os.getenv("ADMIN_TOKEN") or os.getenv("TV_WEBHOOK_TOKEN")
    if not admin or request.headers.get("x-admin-token") != admin:
        raise HTTPException(403, "admin token required")
    try:
        return personas_store.create(body.label, body.tagline or "custom agent",
                                     body.voice, body.instructions, body.tools)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


_ATLAS_CACHE: dict = {}


@app.get("/api/atlas")
def atlas():
    """The real runtime topology of every agent, as mermaid — drawn from the
    compiled LangGraph graphs themselves, not hand-made diagrams."""
    if _ATLAS_CACHE:
        return _ATLAS_CACHE
    graphs = []
    try:
        rt = registry.runtime()
        for meta in registry.AGENT_META:
            aid = meta["id"]
            if aid == "brief":
                nodes = ["question", "prompt + live context", "LLM structured output", "typed answer"]
                edges = [{"s": nodes[i], "t": nodes[i + 1], "cond": False} for i in range(3)]
            else:
                g = rt[aid].get_graph()
                nodes = list(g.nodes)
                edges = [{"s": e.source, "t": e.target, "cond": bool(e.conditional)}
                         for e in g.edges]
            graphs.append({"id": aid, "name": meta["name"], "category": meta["category"],
                           "kind": "LangGraph runtime" if aid != "brief" else "LangChain call",
                           "nodes": nodes, "edges": edges})
    except Exception as exc:
        return {"error": str(exc), "graphs": []}
    for pid, p in PERSONAS.items():
        tool_names = [t["name"] for t in p["tools"]]
        nodes = ["caller audio", "gpt-realtime-2.1 + persona"] + tool_names
        edges = ([{"s": "caller audio", "t": nodes[1], "cond": False},
                  {"s": nodes[1], "t": "caller audio", "cond": False}] +
                 [{"s": nodes[1], "t": t, "cond": True} for t in tool_names] +
                 [{"s": t, "t": nodes[1], "cond": True} for t in tool_names])
        graphs.append({"id": pid, "name": p["label"],
                       "category": PERSONA_CATEGORY.get(pid, "agency"),
                       "kind": "Realtime voice (WebRTC / SIP)",
                       "nodes": nodes, "edges": edges})
    _ATLAS_CACHE.update({"graphs": graphs})
    return _ATLAS_CACHE


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
    from web import personas_store

    p = personas_store.resolve(persona)
    if p is None:
        raise HTTPException(404, f"unknown persona '{persona}'")
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
    if persona in PERSONAS:
        return {"output": run_tool(persona, call.name, call.arguments)}
    from web import personas_store

    if personas_store.resolve(persona) is None:
        raise HTTPException(404, f"unknown persona '{persona}'")
    return {"output": personas_store.run_custom_tool(persona, call.name, call.arguments)}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
