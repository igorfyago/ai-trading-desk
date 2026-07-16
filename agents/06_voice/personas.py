"""Voice agent personas (OpenAI Realtime API).

Three speech-to-speech agents, deliberately spanning the two big commercial
voice-agent use cases plus the desk's own specialty:

  riley   — AI Receptionist for a dental clinic (GENERALIST: any local business)
  quinn   — AI Quoting Agent for a renovation company (GENERALIST: any services firm)
  marcus  — AI Options Desk Agent: tells you the exact GEX-based trade
            (a deterministic rules engine in common/signals.py picks the trade;
             the model only narrates — the LLM never chooses strikes)

Each persona bundles instructions, a Realtime voice, JSON-schema tool
declarations (baked into the session at mint time), and the server-side
Python implementations. Audio runs browser↔OpenAI over WebRTC; every tool
call round-trips through our backend, so data and side effects stay here.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import market, signals
from common.db import get_connection

try:  # tool calls show up in LangSmith when LANGSMITH_TRACING is on
    from langsmith import traceable
except ImportError:  # pragma: no cover
    def traceable(**_kw):
        return lambda f: f


# ----------------------------------------------------- receptionist tools ----

SERVICES = {"cleaning": 30, "checkup": 30, "whitening": 60, "filling": 45, "consultation": 20}


def clinic_openings(day: str) -> str:
    """Deterministic fake calendar: same weekday → same slots."""
    seedmap = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}
    d = seedmap.get(day.lower().strip(), 0)
    slots = [f"{h}:{m:02d}" for i, (h, m) in enumerate(
        [(9, 0), (9, 30), (10, 15), (11, 0), (13, 30), (14, 15), (15, 0), (16, 30)]) if (i + d) % 3 != 0]
    return json.dumps({"day": day, "open_slots": slots, "services": SERVICES})


def book_appointment(patient_name: str, contact: str, service: str, slot: str) -> str:
    if service.lower() not in SERVICES:
        return json.dumps({"error": f"unknown service '{service}'", "services": list(SERVICES)})
    conn = get_connection()
    conn.execute(
        "INSERT INTO appointments (created_at, patient_name, contact, service, slot) VALUES (?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), patient_name, contact, service.lower(), slot),
    )
    conn.commit()
    conn.close()
    return json.dumps({"status": "booked", "patient": patient_name, "service": service, "slot": slot})


# ---------------------------------------------------------- quoting tools ----

RATES = {"kitchen": (95, 140), "bathroom": (110, 160), "painting": (3.5, 6.0),
         "flooring": (8, 14), "deck": (35, 55)}  # $/sqft ranges


def estimate_project(project_type: str, area_sqft: float, finish_level: str = "standard") -> str:
    rates = RATES.get(project_type.lower().strip())
    if not rates:
        return json.dumps({"error": f"unknown project '{project_type}'", "projects": list(RATES)})
    lo, hi = rates
    mult = {"budget": 0.8, "standard": 1.0, "premium": 1.45}.get(finish_level.lower(), 1.0)
    low, high = round(lo * area_sqft * mult, -2), round(hi * area_sqft * mult, -2)
    weeks = max(1, round(area_sqft / 120))
    return json.dumps({"project": project_type, "area_sqft": area_sqft, "finish": finish_level,
                       "estimate_low_usd": low, "estimate_high_usd": high,
                       "typical_duration_weeks": weeks})


def save_quote(customer: str, contact: str, project: str, low_usd: float, high_usd: float) -> str:
    conn = get_connection()
    conn.execute(
        "INSERT INTO quotes (created_at, customer, contact, project, low_usd, high_usd) VALUES (?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), customer, contact, project, low_usd, high_usd),
    )
    conn.commit()
    conn.close()
    return json.dumps({"status": "saved", "customer": customer, "project": project,
                       "range_usd": [low_usd, high_usd],
                       "note": "A project manager will follow up within one business day."})


# ----------------------------------------------------- options desk tools ----

def desk_status(ticker: str) -> str:
    snap = market.latest_snapshot(ticker)
    if not snap:
        return json.dumps({"error": f"No data for {ticker}. We cover SPY, QQQ, IWM."})
    return json.dumps({k: snap[k] for k in ("ticker", "spot", "regime", "traffic_light",
                                            "signal_score", "gamma_flip", "atm_iv", "captured_at")})


def trade_recommendation(ticker: str) -> str:
    return json.dumps(signals.recommend_trade(ticker))


def quote_option(ticker: str, strike: float, kind: str, dte_days: float) -> str:
    snap = market.latest_snapshot(ticker)
    if not snap:
        return json.dumps({"error": f"No data for {ticker}"})
    greeks = market.black_scholes(snap["spot"], strike, dte_days, snap["atm_iv"], kind)
    greeks.update({"ticker": ticker.upper(), "strike": strike, "kind": kind,
                   "spot_ref": snap["spot"], "iv_used": snap["atm_iv"]})
    return json.dumps(greeks)


def expected_move(ticker: str, dte_days: float) -> str:
    snap = market.latest_snapshot(ticker)
    if not snap:
        return json.dumps({"error": f"No data for {ticker}"})
    return json.dumps({"ticker": ticker.upper(), "spot": snap["spot"],
                       "one_sigma_move_usd": market.expected_move(snap["spot"], snap["atm_iv"], dte_days),
                       "horizon_days": dte_days})


# --------------------------------------------------------------- personas ----

def _fn(name, description, props, required):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}


VOICE_STYLE = (
    "HOW YOU SPEAK — this matters more than what you say. You sound like a sharp, "
    "experienced professional on a phone line: warm, quick, and COMPETENT. Humans "
    "sound human because they're at ease, not because they fumble.\n"
    "- YOU answer the phone: when the call connects, you speak first with a short, "
    "natural greeting in character, then let them talk.\n"
    "- React first, answer second: a tiny beat like 'oh sure —' or 'mm, okay so' "
    "before content. Keep it light; at most one small 'um' every several turns — "
    "you know your job cold, so speak like it.\n"
    "- Vary turn length like people do: sometimes one word ('Sure.'), sometimes three "
    "sentences. Never two same-shaped answers in a row. Default SHORT.\n"
    "- DRIVE the call. Never end a turn into dead air — end with the next helpful "
    "step: a question, an offer, a confirmation. Anticipate what they'll want next "
    "and offer it before they ask. One offer per turn, never pushy.\n"
    "- If interrupted, drop your sentence instantly and respond to the new thing — "
    "never resume the old sentence.\n"
    "- Before any tool call that takes time, say a short natural filler FIRST ('one "
    "sec, pulling that up') so there's never dead air.\n"
    "- Numbers as a person says them: 'about four eighty', 'six-oh-five', never "
    "'4.80 dollars' or decimal recitals. NEVER read JSON or field names aloud.\n"
    "- Contractions always; brief warmth (a small laugh, 'ha') where natural. Never "
    "announce you're an AI unless directly asked — then be honest and relaxed about it."
)

PERSONAS = {
    "riley": {
        "label": "Riley — AI Receptionist",
        "tagline": "Front desk for Northline Dental. Books real appointments.",
        "voice": "marin",
        "instructions": (
            "You are Riley, receptionist at Northline Dental, a neighborhood clinic. "
            + VOICE_STYLE +
            " Handle: opening hours (Mon-Fri 9-17), what services cost roughly, and "
            "APPOINTMENT BOOKING — your main job. Booking flow: ask for the service, "
            "offer 2-3 real slots from clinic_openings for their preferred day, then "
            "collect name and phone/email BEFORE calling book_appointment, then confirm "
            "everything back in one sentence. If someone describes pain or an emergency, "
            "be empathetic and offer the earliest slot. Never give medical advice — "
            "book a consultation instead. Keep replies under three sentences. "
            "Greeting when the call connects: 'Northline Dental, this is Riley!' "
            "Anticipate: after any booking, confirm they'll get a reminder and ask if "
            "there's anything else; if they ask about a service, offer to book it."
        ),
        "tools": [
            _fn("clinic_openings", "Open appointment slots for a weekday, plus the service list.",
                {"day": {"type": "string", "description": "weekday, e.g. Tuesday"}}, ["day"]),
            _fn("book_appointment", "Book an appointment (writes to the clinic calendar).",
                {"patient_name": {"type": "string"}, "contact": {"type": "string"},
                 "service": {"type": "string", "enum": list(SERVICES)},
                 "slot": {"type": "string", "description": "e.g. Tuesday 10:15"}},
                ["patient_name", "contact", "service", "slot"]),
        ],
        "implementations": {"clinic_openings": clinic_openings, "book_appointment": book_appointment},
    },
    "quinn": {
        "label": "Quinn — AI Quoting Agent",
        "tagline": "Instant renovation quotes for BrightBuild Co.",
        "voice": "sage",
        "instructions": (
            "You are Quinn, quoting specialist at BrightBuild Renovations. "
            + VOICE_STYLE +
            " Your job: turn a fuzzy project description into a concrete estimate. "
            "Flow: figure out project type (kitchen, bathroom, painting, flooring, deck), "
            "get the approximate size in square feet (help them estimate — 'a normal "
            "kitchen runs about 150'), and the finish level (budget / standard / premium). "
            "Call estimate_project, present the range conversationally ('you're looking "
            "at somewhere between eighteen and twenty-six thousand'), mention duration. "
            "If they want it in writing, collect name + contact and call save_quote. "
            "Estimates are ballpark until a site visit — always say so once. "
            "Greeting when the call connects: 'BrightBuild, Quinn speaking — what are "
            "we building?' Anticipate: after every estimate, offer to save the quote "
            "and set up the free site visit before they have to ask."
        ),
        "tools": [
            _fn("estimate_project", "Price range and duration for a renovation project.",
                {"project_type": {"type": "string", "enum": list(RATES)},
                 "area_sqft": {"type": "number"},
                 "finish_level": {"type": "string", "enum": ["budget", "standard", "premium"]}},
                ["project_type", "area_sqft"]),
            _fn("save_quote", "Save the quote and schedule a follow-up.",
                {"customer": {"type": "string"}, "contact": {"type": "string"},
                 "project": {"type": "string"}, "low_usd": {"type": "number"},
                 "high_usd": {"type": "number"}},
                ["customer", "contact", "project", "low_usd", "high_usd"]),
        ],
        "implementations": {"estimate_project": estimate_project, "save_quote": save_quote},
    },
    "marcus": {
        "label": "Marcus — AI Options Desk",
        "tagline": "The exact GEX trade: structure, strikes, invalidation. By voice.",
        "voice": "cedar",
        "instructions": (
            "You are Marcus, senior strategist on an options desk that trades dealer "
            "positioning (GEX). " + VOICE_STYLE + " Trader's cadence: 'we're short-gamma "
            "tape, spot's under the flip — I want the six-oh-five puts against the wall'. "
            "When someone asks what to trade: call trade_recommendation and walk them "
            "through it in this order — the regime in one plain sentence, the exact "
            "structure with strikes and expiry, why (the rationale), where it's wrong "
            "(the invalidation), and sizing. The recommendation comes from a rules "
            "engine — never invent strikes or override it; if you disagree, say what "
            "you'd watch instead. Use desk_status for a quick read, quote_option to "
            "price individual legs, expected_move for context. ALWAYS end a "
            "recommendation by saying it's a demo on synthetic data, not financial "
            "advice. That sentence is mandatory. "
            "Greeting when the call connects: 'Desk. Marcus.' — then, if they're quiet, "
            "'what are we looking at today?' Anticipate: after any single-ticker read, "
            "offer the trade ('want the trade on that?'); after a quote, offer the "
            "expected move for context."
        ),
        "tools": [
            _fn("desk_status", "Current regime, signal and gamma flip for a ticker.",
                {"ticker": {"type": "string", "description": "SPY, QQQ or IWM"}}, ["ticker"]),
            _fn("trade_recommendation", "The exact rule-based trade for the current regime: "
                "structure, legs with strikes/expiry/prices, rationale, invalidation, sizing.",
                {"ticker": {"type": "string"}}, ["ticker"]),
            _fn("quote_option", "Indicative Black-Scholes price and greeks for one leg.",
                {"ticker": {"type": "string"}, "strike": {"type": "number"},
                 "kind": {"type": "string", "enum": ["call", "put"]},
                 "dte_days": {"type": "number"}}, ["ticker", "strike", "kind", "dte_days"]),
            _fn("expected_move", "One-sigma expected move in dollars over a horizon.",
                {"ticker": {"type": "string"}, "dte_days": {"type": "number"}},
                ["ticker", "dte_days"]),
        ],
        "implementations": {"desk_status": desk_status, "trade_recommendation": trade_recommendation,
                            "quote_option": quote_option, "expected_move": expected_move},
    },
}


@traceable(name="voice_tool", run_type="tool")
def run_tool(persona: str, name: str, arguments: dict) -> str:
    """Dispatch a Realtime function call to its server-side implementation."""
    impl = PERSONAS.get(persona, {}).get("implementations", {}).get(name)
    if impl is None:
        return json.dumps({"error": f"unknown tool {name} for persona {persona}"})
    try:
        return impl(**arguments)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
