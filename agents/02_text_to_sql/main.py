"""Agent 2 — Text-to-SQL over the options-flow database.

Level: first real AGENT — a tool loop. The model sees the schema, writes SQL,
runs it, reads the result (or the error), and iterates until it can answer.
Failed SQL is not a crash: the error text goes back to the model as a tool
result and it self-corrects. That feedback loop is the core agent idea.

By default this queries the bundled SQLite mirror of the
options-flow-analytics `gex_dex_snapshots` schema (auto-seeded, zero setup).
Set DATABASE_URL to point it at the live Postgres instead.

Run:  python agents/02_text_to_sql/main.py "Which ticker had the most negative-gamma snapshots, and what was its average VIX in that regime?"
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.tools import tool
from rich.console import Console

from common.db import describe_schema, get_connection
from common.llm import get_model

load_dotenv()
console = Console()

FORBIDDEN = ("insert", "update", "delete", "drop", "alter", "create", "attach", "pragma", "vacuum")
MAX_ROWS = 50


def _run_query(sql: str) -> list[tuple]:
    """Execute against Postgres if DATABASE_URL is set, else the demo SQLite."""
    url = os.getenv("DATABASE_URL")
    if url:
        import psycopg

        with psycopg.connect(url) as conn:
            return conn.execute(sql).fetchall()
    conn = get_connection()
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@tool
def get_schema() -> str:
    """Return the CREATE TABLE statements for every table in the options-flow
    database. Always call this before writing any SQL."""
    return describe_schema()


@tool
def run_sql(sql: str) -> str:
    """Run one read-only SQL SELECT and return the rows as text.

    Rules: SELECT-only (no DML/DDL), single statement. If the query errors,
    the error message is returned so you can fix the SQL and retry.

    Args:
        sql: A single SELECT statement. Add LIMIT yourself for big scans.
    """
    lowered = sql.strip().lower()
    if not lowered.startswith(("select", "with")):
        return "REJECTED: only SELECT queries are allowed."
    if any(f" {word} " in f" {lowered} " or lowered.startswith(word) for word in FORBIDDEN):
        return "REJECTED: query contains a forbidden keyword (read-only access)."
    if ";" in sql.strip().rstrip(";"):
        return "REJECTED: one statement at a time."
    try:
        rows = _run_query(sql)
    except Exception as exc:  # error goes back to the model so it can self-correct
        return f"SQL ERROR: {exc}"
    if not rows:
        return "OK: query ran, 0 rows."
    shown = rows[:MAX_ROWS]
    body = "\n".join(str(r) for r in shown)
    suffix = f"\n... ({len(rows) - MAX_ROWS} more rows truncated)" if len(rows) > MAX_ROWS else ""
    return f"OK ({len(rows)} rows):\n{body}{suffix}"


SYSTEM = """You are a quant data analyst for an options trading desk.
Answer questions by querying the dealer-positioning database (GEX/DEX snapshots).

Method — follow it strictly:
1. Call get_schema first. Never guess column names.
2. Write ONE focused SELECT at a time; prefer aggregates over dumping rows.
3. If a query errors, read the error and fix your SQL — do not apologize, just retry.
4. When you have the numbers, answer in plain trader language and INCLUDE the final
   SQL you used so the human can verify it.

Notes: net_gex_total > 0 means dealers are long gamma (vol-dampening regime);
negative means short gamma (vol-amplifying). Timestamps are ISO-8601 UTC strings."""


def build_agent(checkpointer=None):
    return create_agent(model=get_model(), tools=[get_schema, run_sql],
                        system_prompt=SYSTEM, checkpointer=checkpointer)


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or (
        "Which ticker had the most negative-gamma snapshots, "
        "and what was its average VIX in that regime?"
    )
    agent = build_agent()
    result = agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"recursion_limit": 25},
    )
    for msg in result["messages"]:
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                arg = tc["args"].get("sql", "")
                console.print(f"[dim]→ {tc['name']}[/dim] [cyan]{arg[:120]}[/cyan]")
    console.print("\n[bold]Answer:[/bold]")
    console.print(result["messages"][-1].content)
