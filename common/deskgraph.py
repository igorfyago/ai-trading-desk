"""The desk brain as a LangGraph graph: structure -> tape -> decide -> order.

WHAT THIS IS. The deterministic pipeline that answers "what's the trade",
expressed as a real LangGraph ``StateGraph`` instead of one opaque function
call. The nodes wrap the SAME seams the engine has always had - market
structure, the tape read, the rules verdict, the spoken order - so the
decision is byte-identical to calling ``signals.recommend_trade`` directly.
What changes is that every live voice call now produces a RUN: node-by-node,
timed, traced to LangSmith when LANGSMITH_TRACING is on (the runtime emits
real node spans), and shipped to the observatory (obs.b4rruf3t.com) where
the DAG can be watched lighting up.

WHAT THIS IS NOT. There is no LLM in this graph, on purpose, and that is the
design rather than a shortcut. The house split is decide=code, explain=model
(see signals.py): the desk's edge came from a pre-registered backtest of
RULES, and docs/REASONING_LAYER.md sets the bar any reasoning model must
clear before it may touch a decision (+EV on the same blindfolded harness).
LangGraph here is what it is in serious production systems: the
orchestration and observability runtime, not a licence to improvise.

Failure posture: the graph is a WINDOW onto the engine, never a wall in
front of it. If langgraph is missing or a node explodes, ``run()`` falls
back to the plain engine call and the voice caller never knows.
"""
from __future__ import annotations

import operator
import os
import threading
import time
from datetime import datetime, timezone
from typing import Annotated, TypedDict

from common import market, signals


class DeskState(TypedDict, total=False):
    """What flows down the graph. Each node fills its slice and appends one
    trace entry; the ``operator.add`` reducer is the standard LangGraph way
    to accumulate a channel instead of overwriting it."""
    ticker: str
    session: str
    trace: Annotated[list[dict], operator.add]
    # structure
    regime: str | None
    signal_score: float | None
    gamma_flip: float | None
    walls: dict
    atm_iv: float | None
    # tape
    stance: str | None
    stage: str | None
    bands_ok: bool | None
    # decide
    rec: dict
    verdict: str | None
    armed: bool
    # order
    order: str


def _traced(name: str):
    """Node decorator: the node times its own body and reports one line of
    what it saw. Pure-python nodes, so self-timing is exact."""
    def wrap(fn):
        def node(state: DeskState) -> dict:
            t0 = time.perf_counter()
            out, note = fn(state)
            out["trace"] = [{"node": name,
                             "ms": round((time.perf_counter() - t0) * 1000),
                             "note": str(note)[:300]}]
            return out
        node.__name__ = name
        return node
    return wrap


@_traced("structure")
def _structure(state: DeskState):
    """Dealer positioning: the slow layer. Regime, score, flip, walls, IV."""
    snap = market.latest_snapshot(state["ticker"]) or {}
    walls = {}
    try:
        walls = signals._latest_walls(market.resolve_feed(state["ticker"])[0])
    except Exception:
        pass
    flip = snap.get("gamma_flip")
    note = (f"{snap.get('regime', 'no snapshot')}, score {snap.get('signal_score')}, "
            + (f"flip {flip}" if flip is not None else "no real flip - walls carry the thesis"))
    return {"regime": snap.get("regime"), "signal_score": snap.get("signal_score"),
            "gamma_flip": flip, "walls": walls, "atm_iv": snap.get("atm_iv")}, note


@_traced("tape")
def _tape(state: DeskState):
    """The fast layer: what the last 240 bars actually did."""
    from common import tape as tape_mod
    read = None
    try:
        read = tape_mod.get_tape_read(market.resolve_feed(state["ticker"])[0])
    except Exception:
        pass
    if not read:
        return ({"stance": None, "stage": None, "bands_ok": None},
                "no tape (feed off or empty)")
    act = read.get("action") or {}
    return ({"stance": act.get("stance"), "stage": read.get("stage"),
             "bands_ok": read.get("bands_ok")},
            act.get("do_now") or read.get("plain", "")[:160])


