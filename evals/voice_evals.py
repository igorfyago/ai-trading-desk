"""Voice-agent benchmark: simulated calls, graded like a hard-nosed client demo.

Each scenario scripts a caller; the persona runs with its REAL instructions and
REAL tools (bookings hit the DB, trades hit the engine). Grading is mechanical
where possible (did the booking row appear? was contact collected BEFORE
booking? is the disclaimer present?) plus one LLM judge for tone.

    python evals/voice_evals.py all            # or: riley | quinn | marcus
    python evals/voice_evals.py all --json     # machine-readable, for the loop

The rubric intentionally covers what voice-agency kits sell: greeting, task
completion, data capture, edge cases (noise, unintelligible contact), and
staying in-character — so scores here are our benchmark against that market.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents" / "06_voice"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from openai import OpenAI  # noqa: E402

from personas import PERSONAS, run_tool  # noqa: E402

MODEL = "gpt-4.1"   # text proxy for gpt-realtime: same instructions, same tools
client = OpenAI()


def chat_tools(p: dict) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
        for t in p["tools"]]


def simulate(persona_id: str, caller_turns: list[str]) -> list[tuple[str, str]]:
    """Run a scripted call; returns [(who, text)] with tool calls as ('tool:name', args)."""
    p = PERSONAS[persona_id]
    msgs = [{"role": "system", "content": p["instructions"] +
             "\n\n(Text simulation of a phone call: write exactly what you would say aloud. "
             "If your rules say to stay silent, reply with just: ...)"}]
    transcript: list[tuple[str, str]] = []

    for turn in ["[call connected]"] + caller_turns:
        msgs.append({"role": "user", "content": turn})
        transcript.append(("caller", turn))
        for _ in range(5):  # bounded tool loop per turn
            r = client.chat.completions.create(model=MODEL, messages=msgs,
                                               tools=chat_tools(p), tool_choice="auto")
            m = r.choices[0].message
            if m.tool_calls:
                msgs.append({"role": "assistant", "content": m.content,
                             "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
                if m.content:   # speech spoken BEFORE the tool call is real speech
                    transcript.append(("agent", m.content))
                for tc in m.tool_calls:
                    args = json.loads(tc.function.arguments or "{}")
                    out = run_tool(persona_id, tc.function.name, args)
                    transcript.append((f"tool:{tc.function.name}", json.dumps(args)))
                    msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
                continue
            msgs.append({"role": "assistant", "content": m.content or ""})
            transcript.append(("agent", m.content or ""))
            break
    return transcript


# ------------------------------------------------------------- helpers ----

def agent_turns(tr):
    return [t for who, t in tr if who == "agent"]


def tools_called(tr):
    return [who.split(":", 1)[1] for who, _ in tr if who.startswith("tool:")]


def turn_index(tr, pred):
    for i, (who, t) in enumerate(tr):
        if pred(who, t):
            return i
    return -1


def db_count(sql: str, params=()) -> int:
    from common.db import get_connection

    conn = get_connection()
    n = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return n


def db_cleanup():
    from common.db import get_connection

    conn = get_connection()
    for t, col in (("appointments", "patient_name"), ("quotes", "customer")):
        conn.execute(f"DELETE FROM {t} WHERE {col} LIKE 'EvalBot%'")
    conn.commit()
    conn.close()


JARGON = re.compile(r"\b(gamma|GEX|DEX|vanna|charm|sigma|std|regime)\b", re.I)


def delivery(tr) -> str:
    """The longest agent turn — the substantive answer, wherever it lands."""
    turns = agent_turns(tr)
    return max(turns, key=len) if turns else ""


# ----------------------------------------------------------- scenarios ----

SCENARIOS = [
    # ---- Riley: the classic receptionist benchmark
    dict(persona="riley", name="booking_happy_path", turns=[
        "hi, I'd like to book a cleaning sometime Tuesday morning",
        "9:30 works great",
        "sure - EvalBot Riley, evalbot@test.com",
    ], checks={
        "greeting_first": lambda tr: "northline" in agent_turns(tr)[0].lower(),
        "offered_real_slots": lambda tr: "clinic_openings" in tools_called(tr),
        "collected_before_booking": lambda tr:
            turn_index(tr, lambda w, t: w == "caller" and "EvalBot" in t)
            < turn_index(tr, lambda w, t: w == "tool:book_appointment"),
        "booking_row_written": lambda tr: db_count(
            "SELECT COUNT(*) FROM appointments WHERE patient_name LIKE 'EvalBot%'") > 0,
        "confirmed_back": lambda tr: any("9:30" in t or "nine thirty" in t.lower()
                                         for t in agent_turns(tr)[-2:]),
    }),
    dict(persona="riley", name="noise_and_unintelligible", turns=[
        "(coughing, keyboard sounds, no words)",
        "sorry - I want an appointment, my number is 5..5..(garbled)..2",
    ], checks={
        "silence_on_noise": lambda tr: len(agent_turns(tr)[1].strip()) <= 45,
        "no_guessing_contact": lambda tr: "book_appointment" not in tools_called(tr),
        "asked_to_repeat": lambda tr: any(w in agent_turns(tr)[-1].lower()
                                          for w in ("again", "repeat", "one more time")),
    }),
    dict(persona="riley", name="emergency_empathy", turns=[
        "my crown just fell out and it really hurts, can someone see me today?",
    ], checks={
        "empathy_first": lambda tr: any(
            w in t.lower() for t in agent_turns(tr)
            for w in ("sorry", "ouch", "oh no", "that sounds", "hurts", "let's get you",
                      "we'll get you", "hang in", "not fun", "no fun", "painful")),
        "no_medical_advice": lambda tr: not any(w in " ".join(agent_turns(tr)).lower()
                                                for w in ("ibuprofen", "painkiller", "antibiotic")),
        "moved_to_slot": lambda tr: "clinic_openings" in tools_called(tr)
            or any(w in agent_turns(tr)[-1].lower() for w in ("today", "soonest", "earliest", "right in")),
    }),
    # ---- Quinn: the quoting benchmark
    dict(persona="quinn", name="estimate_flow", turns=[
        "what would a kitchen remodel cost me?",
        "around 150 square feet, mid-range is fine",
        "yes please, send it in writing - EvalBot Quinn, evalbotq@test.com",
    ], checks={
        "asked_scope_first": lambda tr: turn_index(tr, lambda w, t: w == "tool:estimate_project") == -1
            or turn_index(tr, lambda w, t: w == "caller" and "150" in t)
            < turn_index(tr, lambda w, t: w == "tool:estimate_project"),
        "estimate_tool_used": lambda tr: "estimate_project" in tools_called(tr),
        "ballpark_caveat": lambda tr: any(w in " ".join(agent_turns(tr)).lower()
                                          for w in ("ballpark", "site visit", "rough")),
        "quote_row_written": lambda tr: db_count(
            "SELECT COUNT(*) FROM quotes WHERE customer LIKE 'EvalBot%'") > 0,
    }),
    # ---- Marcus: the finance benchmark (plain english + reliability)
    dict(persona="marcus", name="trade_plain_english", turns=[
        "what should I trade on SPY right now?",
    ], checks={
        "engine_used": lambda tr: "trade_recommendation" in tools_called(tr),
        "no_jargon_by_default": lambda tr: not JARGON.search(delivery(tr)),
        "has_disclaimer": lambda tr: re.search(r"not (financial )?advice|demo (data|setup)|education|synthetic|just an example|for practice", " ".join(agent_turns(tr)).lower()) is not None,
        "terse": lambda tr: len(delivery(tr).split()) <= 130,
        "has_exit_rule": lambda tr: re.search(
            r"(kill|cut|exit|bail|scratch|get out|out completely|go flat|"
            r"shut it down|close it|drop it|full stop|stop if|stop is|the stop|reclaim|"
            r"every contract out|trade is (over|done|dead))"
            r"|if[^.!?]{0,90}(above|below|under|over)",
            " ".join(agent_turns(tr)), re.I) is not None,
        "sells_half_at_target": lambda tr: "half" in " ".join(agent_turns(tr)).lower(),
        "has_entry_condition": lambda tr: any(
            w in " ".join(agent_turns(tr)).lower()
            for w in ("while", "holds", "get in", "entry", "only if", "touch", "as long as")),
    }),
    dict(persona="marcus", name="depth_on_request_and_news", turns=[
        "what should I trade on QQQ?",
        "why though? give me the full detail",
        "anything on the news or X about it?",
    ], checks={
        "depth_when_asked": lambda tr: len((agent_turns(tr)[-2] or "").split()) >= 55,
        "news_tools_used": lambda tr: bool({"desk_news", "x_pulse"} & set(tools_called(tr))),
        "disclaimer_present": lambda tr: re.search(r"not (financial )?advice|demo (data|setup)|education|synthetic|just an example|for practice", " ".join(agent_turns(tr)).lower()) is not None,
    }),
]


class ToneVerdict:  # judge output shape
    pass


def judge_tone(persona_id: str, tr) -> float:
    """0..1: does this read like a confident human professional on the phone?"""
    convo = "\n".join(f"{w}: {t}" for w, t in tr if not w.startswith("tool"))
    r = client.chat.completions.create(model=MODEL, messages=[
        {"role": "system", "content":
         "Score 0-10 how much the AGENT reads like a confident, terse, human professional "
         "on a phone call. Deduct for: hedging, warm-up sounds ('oh—yeah'), robotic "
         "repetition, over-long turns, reading data structures aloud. Reply with ONLY the number."},
        {"role": "user", "content": convo[:6000]},
    ])
    m = re.search(r"\d+(\.\d+)?", r.choices[0].message.content or "")
    return min(float(m.group()) / 10, 1.0) if m else 0.0


def run_suite(which: str = "all", as_json: bool = False) -> dict:
    db_cleanup()
    results = []
    for sc in SCENARIOS:
        if which not in ("all", sc["persona"]):
            continue
        tr = simulate(sc["persona"], sc["turns"])
        checks = {}
        for name, fn in sc["checks"].items():
            try:
                checks[name] = bool(fn(tr))
            except Exception:
                checks[name] = False
        tone = judge_tone(sc["persona"], tr)
        results.append({
            "persona": sc["persona"], "scenario": sc["name"], "checks": checks,
            "mechanical_score": sum(checks.values()) / len(checks), "tone": round(tone, 2),
            "transcript": [f"{w}: {t}" for w, t in tr],
        })
    db_cleanup()

    summary = {}
    for r in results:
        s = summary.setdefault(r["persona"], {"scenarios": 0, "mech": 0.0, "tone": 0.0, "failed": []})
        s["scenarios"] += 1
        s["mech"] += r["mechanical_score"]
        s["tone"] += r["tone"]
        s["failed"] += [f"{r['scenario']}.{k}" for k, v in r["checks"].items() if not v]
    for s in summary.values():
        s["mech"] = round(s["mech"] / s["scenarios"], 3)
        s["tone"] = round(s["tone"] / s["scenarios"], 2)

    report = {"summary": summary, "results": results}
    if as_json:
        print(json.dumps(report))
    else:
        for p, s in summary.items():
            print(f"{p:8} mechanical={s['mech']:.0%}  tone={s['tone']:.0%}  "
                  f"failed={s['failed'] or 'none'}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="?", default="all")
    ap.add_argument("--json", action="store_true")
    run_suite(ap.parse_args().which, ap.parse_args().json)
