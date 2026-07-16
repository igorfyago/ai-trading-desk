"""Agent 4 — Market Research Graph (first LangGraph agent).

Level: explicit graph orchestration. Instead of one opaque tool loop, the
workflow is a StateGraph with distinct phases and TWO loops you can see:

  plan → [ researcher ⇄ tools ]  →  draft → reflect ──ok──→ final report
                 ↑  inner tool loop        │
                 └──── revise (outer reflection loop, max 2) ──┘

The inner loop is the familiar agent⇄tools cycle from level 2. The outer loop
is new: a critic node reads the draft, and a CONDITIONAL EDGE routes either
to END or back to research with the critique injected. Bounded self-revision.

Run:  python agents/04_research_graph/main.py "Compare dealer positioning on SPY vs QQQ right now and what it implies for tomorrow"
"""

import math
import operator
import sys
from pathlib import Path
from typing import Annotated, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field
from rich.console import Console
from typing_extensions import TypedDict

from common import db, market
from common.db import get_connection
from common.llm import get_model

load_dotenv()
console = Console()
MAX_REVISIONS = 2

# ---------------------------------------------------------------- tools ----

@tool
def positioning_snapshot(ticker: str) -> str:
    """Latest dealer-positioning snapshot for a ticker: spot, gamma regime,
    net/abs GEX, gamma flip level, IV, VIX, signal score."""
    snap = market.latest_snapshot(ticker)
    return str(snap) if snap else f"No data for {ticker}. Known tickers: SPY, QQQ, IWM."


@tool
def wall_map(ticker: str) -> str:
    """Current strongest call/put walls for a ticker: the strikes dealers defend."""
    from common import signals

    walls = signals._latest_walls(ticker)
    if not walls:
        return f"No walls recorded for {ticker}."
    return "\n".join(f"{kind} wall: strike {w['strike']} (strength {w['strength']:,.0f})"
                     for kind, w in walls.items())


@tool
def market_news(ticker: str) -> str:
    """Latest headlines for a ticker — catalysts, earnings, macro context."""
    from common import news

    return news.headlines_block(ticker) or f"No recent headlines found for {ticker}."


@tool
def sql_query(sql: str) -> str:
    """Run a read-only SELECT against the positioning DB for anything the
    other tools don't cover. Demo DB tables: snapshots (captured_at, ticker,
    expiry, spot, regime, net_gex_total, abs_gex_total, gamma_flip,
    net_dex_total, atm_iv, vix, signal_score, traffic_light), strike_levels,
    walls. In production the DB is instead one wide table: gex_dex_snapshots
    (timestamp, ticker, expiry, spot, regime, net_gex_total, abs_gex_total,
    gamma_flip, atm_iv, vix_current, signal_score, traffic_light, ... plus
    JSONB columns call_walls, put_walls, gex_per_strike). If a table doesn't
    exist, you're on the other schema — adapt and retry."""
    if not sql.strip().lower().startswith(("select", "with")):
        return "REJECTED: SELECT only."
    try:
        rows = db.run_readonly(sql)[:40]
        return "\n".join(str(r) for r in rows) or "0 rows"
    except Exception as exc:
        return f"SQL ERROR: {exc}"


@tool
def calculator(expression: str) -> str:
    """Evaluate a math expression (supports sqrt, log, exp, abs, round).
    Use for expected-move math, ratios, annualization — never do arithmetic in your head."""
    allowed = {"sqrt": math.sqrt, "log": math.log, "exp": math.exp, "abs": abs,
               "round": round, "min": min, "max": max, "pi": math.pi}
    try:
        return str(eval(expression, {"__builtins__": {}}, allowed))  # noqa: S307 - namespace is locked down
    except Exception as exc:
        return f"MATH ERROR: {exc}"


TOOLS = [positioning_snapshot, wall_map, market_news, sql_query, calculator]

# ---------------------------------------------------------------- state ----

class ResearchState(TypedDict):
    question: str
    plan: str
    messages: Annotated[list, operator.add]   # inner tool-loop transcript
    draft: str
    critique: str
    revisions: int
    report: str


