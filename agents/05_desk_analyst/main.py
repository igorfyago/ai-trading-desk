"""Agent 5 — GEX Trading-Desk Analyst (the flagship text agent).

Level: everything at once — a multi-path LangGraph with:
  • a deterministic data node (fetch)                       — no LLM
  • a REGIME ROUTER: different playbook branch per gamma regime
  • PARALLEL FAN-OUT: three specialist sub-agents (positioning, flow, risk)
    run concurrently, each with its own toolbelt, joined before synthesis
  • a SELF-CRITIQUE LOOP: a risk-manager critic can bounce the memo back
  • HUMAN-IN-THE-LOOP: interrupt() pauses the graph before publishing;
    the human approves / requests changes / rejects, and the graph resumes
  • a CHECKPOINTER: state survives the pause (and would survive a crash)

Run:  python agents/05_desk_analyst/main.py SPY
"""

import json
import operator
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field
from rich.console import Console
from typing_extensions import TypedDict

from common import market
from common.db import get_connection
from common.llm import get_model

load_dotenv()
console = Console()
MAX_CRITIQUE_ROUNDS = 2
OUT_DIR = Path(__file__).resolve().parent / "memos"

# ------------------------------------------------------------- toolbelt ----

@tool
def sql_query(sql: str) -> str:
    """Read-only SELECT on the positioning DB. Tables: snapshots(captured_at,
    ticker, expiry, spot, regime, net_gex_total, abs_gex_total, gamma_flip,
    net_dex_total, atm_iv, vix, signal_score, traffic_light),
    strike_levels(snapshot_id, strike, gex, dex, call_oi, put_oi),
    walls(snapshot_id, kind, strike, strength)."""
    if not sql.strip().lower().startswith(("select", "with")):
        return "REJECTED: SELECT only."
    conn = get_connection()
    try:
        rows = conn.execute(sql).fetchall()[:40]
        return "\n".join(str(r) for r in rows) or "0 rows"
    except Exception as exc:
        return f"SQL ERROR: {exc}"
    finally:
        conn.close()


@tool
def option_quote(spot: float, strike: float, dte_days: float, iv: float, kind: str) -> str:
    """Black-Scholes price and greeks for one option leg.

    Args:
        spot: underlying price
        strike: option strike
        dte_days: days to expiry
        iv: implied volatility as a decimal (0.18 = 18 vol)
        kind: 'call' or 'put'
    """
    return str(market.black_scholes(spot, strike, dte_days, iv, kind))


@tool
def sigma_move(spot: float, iv: float, dte_days: float) -> str:
    """One-sigma expected move in dollars for the given spot, IV and horizon."""
    return str(market.expected_move(spot, iv, dte_days))


# ----------------------------------------------------------------- state ----

class DeskState(TypedDict):
    ticker: str
    snapshot: dict
    profile: list
    playbook: str
    analyses: Annotated[list[str], operator.add]   # parallel branches append here
    memo: dict
    critique: str
    rounds: int
    published_path: str


class Memo(BaseModel):
    bias: Literal["bullish", "bearish", "neutral", "two-sided"]
    conviction: int = Field(ge=1, le=10)
    thesis: str = Field(description="2-3 sentence core view grounded in the data")
    trade_idea: str = Field(description="One concrete options structure with strikes/expiry")
    key_levels: list[str] = Field(description="Levels that matter and why (flip, walls, ±1σ)")
    invalidation: str = Field(description="What would prove the thesis wrong")
    risks: list[str]


# ----------------------------------------------------------------- nodes ----

model = get_model()


def fetch(state: DeskState) -> Command[Literal["long_gamma_playbook", "short_gamma_playbook", "__end__"]]:
    """Deterministic data node + router: no LLM involved."""
    snap = market.latest_snapshot(state["ticker"])
    if snap is None:
        console.print(f"[red]No data for {state['ticker']}[/red]")
        return Command(update={}, goto=END)
    profile = market.gex_profile(state["ticker"])
    goto = "long_gamma_playbook" if snap["regime"] == "positive_gamma" else "short_gamma_playbook"
    return Command(update={"snapshot": snap, "profile": profile}, goto=goto)


def long_gamma_playbook(state: DeskState) -> dict:
    return {"playbook": (
        "REGIME: dealers LONG gamma — hedging dampens moves. Default playbook: "
        "mean-reversion, range trades between walls, premium selling; pinning "
        "risk into expiry; breakouts need a catalyst to break the walls."
    )}


def short_gamma_playbook(state: DeskState) -> dict:
    return {"playbook": (
        "REGIME: dealers SHORT gamma — hedging amplifies moves. Default playbook: "
        "momentum/breakout bias, long premium, wider stops; watch the flip level, "
        "crossing it accelerates; avoid naked short options."
    )}


def _specialist(name: str, focus: str, tools: list) -> callable:
    """Factory: each specialist is a full sub-agent (its own tool loop) run as a node."""
    sub_agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=(
            f"You are the {name} specialist on an options desk. {focus} "
            "Use your tools for any number you don't have. Be quantitative and terse: "
            "5-8 bullet points, every bullet backed by a figure."
        ),
    )

    def node(state: DeskState) -> dict:
        result = sub_agent.invoke(
            {"messages": [{"role": "user", "content": (
                f"Ticker: {state['ticker']}\nSnapshot: {state['snapshot']}\n"
                f"Per-strike GEX/DEX profile: {state['profile']}\n{state['playbook']}"
            )}]},
            config={"recursion_limit": 15},
        )
        return {"analyses": [f"## {name} analysis\n{result['messages'][-1].content}"]}

    return node