@_traced("decide")
def _decide(state: DeskState):
    """The rules verdict. THE authority node - everything else is context.

    Calls the engine whole rather than re-assembling it from the two nodes
    above: recommend_trade's internals (blend gates, ladder precedence,
    tape_armed) are the tested surface, and a re-implementation here would
    be a second engine waiting to disagree with the first."""
    rec = signals.recommend_trade(state["ticker"])
    if "error" in rec:
        return {"rec": rec, "verdict": None, "armed": False}, rec["error"]
    armed = signals.tape_armed(rec.get("tape"))
    verdict = (rec.get("confluence") or {}).get("verdict")
    return ({"rec": rec, "verdict": verdict, "armed": armed},
            f"{rec.get('bias')} · board {verdict} · "
            f"{'ARMED - fills' if armed else 'not armed - plan only'}")


@_traced("order")
def _order(state: DeskState):
    """The words the caller can act on, composed in code (copy_trade)."""
    rec = state.get("rec") or {}
    line = rec.get("copy_trade") or rec.get("error") or "no answer"
    return {"order": line}, line


# the DAG the observatory draws - kept in the module so the desk and the
# observatory can never disagree about the topology
SPEC = {
    "nodes": [
        {"id": "structure", "label": "structure", "hint": "GEX regime · flip · walls"},
        {"id": "tape", "label": "tape", "hint": "bands · stage · day shape"},
        {"id": "decide", "label": "decide", "hint": "rules verdict · armed?"},
        {"id": "order", "label": "order", "hint": "the spoken line"},
    ],
    "edges": [["structure", "tape"], ["tape", "decide"], ["decide", "order"]],
}

_compiled = None
_compile_failed = False


def build():
    """Compile the graph once. Raises only on first use, never at import."""
    global _compiled
    if _compiled is None:
        from langgraph.graph import END, START, StateGraph
        g = StateGraph(DeskState)
        g.add_node("structure", _structure)
        g.add_node("tape", _tape)
        g.add_node("decide", _decide)
        g.add_node("order", _order)
        g.add_edge(START, "structure")
        g.add_edge("structure", "tape")
        g.add_edge("tape", "decide")
        g.add_edge("decide", "order")
        g.add_edge("order", END)
        _compiled = g.compile()
    return _compiled


def run(ticker: str, session: str = "voice") -> dict:
    """One decision through the compiled graph; the engine's payload comes
    back unchanged. The whole run is shipped to the observatory in a daemon
    thread - a POST that can never cost the voice caller a millisecond."""
    global _compile_failed
    if _compile_failed:
        return signals.recommend_trade(ticker)
    try:
        compiled = build()
    except Exception:
        _compile_failed = True            # langgraph absent: the engine stands alone
        return signals.recommend_trade(ticker)

    t0 = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        state = compiled.invoke({"ticker": ticker.upper(), "session": session,
                                 "trace": []})
        rec = state.get("rec") or {"error": "graph produced no decision"}
        nodes = state.get("trace") or []
    except Exception as exc:              # a broken node must never mute Marcus
        rec = signals.recommend_trade(ticker)
        state = {}
        nodes = [{"node": "fallback", "ms": 0, "note": f"{type(exc).__name__}: {exc}"}]

    _ship({"agent": "marcus", "started_at": started_at,
           "ticker": ticker.upper(), "session": session,
           "latency_ms": round((time.perf_counter() - t0) * 1000),
           "outcome": "error" if "error" in rec else "ok",
           "verdict": state.get("verdict"), "armed": bool(state.get("armed")),
           "order": (state.get("order") or rec.get("copy_trade") or "")[:400],
           "nodes": nodes, "spec": SPEC})
    return rec


def _ship(run_record: dict) -> None:
    """Fire-and-forget the run to the observatory. Silence on every failure:
    observability is a bonus and the call is live."""
    url = os.getenv("OBS_INGEST_URL", "").strip()
    token = os.getenv("OBS_INGEST_TOKEN", "").strip()
    if not url:
        return

    def _post():
        try:
            import httpx
            httpx.post(url, json=run_record, timeout=4.0,
                       headers={"X-Desk-Token": token} if token else {})
        except Exception:
            pass

    threading.Thread(target=_post, daemon=True).start()


def graph_meta() -> dict:
    """The topology plus liveness, for the desk's own /api and the tests."""
    ok = True
    try:
        build()
    except Exception:
        ok = False
    return {"spec": SPEC, "langgraph": ok,
            "ships_to": os.getenv("OBS_INGEST_URL") or None}
