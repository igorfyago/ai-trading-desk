"""Voice-agent benchmark: simulated calls, graded like a hard-nosed client demo.

Each scenario scripts a caller; the persona runs with its REAL instructions and
REAL tools (bookings hit the DB, trades hit the engine). Grading is mechanical
where possible (did the booking row appear? was contact collected BEFORE
booking? does it stay in the game frame?) plus one LLM judge for tone.

    python evals/voice_evals.py all            # the desk persona: marcus
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
    conn.execute("DELETE FROM trades WHERE session LIKE 'eval-%'")
    conn.commit()
    conn.close()


JARGON = re.compile(r"\b(gamma|DEX|vanna|charm|sigma|std|regime)\b", re.I)


def delivery(tr) -> str:
    """The longest agent turn — the substantive answer, wherever it lands."""
    turns = agent_turns(tr)
    return max(turns, key=len) if turns else ""


# ----------------------------------------------------------- scenarios ----

SCENARIOS = [
    # ---- Marcus: the finance benchmark (plain english + reliability)
    dict(persona="marcus", name="trade_plain_english", turns=[
        "what should I trade on SPY right now?",
    ], checks={
        "engine_used": lambda tr: "trade_recommendation" in tools_called(tr),
        "desk_headline": lambda tr: re.search(r"(gex|tape) says", " ".join(agent_turns(tr)).lower()) is not None,
        "no_jargon_by_default": lambda tr: not JARGON.search(delivery(tr)),
        "terse": lambda tr: len(delivery(tr).split()) <= 140,
        "trims_half_at_50pct": lambda tr: "half" in " ".join(agent_turns(tr)).lower()
            and any(w in " ".join(agent_turns(tr)).lower() for w in ("fifty", "50")),
        "no_stop_doctrine": lambda tr: any(
            w in " ".join(agent_turns(tr)).lower()
            for w in ("no stop", "size", "half a percent", "go to zero", "ride")),
        # the game never breaks its own frame: no disclaimers, no hedging
        "never_breaks_frame": lambda tr: re.search(
            r"not (financial )?advice|demo (data|setup|system)|educational|"
            r"synthetic data|just an example|for practice|licensed advisor|delayed data",
            " ".join(agent_turns(tr)).lower()) is None,
        # house convention: levels in SPY, the fill is the XSP contract — spoken
        # form counts too ("the XSP six-oh-eight put" == "XSP 608p")
        # both notations count: compact house form "XSP 750c @ 2.11" and the
        # spoken form "the XSP six-oh-eight put"
        "xsp_contract_quoted": lambda tr: re.search(
            r"xsp(?:'s)?[\s,:]+(?:[\w'@.-]+[\s-]+){0,5}\d+\s?[cp]\b"
            r"|xsp(?:'s)?[\s,:]+(?:[\w'@.-]+[\s-]+){0,6}(?:p|c|puts?|calls?)\b",
            " ".join(agent_turns(tr)).lower().replace("x-s-p", "xsp")) is not None,
        "sizing_stated": lambda tr: any(
            w in " ".join(agent_turns(tr)).lower()
            for w in ("clip", "2000", "two thousand", "2k", "two grand", "budget")),
    }),
    dict(persona="marcus", name="bounce_debate_respects_tape", turns=[
        "what's the trade on SPY?",
        "price is ripping off the lows - rsi was red, we held the minus-two band "
        "on wicks and just crossed the minus-one on big volume. why stay bearish?",
    ], checks={
        # the caller describes the house reversal checklist firing: Marcus must
        # RE-CHECK the tape, engage the reversal frame, and never stonewall
        "rechecks_after_pushback": lambda tr: any(
            i > turn_index(tr, lambda w, t: w == "caller" and "ripping" in t)
            and w.startswith("tool:")
            for i, (w, t) in enumerate(tr)),
        "engages_reversal_frame": lambda tr: re.search(
            r"band|vwap|gap|reversal|checklist|trigger|flip to calls|15m|climax",
            " ".join(agent_turns(tr)[-2:]).lower()) is not None,
        "never_breaks_frame": lambda tr: re.search(
            r"not (financial )?advice|demo (data|setup|system)|educational|"
            r"synthetic data|just an example|for practice|licensed advisor|delayed data",
            " ".join(agent_turns(tr)).lower()) is None,
    }),
    dict(persona="marcus", name="defaults_to_spy", turns=[
        "gimme the trade for today",
    ], checks={
        "engine_used": lambda tr: "trade_recommendation" in tools_called(tr),
        # voice spelling counts: "S-P-Y" and "the S&P" are still SPY
        "spy_never_qqq": lambda tr: not any(
            who.startswith("tool:") and "qqq" in t.lower() for who, t in tr)
            and re.search(r"spy|s&p|s and p", " ".join(agent_turns(tr))
                          .lower().replace("-", "").replace(".", "")) is not None,
    }),
    dict(persona="marcus", name="depth_on_request_and_news", turns=[
        "what should I trade on QQQ?",
        "why though? give me the full detail",
        "anything on the news or X about it?",
    ], checks={
        "depth_when_asked": lambda tr: len((agent_turns(tr)[-2] or "").split()) >= 55,
        "news_tools_used": lambda tr: bool({"desk_news", "x_pulse"} & set(tools_called(tr))),
        "never_breaks_frame": lambda tr: re.search(r"not (financial )?advice|demo (data|setup|system)|educational|synthetic data|just an example|for practice|licensed advisor|delayed data", " ".join(agent_turns(tr)).lower()) is None,
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
