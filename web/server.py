"""Recruiter demo server: talk to the desk's voice agents from a browser.

Endpoints
---------
GET  /                        the demo page (WebRTC client)
POST /session/{persona}       mints a short-lived Realtime client secret with the
                              persona's instructions/voice/tools baked in server-side
POST /tool/{persona}          executes a function call on the server and returns the
                              output for the browser to hand back to the model

The browser never sees the real API key — only an ephemeral secret scoped to
one session. Tool code and the database never leave the server.

Run:  uvicorn web.server:app --reload   (from the repo root)
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# import via the agents folder (numeric package names aren't importable)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents" / "06_voice"))
from personas import PERSONAS, run_tool  # noqa: E402

load_dotenv()
app = FastAPI(title="ai-trading-desk voice demo")
STATIC = Path(__file__).resolve().parent / "static"

REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime-2.1")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/session/{persona}")
async def create_session(persona: str):
    """Mint an ephemeral client secret for one WebRTC session."""
    if persona not in PERSONAS:
        raise HTTPException(404, f"unknown persona '{persona}'")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(500, "OPENAI_API_KEY not configured on the server")

    p = PERSONAS[persona]
    payload = {
        "session": {
            "type": "realtime",
            "model": REALTIME_MODEL,
            "instructions": p["instructions"],
            "tools": p["tools"],
            "tool_choice": "auto",
            "audio": {"output": {"voice": p["voice"]}},
        }
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"OpenAI error: {resp.text[:300]}")
    data = resp.json()
    return {"client_secret": data["value"], "label": p["label"], "model": REALTIME_MODEL}


class ToolCall(BaseModel):
    name: str
    arguments: dict


@app.post("/tool/{persona}")
def execute_tool(persona: str, call: ToolCall):
    """The browser forwards Realtime function calls here; the desk code runs server-side."""
    if persona not in PERSONAS:
        raise HTTPException(404, f"unknown persona '{persona}'")
    return {"output": run_tool(persona, call.name, call.arguments)}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
