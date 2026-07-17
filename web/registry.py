"""Agent registry for the web UI.

Lazy-loads the six agents once, gives each a stable id, and exposes two ways
to run them:
  stream_chat()  — generator of UI events (node progress, tool calls, tokens,
                   HITL interrupts, final answer) for the chat frontend
  run_for_voice() — synchronous call used by the Realtime "voice bridge",
                   which lets you TALK to any text agent

Loading is lazy so the server boots (and serves the page with a helpful
error) even before OPENAI_API_KEY is configured.
"""

import importlib.util
import re
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

AGENT_META = [   # category: finance | agency | custom
    {"id": "brief", "category": "finance", "level": 1, "name": "Market Brief",
     "desc": "Structured-output strategist. One LLM call, typed answer.",
     "hint": "Is SPY pinned by dealers into Friday opex?"},
    {"id": "sql", "category": "finance", "level": 2, "name": "SQL Analyst",
     "desc": "English → SQL over the GEX database, self-correcting tool loop.",
     "hint": "Top 5 put walls by strength, with spot at the time"},
    {"id": "repo", "category": "finance", "level": 3, "name": "Repo Guide",
     "desc": "RAG over the options-flow-analytics codebase, with citations.",
     "hint": "How is the gamma flip level computed?"},
    {"id": "research", "category": "finance", "level": 4, "name": "Research Desk",
     "desc": "LangGraph: plan → multi-tool research → critic loop → report.",
     "hint": "Compare dealer positioning on SPY vs QQQ"},
    {"id": "analyst", "category": "finance", "level": 5, "name": "Desk Analyst",
     "desc": "Multi-path graph: regime router, 3 parallel specialists, risk gate, "
             "and YOUR approval before publishing.",
     "hint": "Run the desk on SPY"},
]

from common import tickers as universe

# any watchlist name matches case-insensitively — EXCEPT tickers that collide
# with English (NOW, BE, RUN...), which need CAPS or a $ prefix to count
_SAFE = sorted(universe.WATCHLIST - universe.AMBIGUOUS, key=len, reverse=True)
_AMB = sorted(universe.AMBIGUOUS & universe.WATCHLIST, key=len, reverse=True)
# (?!\w) instead of \b as the right boundary — \b can't follow "ES1!"
TICKER_RE = re.compile(r"(?<![\w$])(" + "|".join(map(re.escape, _SAFE)) + r")(?!\w)", re.I)
_AMB_RE = re.compile(r"(?i:\$(" + "|".join(map(re.escape, _AMB)) + r")(?!\w))|"
                     r"(?<![\w$])(" + "|".join(map(re.escape, _AMB)) + r")(?!\w)")


def extract_tickers(text: str) -> list[str]:
    found = {m.upper() for m in TICKER_RE.findall(text)}
    for dollar, caps in _AMB_RE.findall(text):        # $now or literal NOW
        found.add((dollar or caps).upper())
    return sorted(found, key=lambda t: (t != "SPY", t))


def _live_context(text: str) -> str:
    """One source of truth for every financial agent: LIVE spot from the shared
    feed, the latest chain snapshot (structure), plus fresh headlines."""
    from common import market, news

    # SPY is the desk's home ticker: it leads when named, and no ticker at all
    # means SPY only — the rest of the universe joins when the user names it
    tickers = extract_tickers(text) or ["SPY"]
    parts = []
    for t in tickers[:3]:
        live = market.live_spot(t)
        if live:
            parts.append(
                f"{t} LIVE spot {live['price']} ({live['source']}"
                f"{', 15m-delayed' if live['delayed'] else ''}, {live['ts']})")
        snap = market.latest_snapshot(t)
        if snap:
            parts.append(
                f"{t} structure: {'spot ' + str(snap['spot']) + ', ' if not live else ''}"
                f"regime {snap['regime']}, gamma flip "
                f"{snap['gamma_flip']}, signal {snap['signal_score']}, ATM IV "
                f"{snap['atm_iv']}, as of {snap['captured_at']}")
    block = news.headlines_block(tickers[0])
    if block:
        parts.append(block)
    from common import trades

    book = trades.book_block()
    if book:
        parts.append(book + "\n(The book can change at any moment from the "
                     "chart's ADD/SELL/CLOSE buttons — this snapshot is current "
                     "as of this message.)")
    return "\n".join(parts)

