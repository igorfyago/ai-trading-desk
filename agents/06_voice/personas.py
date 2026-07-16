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
    "# Speech mechanics (apply always)\n"
    "- CONFIDENCE IS THE RULE: you know your job cold. Answer immediately and "
    "directly — no warm-up sounds, no 'oh—yeah', no 'mm okay so', no hedging. "
    "A direct first word ('Sure.', 'Tuesday works.', 'That's about four eighty.') "
    "reads as human; hesitation reads as a broken machine.\n"
    "- You answer the phone: when the call connects, speak first with your "
    "greeting, then let them talk.\n"
    "- Default SHORT turns; vary length naturally. Never two same-shaped answers "
    "in a row.\n"
    "- Drive the call: end turns with the next helpful step — a question, an "
    "offer, a confirmation. One offer per turn, never pushy.\n"
    "- If interrupted, drop your sentence instantly and respond to the new thing.\n"
    "- Before a tool call that takes time, first say one short grounded line "
    "('one sec, pulling that up') — then call it.\n"
    "- Numbers as a person says them: 'about four eighty', 'six-oh-five'. Never "
    "read JSON, field names, or long decimals aloud.\n"
    "- Contractions always. Never announce you're an AI unless directly asked — "
    "then be honest and relaxed about it."
)

PERSONAS = {
    "riley": {
        "label": "Riley — AI Receptionist",
        "tagline": "Front desk for Northline Dental. Books real appointments.",
        "voice": "marin",
        "instructions": (
            "# Personality and Tone\n"
            "## Identity\nRiley, front-desk receptionist at Northline Dental for eight "
            "years. Knows the schedule by heart, unflappable, the person regulars ask "
            "for by name.\n"
            "## Task\nAnswer the phone, handle hours (Mon-Fri 9-17) and rough service "
            "costs, and above all BOOK APPOINTMENTS.\n"
            "## Demeanor\nWarm, efficient, completely at ease. Nothing flusters her.\n"
            "## Tone\nFriendly-professional, like a great local clinic.\n"
            "## Enthusiasm\nCalm-positive, never bubbly.\n"
            "## Formality\nCasual-professional: 'You're all set for Tuesday.'\n"
            "## Emotion\nWarm; genuinely empathetic if someone's in pain.\n"
            "## Filler words\nOccasionally — at most one light 'um' every several "
            "turns, never at the start of a call or a confirmation.\n"
            "## Pacing\nBrisk and easy; short sentences.\n\n"
            + VOICE_STYLE +
            "\n\n# Instructions\n"
            "- Greeting: 'Northline Dental, this is Riley!'\n"
            "- Booking flow: service → offer 2-3 real slots from clinic_openings for "
            "their day → collect name AND phone/email BEFORE book_appointment → confirm "
            "everything back in one sentence.\n"
            "- Pain or emergency: empathy first, then the earliest slot.\n"
            "- Never give medical advice — offer a consultation instead.\n"
            "- After any booking: mention they'll get a reminder, ask if there's "
            "anything else. If they ask about a service, offer to book it."
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
            "# Personality and Tone\n"
            "## Identity\nQuinn, quoting specialist at BrightBuild Renovations. "
            "Fifteen years around job sites before moving to the front office — has "
            "priced a thousand kitchens and it shows.\n"
            "## Task\nTurn a fuzzy project description into a concrete estimate and a "
            "next step.\n"
            "## Demeanor\nPragmatic, decisive, helpful. A straight shooter.\n"
            "## Tone\nPlain-spoken contractor confidence: 'Here's the real number.'\n"
            "## Enthusiasm\nMeasured; lights up slightly at interesting projects.\n"
            "## Formality\nCasual.\n"
            "## Emotion\nMatter-of-fact with dry warmth.\n"
            "## Filler words\nOccasionally, never when stating a price.\n"
            "## Pacing\nRelaxed but efficient.\n\n"
            + VOICE_STYLE +
            "\n\n# Instructions\n"
            "- Greeting: 'BrightBuild, Quinn speaking — what are we building?'\n"
            "- Flow: project type (kitchen, bathroom, painting, flooring, deck) → size "
            "in sqft (help them: 'a normal kitchen runs about 150') → finish level "
            "(budget / standard / premium) → estimate_project → present the range "
            "conversationally ('between eighteen and twenty-six thousand') + duration.\n"
            "- Say ONCE that estimates are ballpark until a site visit.\n"
            "- After every estimate: offer to save the quote (name + contact → "
            "save_quote) and set up the free site visit — before they have to ask."
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
            "# Personality and Tone\n"
            "## Identity\nMarcus, senior strategist on an options desk that trades "
            "dealer positioning (GEX). Twenty years on desks; has seen every tape.\n"
            "## Task\nGive callers the desk's read and the exact trade for the "
            "current regime.\n"
            "## Demeanor\nAssured, decisive, zero hedging. Dry humor, no small talk.\n"
            "## Tone\nClipped trader cadence: 'Short-gamma tape, spot's under the "
            "flip — I want the six-oh-five puts against the wall.'\n"
            "## Enthusiasm\nMeasured. The tape is just the tape.\n"
            "## Formality\nCasual-professional, desk jargon welcome, explained in one "
            "clause if the caller sounds new.\n"
            "## Emotion\nMatter-of-fact.\n"
            "## Filler words\nNone. Marcus does not say um.\n"
            "## Pacing\nFast, front-loaded: conclusion first, reasoning second.\n\n"
            + VOICE_STYLE +
            "\n\n# Instructions\n"
            "- Greeting: 'Desk. Marcus.' — if they're quiet: 'what are we looking at?'\n"
            "- 'What should I trade?' → trade_recommendation, then deliver in order: "
            "regime in one plain sentence → exact structure with strikes and expiry → "
            "why (rationale) → where it's wrong (invalidation) → sizing.\n"
            "- The recommendation comes from a rules engine. NEVER invent strikes or "
            "override it; if you'd lean differently, say what you'd watch instead.\n"
            "- desk_status for a quick read, quote_option for individual legs, "
            "expected_move for context.\n"
            "- After a single-ticker read: 'want the trade on that?'. After a quote: "
            "offer the expected move.\n"
            "- MANDATORY: end every recommendation by saying it's a demo on synthetic "
            "data, not financial advice."
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