class Verdict(BaseModel):
    sufficient: bool = Field(description="Does the draft fully answer the question with numbers?")
    critique: str = Field(description="If not sufficient: what is missing or unsupported")


# ---------------------------------------------------------------- nodes ----

model = get_model()
research_model = model.bind_tools(TOOLS)


def plan_node(state: ResearchState) -> dict:
    plan = model.invoke([
        SystemMessage("You are a research lead on an options desk. Write a numbered "
                      "3-5 step data-gathering plan for the question. Steps must map to "
                      "available data: positioning snapshots, wall maps, SQL on the "
                      "snapshots DB, and a calculator. No prose beyond the steps."),
        HumanMessage(state["question"]),
    ]).content
    return {"plan": plan, "messages": [
        SystemMessage("You are a quant researcher. Execute the plan step by step using "
                      "tools. Gather ALL numbers before concluding. Plan:\n" + plan),
        HumanMessage(state["question"]),
    ]}


def researcher(state: ResearchState) -> dict:
    return {"messages": [research_model.invoke(state["messages"])]}


def route_research(state: ResearchState) -> Literal["tools", "draft"]:
    return "tools" if state["messages"][-1].tool_calls else "draft"


def draft_node(state: ResearchState) -> dict:
    draft = model.invoke(state["messages"] + [
        HumanMessage("Write the research answer now: direct thesis first, then the "
                     "supporting numbers you gathered, then risks/caveats. Cite figures "
                     "explicitly — no vague claims.")
    ]).content
    return {"draft": draft}


def reflect_node(state: ResearchState) -> dict:
    verdict = model.with_structured_output(Verdict).invoke([
        SystemMessage("You are a skeptical desk head reviewing a junior's research note. "
                      "It is sufficient only if every claim is backed by a retrieved number."),
        HumanMessage(f"Question: {state['question']}\n\nDraft:\n{state['draft']}"),
    ])
    return {"critique": "" if verdict.sufficient else verdict.critique,
            "revisions": state["revisions"] + 1}


def route_reflection(state: ResearchState) -> Literal["revise", "finalize"]:
    if state["critique"] and state["revisions"] <= MAX_REVISIONS:
        return "revise"
    return "finalize"


def revise_node(state: ResearchState) -> dict:
    # feed the critique back into the tool loop so the researcher fills the gaps
    return {"messages": [HumanMessage(
        "Reviewer rejected the draft. Address this critique — gather any missing "
        f"data with tools before re-answering:\n{state['critique']}")]}


def finalize(state: ResearchState) -> dict:
    return {"report": state["draft"]}


# ---------------------------------------------------------------- graph ----

def build_graph():
    return (
        StateGraph(ResearchState)
        .add_node("plan", plan_node)
        .add_node("researcher", researcher)
        .add_node("tools", ToolNode(TOOLS, handle_tool_errors=True))
        .add_node("draft", draft_node)
        .add_node("reflect", reflect_node)
        .add_node("revise", revise_node)
        .add_node("finalize", finalize)
        .add_edge(START, "plan")
        .add_edge("plan", "researcher")
        .add_conditional_edges("researcher", route_research, ["tools", "draft"])
        .add_edge("tools", "researcher")                       # inner loop
        .add_edge("draft", "reflect")
        .add_conditional_edges("reflect", route_reflection, {"revise": "revise", "finalize": "finalize"})
        .add_edge("revise", "researcher")                      # outer loop
        .add_edge("finalize", END)
        .compile()
    )


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or (
        "Compare dealer positioning on SPY vs QQQ right now and what it implies for tomorrow"
    )
    graph = build_graph()
    final = None
    for event in graph.stream(
        {"question": question, "messages": [], "revisions": 0},
        config={"recursion_limit": 60, "run_name": "cli:research-graph", "tags": ["cli", "research"]},
        stream_mode="updates",
    ):
        for node, update in event.items():
            console.print(f"[dim]* node: {node}[/dim]")
            final = update
    console.print("\n[bold]Research report:[/bold]\n")
    console.print(final.get("report", "(no report produced)"))