_lock = threading.Lock()
_rt: dict = {}
_threads: dict = {}   # (session, agent) -> thread_id for checkpointed agents
_pending: dict = {}   # session -> analyst thread config awaiting approval


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def runtime() -> dict:
    """Build all agents once. Raises RuntimeError with a friendly message."""
    with _lock:
        if _rt:
            return _rt
        import os
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set on the server — add it to .env")
        from langgraph.checkpoint.memory import InMemorySaver

        brief = _load("agent_brief", "agents/01_market_brief/main.py")
        sql = _load("agent_sql", "agents/02_text_to_sql/main.py")
        repo = _load("agent_repo", "agents/03_repo_interpreter/main.py")
        research = _load("agent_research", "agents/04_research_graph/main.py")
        analyst = _load("agent_analyst", "agents/05_desk_analyst/main.py")

        _rt.update({
            "brief": brief,
            "sql": sql.build_agent(checkpointer=InMemorySaver()),
            "repo": repo.build_agent(checkpointer=InMemorySaver()),
            "research": research.build_graph(),
            "analyst": analyst.build_graph(),   # has its own InMemorySaver
        })
        return _rt


def _config(session: str, agent: str, extra: dict | None = None) -> dict:
    key = (session, agent)
    if key not in _threads:
        _threads[key] = f"{agent}-{session}-{uuid.uuid4().hex[:8]}"
    cfg = {"run_name": f"web:{agent}", "tags": ["web-ui", agent],
           "metadata": {"session": session},
           "configurable": {"thread_id": _threads[key]},
           "recursion_limit": 80}
    if extra:
        cfg.update(extra)
    return cfg


def _tool_events(update: dict):
    """Extract tool-call chips from a LangGraph/agent state update."""
    for node_update in update.values():
        if not isinstance(node_update, dict):
            continue
        for msg in node_update.get("messages", []) or []:
            for tc in getattr(msg, "tool_calls", None) or []:
                args = ", ".join(f"{k}={str(v)[:60]}" for k, v in tc["args"].items())
                yield {"type": "tool", "name": tc["name"], "args": args}


def _log_chat(session: str, agent: str, role: str, content: str) -> None:
    """One conversation, every surface: the transcript is server-side so the
    dashboard's Marcus panel and the full desk replay the SAME chat."""
    if not content:
        return
    try:
        from datetime import datetime, timezone

        from common.db import get_connection

        conn = get_connection()
        conn.execute(
            "INSERT INTO chat_log (created_at, session, agent, role, content)"
            " VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), session, agent, role,
             content[:8000]))
        conn.commit()
        conn.close()
    except Exception:
        pass  # the transcript is a convenience, never a failure


def stream_chat(agent_id: str, text: str, session: str):
    """Yield UI events for one user message (tee'd into the shared transcript)."""
    _log_chat(session, agent_id, "user", text)
    for ev in _stream_chat_inner(agent_id, text, session):
        if ev.get("type") == "final":
            _log_chat(session, agent_id, "assistant", ev.get("text", ""))
        yield ev


