"""Agent 1 — Market Brief (the simplest possible "agent").

Level: one LLM call, zero tools, zero loops. The only trick is STRUCTURED
OUTPUT: instead of free text we force the model into a Pydantic schema, so
downstream code (or a bigger agent) can consume the result programmatically.

Run:  python agents/01_market_brief/main.py "Is SPY pinned by dealers into Friday opex?"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pydantic import BaseModel, Field
from rich import print as rprint

from common.llm import get_model


class MarketQuery(BaseModel):
    """A trader's natural-language question, parsed into desk-usable fields."""

    tickers: list[str] = Field(description="Tickers mentioned or implied, uppercase")
    metrics: list[str] = Field(
        description="Quant concepts involved, e.g. GEX, gamma flip, IV, expected move, OI"
    )
    intent: str = Field(
        description="One of: positioning, pricing, risk, education, execution"
    )
    horizon: str = Field(description="Time horizon, e.g. intraday, weekly opex, monthly")
    restated_question: str = Field(description="The question restated precisely in desk jargon")
    answer: str = Field(description="A concise, direct answer (3-5 sentences)")
    confidence: float = Field(ge=0, le=1, description="How confident the answer is")


SYSTEM = """You are a sell-side derivatives strategist. Parse the trader's question
and answer it from first principles of dealer positioning (GEX/DEX mechanics,
gamma regimes, walls, charm/vanna flows). Be precise and quantitative where possible.
If the question needs live data you don't have, say what data you would check."""


def run(question: str) -> MarketQuery:
    model = get_model()
    structured = model.with_structured_output(MarketQuery)
    return structured.invoke(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    )


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "Is SPY pinned by dealers into Friday opex?"
    result = run(q)
    rprint(result)
