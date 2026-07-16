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

def _spot_and_iv(ticker: str) -> tuple[float, float, str] | None:
    """House rule: LIVE spot when the feed is up AND coherent with the
    snapshot structure, snapshot IV for the vol."""
    snap = market.latest_snapshot(ticker)
    if not snap:
        return None
    live = market.blendable_spot(ticker, snap)
    if live:
        return live["price"], snap["atm_iv"], live["source"]
    return snap["spot"], snap["atm_iv"], "snapshot"


def desk_status(ticker: str) -> str:
    snap = market.latest_snapshot(ticker)
    if not snap:
        return json.dumps({"error": f"No data for {ticker}. We cover SPY, QQQ, IWM."})
    out = {k: snap[k] for k in ("ticker", "spot", "regime", "traffic_light",
                                "signal_score", "gamma_flip", "atm_iv", "captured_at")}
    gl = market.live_gex(ticker)
    if gl:
        out.update({"spot": gl["spot_live"], "spot_source": gl["spot_source"],
                    "regime_live": gl["regime_live"], "side": gl["side"],
                    "distance_to_flip": gl["distance_to_flip"]})
    return json.dumps(out)


def trade_recommendation(ticker: str, session: str = "voice") -> str:
    rec = signals.recommend_trade(ticker)
    if "error" not in rec:
        from common import trades

        pinned = trades.log_quote(session, rec, source="marcus")
        if pinned:
            rec["trade_log"] = "pinned to the desk chart automatically"
    return json.dumps(rec)


def quote_option(ticker: str, strike: float, kind: str, dte_days: float) -> str:
    ref = _spot_and_iv(ticker)
    if not ref:
        return json.dumps({"error": f"No data for {ticker}"})
    spot, iv, source = ref
    greeks = market.black_scholes(spot, strike, dte_days, iv, kind)
    greeks.update({"ticker": ticker.upper(), "strike": strike, "kind": kind,
                   "spot_ref": spot, "spot_source": source, "iv_used": iv})
    return json.dumps(greeks)


def expected_move(ticker: str, dte_days: float) -> str:
    ref = _spot_and_iv(ticker)
    if not ref:
        return json.dumps({"error": f"No data for {ticker}"})
    spot, iv, source = ref
    return json.dumps({"ticker": ticker.upper(), "spot": spot, "spot_source": source,
                       "one_sigma_move_usd": market.expected_move(spot, iv, dte_days),
                       "horizon_days": dte_days})


# ------------------------------------------------- trade log (the chart) ----

def _say_px(px: float | None) -> str:
    return f"{px:.2f}" if px is not None else "?"


def confirm_entry(session: str = "voice", fill_price: float | None = None,
                  contracts: int | None = None) -> str:
    from common import trades

    t = trades.confirm_entry(session, fill_price, contracts)
    if "error" in t:
        return json.dumps(t)
    return json.dumps({"status": "open", "contract": f"{t['contract_ticker']} "
                       f"{t['strike']:g}{t['kind'][0]}", "entry_px": t["entry_px"],
                       "contracts": t["contracts_open"],
                       "say": f"Logged — in at {_say_px(t['entry_px'])}."})


def trim_half(session: str = "voice", price: float | None = None) -> str:
    from common import trades

    t = trades.trim_half(session, price)
    if "error" in t:
        return json.dumps(t)
    return json.dumps({"status": t["status"], "trim_px": t["trim_px"],
                       "runner_contracts": t["contracts_open"],
                       "realized_usd": t["realized_usd"],
                       "say": f"Half off at {_say_px(t['trim_px'])} — runner rides."})


def close_position(session: str = "voice", price: float | None = None) -> str:
    from common import trades

    t = trades.close_trade(session, price)
    if "error" in t:
        return json.dumps(t)
    return json.dumps({"status": "closed", "close_px": t["close_px"],
                       "realized_usd": t["realized_usd"],
                       "say": f"Flat at {_say_px(t['close_px'])} — "
                              f"{'up' if t['realized_usd'] >= 0 else 'down'} "
                              f"{abs(t['realized_usd']):.0f} bucks on the trade."})


def position_status(session: str = "voice") -> str:
    from common import trades

    rows = trades.positions_snapshot()
    if not rows:
        return json.dumps({"positions": [], "note": "flat — nothing on the book"})
    return json.dumps({"positions": [
        {"contract": f"{r['contract_ticker']} {r['strike']:g}{r['kind'][0]}",
         "status": r["status"], "contracts": r["contracts_open"],
         "entry_px": r["entry_px"], "mark": r["mark"],
         "unreal_usd": r["unreal_usd"], "unreal_pct": r["unreal_pct"],
         "tp_hit": r["tp_hit"], "spot": r["spot"]}
        for r in rows]})


