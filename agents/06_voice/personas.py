"""Agent 6 — Voice agent definitions (OpenAI Realtime API).

Two speech-to-speech agents share one architecture; only this file differs
between them. Each persona bundles:
  • instructions  — the voice-tuned system prompt
  • voice         — Realtime voice ("marin"/"cedar" are the most natural)
  • tools         — JSON-schema function declarations sent in the session config
  • implementations — the Python functions that actually run, SERVER-SIDE

The split matters: the model and audio run in the browser via WebRTC for
latency, but every tool call round-trips through our backend (web/server.py),
so data access and side effects stay on the server where they belong.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import market
from common.db import get_connection

# ------------------------------------------------------------ tool impls ----

def desk_status(ticker: str) -> str:
    snap = market.latest_snapshot(ticker)
    if not snap:
        return json.dumps({"error": f"No data for {ticker}. We cover SPY, QQQ, IWM."})
    return json.dumps({
        "ticker": snap["ticker"], "spot": snap["spot"], "regime": snap["regime"],
        "traffic_light": snap["traffic_light"], "signal_score": snap["signal_score"],
        "gamma_flip": snap["gamma_flip"], "as_of": snap["captured_at"],
    })


def book_callback(caller_name: str, contact: str, topic: str, preferred_time: str = "") -> str:
    conn = get_connection()
    conn.execute(
        "INSERT INTO callbacks (created_at, caller_name, contact, topic, preferred_time)"
        " VALUES (?,?,?,?,?)",
        (datetime.utcnow().isoformat(), caller_name, contact, topic, preferred_time),
    )
    conn.commit()
    conn.close()
    return json.dumps({"status": "booked", "for": caller_name, "topic": topic,
                       "preferred_time": preferred_time or "first available"})


def market_context(ticker: str) -> str:
    snap = market.latest_snapshot(ticker)
    if not snap:
        return json.dumps({"error": f"No data for {ticker}. We quote SPY, QQQ, IWM."})
    return json.dumps({
        "ticker": snap["ticker"], "spot": snap["spot"], "atm_iv": snap["atm_iv"],
        "nearest_expiry": snap["expiry"], "regime": snap["regime"], "vix": snap["vix"],
    })


def quote_option(ticker: str, strike: float, kind: str, dte_days: float) -> str:
    snap = market.latest_snapshot(ticker)
    if not snap:
        return json.dumps({"error": f"No data for {ticker}"})
    greeks = market.black_scholes(snap["spot"], strike, dte_days, snap["atm_iv"], kind)
    greeks.update({"ticker": ticker.upper(), "strike": strike, "kind": kind,
                   "dte_days": dte_days, "spot_ref": snap["spot"], "iv_used": snap["atm_iv"],
                   "disclaimer": "indicative, ATM vol, no skew"})
    return json.dumps(greeks)


def expected_move(ticker: str, dte_days: float) -> str:
    snap = market.latest_snapshot(ticker)
    if not snap:
        return json.dumps({"error": f"No data for {ticker}"})
    move = market.expected_move(snap["spot"], snap["atm_iv"], dte_days)
    return json.dumps({"ticker": ticker.upper(), "spot": snap["spot"],
                       "one_sigma_move_usd": move, "horizon_days": dte_days})


# --------------------------------------------------------------- personas ----

def _fn(name, description, props, required):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}


PERSONAS = {
    "receptionist": {
        "label": "AI Receptionist",
        "voice": "marin",
        "instructions": (
            "You are Riley, the front-desk receptionist at Yago Capital, an options "
            "analytics desk. You are warm, quick, and human: short sentences, natural "
            "fillers, never robotic, never read JSON aloud. Greet callers, answer what "
            "the desk does (dealer GEX/DEX positioning analytics on SPY, QQQ, IWM), "
            "give the current desk read on a ticker when asked (use desk_status and "
            "translate it to plain words like 'we're in a calm, long-gamma tape'), and "
            "book callbacks with the analyst team — always collect name, contact, and "
            "topic before calling book_callback, and confirm it back. If asked for "
            "financial advice, politely decline and offer a callback instead. Keep "
            "every reply under three sentences unless asked for more."
        ),
        "tools": [
            _fn("desk_status", "Current desk read on a ticker: regime, signal, gamma flip.",
                {"ticker": {"type": "string", "description": "SPY, QQQ or IWM"}}, ["ticker"]),
            _fn("book_callback", "Book a callback with the analyst team.",
                {"caller_name": {"type": "string"}, "contact": {"type": "string",
                 "description": "phone or email"}, "topic": {"type": "string"},
                 "preferred_time": {"type": "string"}}, ["caller_name", "contact", "topic"]),
        ],
        "implementations": {"desk_status": desk_status, "book_callback": book_callback},
    },
    "quoting": {
        "label": "AI Quoting Agent",
        "voice": "cedar",
        "instructions": (
            "You are Marcus, a senior options quote clerk at Yago Capital. Fast, precise, "
            "trader's cadence — you say 'the 620 calls, five days out, are going about "
            "four eighty, forty-two delta', never read raw JSON. Workflow: get the caller's "
            "ticker, strike, call/put and horizon; call market_context first if you need "
            "spot or IV; then quote_option; round prices to five cents in speech. Offer the "
            "one-sigma expected move for context when relevant. Always state quotes are "
            "indicative and demo data, not an offer to trade. If asked whether to do the "
            "trade, give the mechanics (breakeven, what has to happen) but not advice. "
            "Confirm numbers by repeating them back before quoting."
        ),
        "tools": [
            _fn("market_context", "Spot, ATM IV, nearest expiry and regime for a ticker.",
                {"ticker": {"type": "string"}}, ["ticker"]),
            _fn("quote_option", "Indicative Black-Scholes price and greeks for one option leg.",
                {"ticker": {"type": "string"}, "strike": {"type": "number"},
                 "kind": {"type": "string", "enum": ["call", "put"]},
                 "dte_days": {"type": "number", "description": "days to expiry"}},
                ["ticker", "strike", "kind", "dte_days"]),
            _fn("expected_move", "One-sigma expected move in dollars over a horizon.",
                {"ticker": {"type": "string"}, "dte_days": {"type": "number"}},
                ["ticker", "dte_days"]),
        ],
        "implementations": {"market_context": market_context, "quote_option": quote_option,
                            "expected_move": expected_move},
    },
}


def run_tool(persona: str, name: str, arguments: dict) -> str:
    """Dispatch a Realtime function call to its server-side implementation."""
    impl = PERSONAS.get(persona, {}).get("implementations", {}).get(name)
    if impl is None:
        return json.dumps({"error": f"unknown tool {name} for persona {persona}"})
    try:
        return impl(**arguments)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