positioning_analyst = _specialist(
    "POSITIONING", "Read the GEX/DEX profile: regime strength, distance to gamma flip, "
    "concentration vs history (query the DB for context).", [sql_query, sigma_move])
flow_analyst = _specialist(
    "FLOW & LEVELS", "Map the battlefield: call/put walls, OI concentration, expected-move "
    "band vs wall spacing — where is spot magnetized or repelled?", [sql_query, sigma_move])
risk_analyst = _specialist(
    "RISK", "Price the trade: what do candidate structures cost, what are the greeks, "
    "what kills the trade? Stress the thesis.", [option_quote, sigma_move, sql_query])


def synthesize(state: DeskState) -> dict:
    critique_note = (
        f"\n\nA previous memo was rejected by the risk manager for:\n{state['critique']}\n"
        "Fix exactly that." if state.get("critique") else ""
    )
    memo = model.with_structured_output(Memo).invoke([
        {"role": "system", "content":
            "You are the head of desk. Merge the specialists' work into one signal memo. "
            "Resolve disagreements explicitly — do not average them away."},
        {"role": "user", "content":
            f"Ticker: {state['ticker']}\nSnapshot: {state['snapshot']}\n\n"
            + "\n\n".join(state["analyses"][-3:]) + critique_note},
    ])
    return {"memo": memo.model_dump()}


def risk_review(state: DeskState) -> dict:
    class Review(BaseModel):
        approved: bool
        critique: str = Field(description="If rejected: the specific inconsistency or unsupported claim")

    review = model.with_structured_output(Review).invoke([
        {"role": "system", "content":
            "You are a hard-nosed risk manager. Reject the memo if the trade idea "
            "contradicts the regime playbook, ignores a wall/flip level, or states a "
            "figure not present in the analyses."},
        {"role": "user", "content":
            f"Playbook: {state['playbook']}\n\nAnalyses:\n" + "\n".join(state["analyses"][-3:])
            + f"\n\nMemo:\n{json.dumps(state['memo'], indent=1)}"},
    ])
    return {"critique": "" if review.approved else review.critique,
            "rounds": state["rounds"] + 1}


def route_review(state: DeskState) -> Literal["synthesize", "human_approval"]:
    if state["critique"] and state["rounds"] <= MAX_CRITIQUE_ROUNDS:
        return "synthesize"
    return "human_approval"


def human_approval(state: DeskState) -> Command[Literal["publish", "synthesize", "__end__"]]:
    """Pause the graph and surface the memo to a human. interrupt() must be
    first — everything before it re-runs on resume."""
    decision = interrupt({
        "memo": state["memo"],
        "instructions": "Reply with {'action': 'approve' | 'revise' | 'reject', 'notes': '...'}",
    })
    action = decision.get("action", "reject")
    if action == "approve":
        return Command(update={}, goto="publish")
    if action == "revise":
        return Command(update={"critique": decision.get("notes", "Human requested changes"),
                               "rounds": 0}, goto="synthesize")
    return Command(update={}, goto=END)


def publish(state: DeskState) -> dict:
    """Side effect AFTER the interrupt: runs exactly once."""
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUT_DIR / f"{state['ticker']}-{stamp}.json"
    path.write_text(json.dumps(state["memo"], indent=2))
    return {"published_path": str(path)}


# ----------------------------------------------------------------- graph ----

def build_graph(checkpointer=None):
    builder = (
        StateGraph(DeskState)
        .add_node("fetch", fetch)
        .add_node("long_gamma_playbook", long_gamma_playbook)
        .add_node("short_gamma_playbook", short_gamma_playbook)
        .add_node("positioning", positioning_analyst)
        .add_node("flow", flow_analyst)
        .add_node("risk", risk_analyst)
        .add_node("synthesize", synthesize)
        .add_node("risk_review", risk_review)
        .add_node("human_approval", human_approval)
        .add_node("publish", publish)
        .add_edge(START, "fetch")
    )
    # both playbook branches fan out to the three specialists in parallel
    for playbook in ("long_gamma_playbook", "short_gamma_playbook"):
        for analyst in ("positioning", "flow", "risk"):
            builder.add_edge(playbook, analyst)
    return (
        builder
        .add_edge(["positioning", "flow", "risk"], "synthesize")   # join: wait for all three
        .add_edge("synthesize", "risk_review")
        .add_conditional_edges("risk_review", route_review, ["synthesize", "human_approval"])
        .add_edge("publish", END)
        .compile(checkpointer=checkpointer or InMemorySaver())
    )


if __name__ == "__main__":
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    graph = build_graph()
    config = {"configurable": {"thread_id": f"desk-{ticker}"}, "recursion_limit": 80,
              "run_name": "cli:desk-analyst", "tags": ["cli", "analyst"]}

    result = graph.invoke({"ticker": ticker, "analyses": [], "rounds": 0}, config)

    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        console.print("\n[bold yellow]— HUMAN APPROVAL REQUIRED —[/bold yellow]")
        console.print(json.dumps(payload["memo"], indent=2))
        action = console.input("\n[bold]approve / revise / reject?[/bold] ").strip().lower() or "approve"
        notes = console.input("notes (optional): ") if action == "revise" else ""
        result = graph.invoke(Command(resume={"action": action, "notes": notes}), config)

    if result.get("published_path"):
        console.print(f"\n[green]Memo published → {result['published_path']}[/green]")
    else:
        console.print("\n[red]Memo not published.[/red]")