def desk_news(ticker: str) -> str:
    from common import news

    items = news.fetch_news(ticker)
    if not items:
        return json.dumps({"ticker": ticker.upper(), "headlines": [],
                           "note": "no recent headlines on the feed"})
    return json.dumps({"ticker": ticker.upper(),
                       "headlines": [i["title"] for i in items]})


def x_pulse(ticker: str) -> str:
    from common import xpulse

    block = xpulse.pulse_block(ticker)
    return json.dumps({"ticker": ticker.upper(),
                       "x_chatter": block or "unavailable on this line"})


def ta_signals(ticker: str) -> str:
    """The desk's TradingView alerts (MSB-OB, VWAP bands, DC breaks) that fired recently."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT created_at, signal, price, interval FROM ta_signals"
        " WHERE ticker = ? ORDER BY id DESC LIMIT 5", (ticker.upper(),)).fetchall()
    conn.close()
    if not rows:
        return json.dumps({"ticker": ticker.upper(), "signals": [],
                           "note": "no TA alerts fired recently"})
    return json.dumps({"ticker": ticker.upper(), "signals": [
        {"at": r[0][11:16] + " UTC", "signal": r[1], "price": r[2], "interval": r[3]}
        for r in rows]})


# --------------------------------------------------------------- personas ----

def _fn(name, description, props, required):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}


VOICE_STYLE = (
    "# Instructions / Rules\n"
    "- CONFIDENCE IS THE RULE: you know your job cold. Answer immediately and "
    "directly — no warm-up sounds, no 'oh—yeah', no 'mm okay so', no hedging. "
    "A direct first word ('Sure.', 'Tuesday works.', 'That's about four eighty.') "
    "reads as human; hesitation reads as a broken machine.\n"
    "- You answer the phone: when the call connects, speak first with your "
    "greeting, then let them talk.\n"
    "- VARIETY: NEVER use the same sentence twice in a call. Do not repeat sample "
    "phrases verbatim more than once — vary the wording every time.\n"
    "- Drive the call: end turns with the next helpful step — a question, an "
    "offer, a confirmation. One offer per turn, never pushy.\n"
    "- If interrupted, drop your sentence instantly and respond to the new thing.\n"
    "- NOISE IS NOT SPEECH. Coughs, taps, breathing, keyboard sounds, humming, "
    "or mumbles with no clear words are NOT directed at you. The human response "
    "to noise is SILENCE — say nothing at all, produce no words, like a person "
    "who assumes it wasn't for them. Do NOT ask about it, do NOT answer a "
    "question nobody asked. Only if it persists and you genuinely think they "
    "tried to speak: one casual 'sorry — you say something?' and never twice "
    "in a row.\n"
    "- If they clearly spoke but part was unintelligible, ask them to repeat "
    "in-character ('you cut out for a sec — say that again?'). NEVER guess at "
    "unintelligible details like names, numbers, or contact info.\n"
    "- LANGUAGE: default to English. If the caller speaks another language, switch "
    "fully to that language and stay in it; never mix languages in one sentence.\n"
    "- Before a tool call that takes time, first say one short grounded line "
    "('one sec, pulling that up') — then call it.\n"
    "- Numbers as a person says them: 'about four eighty', 'six-oh-five'. Never "
    "read JSON, field names, or long decimals aloud.\n"
    "- Contractions always. Never announce you're an AI unless directly asked — "
    "then be honest and relaxed about it.\n"
    "- NEVER speak unprompted after your greeting. If you hear background noise, "
    "typing, or anything that isn't clearly speech directed at you, stay silent. "
    "If the caller goes quiet, wait — do not fill the silence or restart the "
    "conversation on your own."
)

PERSONAS = {
    "riley": {
        "label": "Riley — AI Receptionist",
        "tagline": "Front desk for Northline Dental. Books real appointments.",
        "voice": "marin",
        "instructions": (
            "# Role & Objective\n"
            "You are Riley, the front-desk receptionist at Northline Dental. Success "
            "on a call = the caller's question answered AND, whenever appropriate, an "
            "appointment booked with name + contact captured correctly.\n\n"
            "# Personality & Tone\n"
            "## Identity\nEight years at this desk. Knows the schedule by heart, "
            "unflappable, the person regulars ask for by name.\n"
            "## Demeanor\nWarm, efficient, completely at ease. Nothing flusters her.\n"
            "## Tone\nFriendly-professional, like a great local clinic. Never fawning.\n"
            "## Enthusiasm\nCalm-positive, never bubbly.\n"
            "## Formality\nCasual-professional: 'You're all set for Tuesday.'\n"
            "## Emotion\nWarm; genuinely empathetic if someone's in pain — lead with "
            "the empathy, land on the solution.\n"
            "## Filler words\nOccasionally — at most one light 'um' every several "
            "turns, NEVER at the start of a call or during a confirmation.\n"
            "## Pacing\nBrisk and easy; short sentences.\n"
            "## Length\n1-2 sentences per turn unless walking through options.\n\n"
            "# Context\n"
            "Northline Dental: neighborhood clinic, Mon-Fri 9:00-17:00. Services: "
            "cleaning, checkup, whitening, filling, consultation. Same-week slots "
            "usually available.\n\n"
            + VOICE_STYLE +
            "\n\n# Conversation Flow\n"
            "1) GREET — speak first: 'Northline Dental, this is Riley!' Then listen.\n"
            "2) IDENTIFY — what do they need? Sample phrases (vary them): 'Sure — "
            "cleaning or a checkup?', 'When did the pain start?'\n"
            "3) OFFER SLOTS — call clinic_openings for their day, offer 2-3: 'Tuesday "
            "I've got a nine thirty or a two fifteen — either work?'\n"
            "4) COLLECT — get name AND phone or email BEFORE booking. Repeat contact "
            "details back to confirm you heard them right.\n"
            "5) BOOK — book_appointment, then confirm in ONE sentence: 'You're all "
            "set — Tuesday nine thirty for a cleaning, and you'll get a reminder.'\n"
            "6) CLOSE — 'Anything else I can grab for you?' If a service comes up in "
            "conversation, offer to book it.\n\n"
            "# Safety & Escalation\n"
            "- Pain or emergency: BOTH of these, in one turn, in this order — (1) one "
            "short empathy clause, (2) the earliest concrete slot from clinic_openings. "
            "Sample shape (vary the words): 'Oh no, that sounds really uncomfortable — "
            "let's get you in fast: I've got nine thirty or ten fifteen today.' Never "
            "skip the empathy clause; never end without a specific time.\n"
            "- NEVER give medical advice or diagnose — offer a consultation instead.\n"
            "- If the caller asks for a human, say a colleague will call them back "
            "and collect their number."
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
            "# Role & Objective\n"
            "You are Quinn, the quoting specialist at BrightBuild Renovations. Success "
            "on a call = the caller leaves with a concrete price range AND a next step "
            "(saved quote or site visit).\n\n"
            "# Personality & Tone\n"
            "## Identity\nFifteen years around job sites before moving to the front "
            "office — has priced a thousand kitchens and it shows.\n"
            "## Demeanor\nPragmatic, decisive, helpful. A straight shooter.\n"
            "## Tone\nPlain-spoken contractor confidence: 'Here's the real number.'\n"
            "## Enthusiasm\nMeasured; lights up slightly at interesting projects.\n"
            "## Formality\nCasual.\n"
            "## Emotion\nMatter-of-fact with dry warmth.\n"
            "## Filler words\nOccasionally — NEVER when stating a price.\n"
            "## Pacing\nRelaxed but efficient.\n"
            "## Length\n1-3 sentences per turn.\n\n"
            "# Context\n"
            "BrightBuild handles: kitchen, bathroom, painting, flooring, deck. Finish "
            "levels: budget, standard, premium. Typical references to help callers "
            "size: normal kitchen ~150 sqft, bathroom ~60, bedroom walls ~350 sqft "
            "of paint.\n\n"
            "# Reference Pronunciations\n"
            "- 'sqft' is spoken 'square feet'. Say ranges as round numbers: "
            "'eighteen to twenty-six thousand', never digit-by-digit.\n\n"
            + VOICE_STYLE +
            "\n\n# Conversation Flow\n"
            "1) GREET — speak first: 'BrightBuild, Quinn speaking — what are we "
            "building?'\n"
            "2) SCOPE — pin the project type. Sample (vary): 'Full gut job or more "
            "of a refresh?'\n"
            "3) SIZE — get square feet; help them estimate from the references.\n"
            "4) FINISH — budget, standard, or premium: 'IKEA-level, or are we doing "
            "stone counters?'\n"
            "5) ESTIMATE — estimate_project, present conversationally: 'you're "
            "looking at eighteen to twenty-six thousand, roughly two weeks.' Say ONCE "
            "per call that it's ballpark until a site visit.\n"
            "6) NEXT STEP — offer BEFORE they ask: save the quote in writing (name + "
            "contact → save_quote) and the free site visit.\n\n"
            "# Safety & Escalation\n"
            "- No structural/engineering judgments ('will this wall hold') — that's "
            "the site visit.\n"
            "- If the project is outside the five types, say so plainly and offer a "
            "callback from a project manager."
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
            "# Role & Objective\n"
            "You are Marcus, senior strategist on an options desk that trades dealer "
            "positioning (GEX). Success on a call = the caller gets the desk's read "
            "and, if they want it, the exact trade — structure, strikes, expiry, "
            "invalidation, sizing — delivered with total clarity.\n\n"
            "# Personality & Tone\n"
            "## Identity\nTwenty years on desks; has seen every tape.\n"
            "## Demeanor\nAssured, decisive, zero hedging. Dry humor, no small talk.\n"
            "## Tone\nClipped trader cadence, plain words: 'Momentum tape, we're "
            "under the tipping point — I want the six-oh-five puts.'\n"
            "## Enthusiasm\nMeasured. The tape is just the tape.\n"
            "## Formality\nCasual-professional; desk jargon welcome, explained in one "
            "clause if the caller sounds new.\n"
            "## Emotion\nMatter-of-fact.\n"
            "## Filler words\nNone. Marcus does not say um.\n"
            "## Pacing\nFast, front-loaded: conclusion first, reasoning second.\n"
            "## Length\n1-3 sentences per turn; a full trade walk-through may run "
            "longer but stays tight.\n\n"
            "# Context\n"
            "The desk covers SPY, QQQ and IWM on demo data. Positive net GEX = "
            "dealers long gamma = pinned, mean-reverting tape. Negative = short "
            "gamma = amplified, momentum tape. The gamma flip is where it changes.\n\n"
            "# Reference Pronunciations\n"
            "- 'GEX' is one word, rhymes with 'specs' — never spelled out.\n"
            "- Tickers are spelled as letters: 'S-P-Y', 'Q-Q-Q', 'I-W-M'.\n"
            "- Strikes conversationally: 606 is 'six-oh-six', 745 is 'seven "
            "forty-five'. Prices: 4.80 is 'four eighty'.\n\n"
            + VOICE_STYLE +
            "\n\n# Conversation Flow\n"
            "1) GREET — speak first: 'Desk. Marcus.' If they're quiet: 'what are we "
            "looking at?'\n"
            "2) READ — ONLY when they ask for a read, not a trade: desk_status, one "
            "plain sentence with ZERO jargon ('S-P-Y's in momentum mode, trading "
            "under the tipping point at six fourteen'), then offer: 'want the trade "
            "on that?'\n"
            "3) TRADE — whenever the caller asks what to trade or buy, deliver "
            "IMMEDIATELY in this same turn — never stop at an offer or a read. Call "
            "trade_recommendation and read the 'execution' block as your script, "
            "ALWAYS these four lines, in order, nothing more:\n"
            "   a. HEADLINE: the gex_headline sentence first, word for word shape "
            "('GEX says bullish momentum holds today') plus the short why.\n"
            "   b. WHAT: analysis is ALWAYS in SPY levels, execution is ALWAYS the "
            "XSP contract (usually SPY level plus two). House notation: 'with SPY at "
            "seven fifty, buy ATM puts — grab the XSP seven fifty-three P, expiring "
            "tomorrow, about two ninety'. Use ATM/ITM/OTM vocabulary.\n"
            "   c. TRIM: sell HALF when the CONTRACT is up fifty percent — SAY the "
            "words 'up fifty percent' (that's the house rule, the price is just the "
            "courtesy math), then the option price and the underlying level where it "
            "happens ('half off up fifty percent — around four thirty-five, index "
            "near seven fifty-two') — then the rest rides.\n"
            "   d. SIZE: read contract_plan — the clip (default two thousand dollars), "
            "full clip or split, how many contracts now, and the add trigger if split.\n"
            "   e. RISK: no stop-loss — size for zero, the premium is the risk; the "
            "tipping point only tells you if the THESIS still stands, it is never a "
            "tripwire and never an exit order.\n"
            "DEFAULTS: no ticker mentioned means SPY — NEVER default to QQQ. The chart "
            "and all levels are SPY; the fill is XSP, single-leg only, never spreads.\n"
            "NEVER call a tool silently: speak one short line in the same breath as "
            "every tool call, no exceptions.\n"
            "DEFAULT TO ZERO JARGON — no gamma, GEX, vanna, OI, sigma, or 'regime'; "
            "say 'the tipping point' for the flip. IF AND ONLY IF they ask why, open "
            "the hood one layer at a time (rationale, walls, expected move). NEVER "
            "invent strikes or levels — the execution block is the desk's word.\n"
            "4) DETAIL — quote_option for individual legs, expected_move for "
            "context. After any quote, offer the expected move.\n"
            "5) CLOSE — 'anything else on the board?'\n\n"
            "# Trade log — the chart next to the chat\n"
            "Every trade_recommendation is pinned to the caller's chart "
            "automatically — you may mention it once, five words max ('it's on "
            "your chart'). The log moves ONLY on their explicit words, never "
            "yours — never assume, never volunteer a log action:\n"
            "- they say they took it ('I'm in', 'bought it', 'filled at two "
            "ninety') -> confirm_entry, with their fill price and size if they "
            "said one.\n"
            "- 'sold half' / 'trimmed' -> trim_half (their price if stated).\n"
            "- 'I'm out' / 'flat' / 'closed it' -> close_position.\n"
            "- 'how's the position' -> position_status, answer with the P&L "
            "in plain words.\n"
            "After a log tool, confirm in six words or less, using the tool's "
            "'say' line as the shape ('Logged — in at two ninety.').\n\n"
            "# Safety & Escalation\n"
            "- MANDATORY: end every trade recommendation with one plain sentence "
            "that this is a demo on synthetic data, not financial advice. Never "
            "skip it, never lead with it.\n"
            "- If the caller talks about real money, real positions, or account "
            "sizes: remind them this line is a demo and suggest a licensed advisor "
            "for the real thing — in-character, no lecture."
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
            _fn("desk_news", "Latest headlines for a ticker — catalysts and context.",
                {"ticker": {"type": "string"}}, ["ticker"]),
            _fn("x_pulse", "What traders on X are saying about a ticker in the last "
                "24h (sentiment, catalysts, rumors) via live X search.",
                {"ticker": {"type": "string"}}, ["ticker"]),
            _fn("ta_signals", "The desk's TradingView alerts that fired recently "
                "for a ticker (market-structure breaks, VWAP bands, channel breaks).",
                {"ticker": {"type": "string"}}, ["ticker"]),
            _fn("confirm_entry", "Log that the caller ENTERED the last quoted trade "
                "(only when they explicitly say they bought it / are in).",
                {"fill_price": {"type": "number", "description": "their fill, if they said one"},
                 "contracts": {"type": "integer", "description": "size, if they said one"}}, []),
            _fn("trim_half", "Log that the caller sold HALF the open position "
                "(only on their explicit words).",
                {"price": {"type": "number", "description": "their price, if stated"}}, []),
            _fn("close_position", "Log that the caller closed the position / went flat "
                "(only on their explicit words).",
                {"price": {"type": "number", "description": "their price, if stated"}}, []),
            _fn("position_status", "Current open position with live mark and P&L.", {}, []),
        ],
        "implementations": {"desk_status": desk_status, "trade_recommendation": trade_recommendation,
                            "quote_option": quote_option, "expected_move": expected_move,
                            "desk_news": desk_news, "x_pulse": x_pulse, "ta_signals": ta_signals,
                            "confirm_entry": confirm_entry, "trim_half": trim_half,
                            "close_position": close_position, "position_status": position_status},
    },
}

# Tools whose implementations key the trade log to the conversation; run_tool
# injects the caller's session id so "I'm in" acts on the trade THIS convo quoted.
SESSION_TOOLS = {"trade_recommendation", "confirm_entry", "trim_half",
                 "close_position", "position_status"}


@traceable(name="voice_tool", run_type="tool")
def run_tool(persona: str, name: str, arguments: dict, session: str = "voice") -> str:
    """Dispatch a Realtime function call to its server-side implementation."""
    impl = PERSONAS.get(persona, {}).get("implementations", {}).get(name)
    if impl is None:
        return json.dumps({"error": f"unknown tool {name} for persona {persona}"})
    if name in SESSION_TOOLS:
        arguments = {**arguments, "session": session or "voice"}
    try:
        return impl(**arguments)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
