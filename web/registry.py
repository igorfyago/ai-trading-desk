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

AGENT_META = [
    {"id": "brief", "level": 1, "name": "Market Brief",
     "desc": "Structured-output strategist. One LLM call, typed answer.",
     "hint": "Is SPY pinned by dealers into Friday opex?"},
    {"id": "sql", "level": 2, "name": "SQL Analyst",
     "desc": "English → SQL over the GEX database, self-correcting tool loop.",
     "hint": "Top 5 put walls by strength, with spot at the time"},
    {"id": "repo", "level": 3, "name": "Repo Guide",
     "desc": "RAG over the options-flow-analytics codebase, with citations.",
     "hint": "How is the gamma flip level computed?"},
    {"id": "research", "level": 4, "name": "Research Desk",
     "desc": "LangGraph: plan → multi-tool research → critic loop → report.",
     "hint": "Compare dealer positioning on SPY vs QQQ"},
    {"id": "analyst", "level": 5, "name": "Desk Analyst",
     "desc": "Multi-path graph: regime router, 3 parallel specialists, risk gate, "
             "and YOUR approval before publishing.",
     "hint": "Run the desk on SPY"},
]

TICKER_RE = re.compile(r"\b(SPY|QQQ|IWM)\b", re.I)


def _live_context(text: str) -> str:
    """One source of truth for every financial agent: latest snapshots for the
    tickers mentioned (all covered tickers if none named) plus fresh headlines."""
    from common import market, news

    tickers = list({m.upper() for m in TICKER_RE.findall(text)}) or ["SPY", "QQQ", "IWM"]
    parts = []
    for t in tickers[:3]:
        snap = market.latest_snapshot(t)
        if snap:
            parts.append(
                f"{t}: spot {snap['spot']}, regime {snap['regime']}, gamma flip "
                f"{snap['gamma_flip']}, signal {snap['signal_score']}, ATM IV "
                f"{snap['atm_iv']}, as of {snap['captured_at']}")
    block = news.headlines_block(tickers[0])
    if block:
        parts.append(block)
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


def stream_chat(agent_id: str, text: str, session: str):
    """Yield UI events for one user message."""
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
