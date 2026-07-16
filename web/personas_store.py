"""Custom voice personas: the editable layer of the agent builder.

A custom persona = label + voice + instructions + a selection of tools from
the SAFE ALLOWLIST below (existing, server-side, read-mostly tools). Stored
in the DB, served next to the built-in personas, talk-to-able immediately.

Creation is admin-token-gated: the builder UI is public to look at, but only
the desk owner can actually mint new agents on this server.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents" / "06_voice"))

import personas as builtin  # noqa: E402

from common.db import get_connection  # noqa: E402

# name -> (schema, impl) harvested from the built-in personas
_ALL_TOOLS: dict[str, tuple[dict, callable]] = {}
for _p in builtin.PERSONAS.values():
    for _t in _p["tools"]:
        _ALL_TOOLS[_t["name"]] = (_t, _p["implementations"][_t["name"]])

TOOL_ALLOWLIST = sorted(_ALL_TOOLS)
VOICES = ["marin", "cedar", "sage", "alloy", "ash", "ballad", "coral", "echo", "shimmer", "verse"]

_TABLE = """CREATE TABLE IF NOT EXISTS custom_personas (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    tagline TEXT NOT NULL,
    voice TEXT NOT NULL,
    instructions TEXT NOT NULL,
    tools_csv TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""


def _conn():
    conn = get_connection()
    conn.execute(_TABLE)
    return conn


def create(label: str, tagline: str, voice: str, instructions: str, tools: list[str]) -> dict:
    pid = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:24]
    if not pid or pid in builtin.PERSONAS:
        raise ValueError("bad or reserved name")
    if voice not in VOICES:
        raise ValueError(f"voice must be one of {VOICES}")
    bad = [t for t in tools if t not in _ALL_TOOLS]
    if bad:
        raise ValueError(f"unknown tools: {bad}")
    if not (40 <= len(instructions) <= 6000):
        raise ValueError("instructions must be 40-6000 chars")
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO custom_personas VALUES (?,?,?,?,?,?,?)",
        (pid, label[:60], tagline[:120], voice, instructions, ",".join(tools),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return {"id": pid, "label": label}


def list_customs() -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT id, label, tagline FROM custom_personas ORDER BY created_at").fetchall()
    conn.close()
    return [{"id": r[0], "label": r[1], "tagline": r[2], "category": "custom"} for r in rows]


def resolve(pid: str) -> dict | None:
    """Return a persona dict (same shape as built-ins) for any id."""
    if pid in builtin.PERSONAS:
        return builtin.PERSONAS[pid]
    conn = _conn()
    row = conn.execute(
        "SELECT label, tagline, voice, instructions, tools_csv FROM custom_personas"
        " WHERE id = ?", (pid,)).fetchone()
    conn.close()
    if not row:
        return None
    tool_names = [t for t in row[4].split(",") if t]
    return {
        "label": row[0], "tagline": row[1], "voice": row[2],
        "instructions": row[3] + "\n\n" + builtin.VOICE_STYLE,
        "tools": [_ALL_TOOLS[t][0] for t in tool_names],
        "implementations": {t: _ALL_TOOLS[t][1] for t in tool_names},
    }


def run_custom_tool(pid: str, name: str, arguments: dict) -> str:
    p = resolve(pid)
    impl = (p or {}).get("implementations", {}).get(name)
    if impl is None:
        return json.dumps({"error": f"unknown tool {name} for persona {pid}"})
    try:
        return impl(**arguments)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