def _stream_chat_inner(agent_id: str, text: str, session: str):
    try:
        rt = runtime()
    except Exception as exc:
        yield {"type": "error", "text": str(exc)}
        return

    try:
        if agent_id == "brief":
            result = rt["brief"].run(text, context=_live_context(text))
            meta = " · ".join(x for x in (", ".join(result.tickers), result.horizon) if x and x != "N/A")
            md = result.answer + (f"\n\n<small>*{meta}*</small>" if meta else "")
            yield {"type": "final", "text": md}

        elif agent_id in ("sql", "repo"):
            final = ""
            for mode, chunk in rt[agent_id].stream(
                {"messages": [{"role": "user", "content": text}]},
                config=_config(session, agent_id), stream_mode=["messages", "updates"],
            ):
                if mode == "messages":
                    msg, meta = chunk
                    content = getattr(msg, "content", "")
                    if isinstance(content, str) and content and getattr(msg, "type", "") == "AIMessageChunk":
                        final += content
                        yield {"type": "token", "text": content}
                else:
                    yield from _tool_events(chunk)
            yield {"type": "final", "text": final}

        elif agent_id == "research":
            report = ""
            for update in rt["research"].stream(
                {"question": text, "messages": [], "revisions": 0},
                config=_config(session, agent_id), stream_mode="updates",
            ):
                for node, val in update.items():
                    yield {"type": "node", "name": node}
                    if isinstance(val, dict) and val.get("report"):
                        report = val["report"]
                yield from _tool_events(update)
            yield {"type": "final", "text": report or "(no report produced)"}

        elif agent_id == "analyst":
            m = TICKER_RE.search(text)
            ticker = m.group(1).upper() if m else "SPY"
            cfg = _config(session, agent_id)
            yield from _run_analyst(rt["analyst"],
                                    {"ticker": ticker, "analyses": [], "rounds": 0}, cfg, session)
        else:
            yield {"type": "error", "text": f"unknown agent '{agent_id}'"}
    except Exception as exc:
        yield {"type": "error", "text": f"{type(exc).__name__}: {exc}"}


def _run_analyst(graph, graph_input, cfg, session):
    for update in graph.stream(graph_input, config=cfg, stream_mode="updates"):
        if "__interrupt__" in update:
            _pending[session] = cfg
            payload = update["__interrupt__"][0].value
            yield {"type": "interrupt", "memo": payload.get("memo", payload)}
            return
        for node in update:
            yield {"type": "node", "name": node}
        yield from _tool_events(update)
    state = graph.get_state(cfg).values
    if state.get("published_path"):
        import json as _json
        yield {"type": "final",
               "text": "**Memo approved & published** → `" + state["published_path"] + "`\n\n```json\n"
                       + _json.dumps(state["memo"], indent=2) + "\n```"}
    else:
        yield {"type": "final", "text": "Memo was not published."}


def resume_analyst(session: str, action: str, notes: str):
    """Continue the paused desk-analyst graph after a human decision."""
    cfg = _pending.pop(session, None)
    if cfg is None:
        yield {"type": "error", "text": "No memo is awaiting approval in this session."}
        return
    from langgraph.types import Command
    rt = runtime()
    try:
        yield from _run_analyst(rt["analyst"], Command(resume={"action": action, "notes": notes}),
                                cfg, session)
    except Exception as exc:
        yield {"type": "error", "text": f"{type(exc).__name__}: {exc}"}


# ------------------------------------------------------------ voice bridge ----

def run_for_voice(agent_id: str, question: str, session: str) -> dict:
    """Synchronous agent call for the Realtime voice bridge tool."""
    events = list(stream_chat(agent_id, question, session))
    for ev in events:
        if ev["type"] == "interrupt":
            return {"status": "APPROVAL_REQUIRED",
                    "memo": ev["memo"],
                    "instruction": "Read the memo's thesis, trade idea and conviction to the "
                                   "caller and ask them to approve, revise (with notes) or "
                                   "reject. Then call resolve_approval."}
    for ev in reversed(events):
        if ev["type"] == "final":
            return {"status": "ok", "answer": ev["text"]}
        if ev["type"] == "error":
            return {"status": "error", "detail": ev["text"]}
    return {"status": "error", "detail": "agent produced no answer"}


def resolve_voice_approval(session: str, action: str, notes: str) -> dict:
    events = list(resume_analyst(session, action, notes))
    for ev in events:
        if ev["type"] == "interrupt":
            return {"status": "APPROVAL_REQUIRED", "memo": ev["memo"]}
    for ev in reversed(events):
        if ev["type"] == "final":
            return {"status": "ok", "result": ev["text"]}
        if ev["type"] == "error":
            return {"status": "error", "detail": ev["text"]}
    return {"status": "error", "detail": "no result"}
