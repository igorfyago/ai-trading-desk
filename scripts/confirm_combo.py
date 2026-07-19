"""Does the tape trigger ADD anything on top of a reversal day?

docs/BACKTEST.md graded the triggered checklist as a standalone entry and
found no edge (38.5% hit, and -21.4% option EV when I re-ran it). It then
wrote down a hypothesis and never tested it:

    "its likely value is confirmation inside a reversal-day context,
     not standalone entry"

That is the one combination on the table that has not already failed. This
tests it directly by splitting every signal three ways:

    SHAPE ONLY      a takeable reversal day, checklist not fired
    SHAPE + TRIGGER both - the confirmation case, never measured
    TRIGGER ONLY    the checklist alone, already known to lose

PRE-REGISTERED, fixed before the run:

    The combination is an improvement only if SHAPE+TRIGGER beats SHAPE ONLY
    by at least +5.0 bps on the 2h move, with at least 15 clustered events.
    Fewer than 15 events is INCONCLUSIVE, not a pass - the intersection of two
    conditions is small by construction and a handful of trades cannot carry
    a conclusion.

    If SHAPE+TRIGGER is worse, the trigger is confirmed as noise everywhere
    and should be cut from the engine entirely rather than merely demoted.

Blindfold and mechanics are the harness's own: 240-bar window, decision hours
10:00-15:30 true ET, entry at the next bar's open, one position at a time,
day-clustered bootstrap.

    python scripts/confirm_combo.py
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.tape import read_tape  # noqa: E402

WINDOW = 240
HOLD = 8                       # 2 hours on 15m bars
DEC_FROM = 10 * 3600
DEC_TO = 15 * 3600 + 1800
MIN_EVENTS = 15
MIN_GAIN = 5.0


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def scan(bars: list[dict], sym: str) -> list[dict]:
    from scripts.backtest_tape import _dst_offset, et_day

    out, open_until, seen = [], -1, set()
    for i in range(WINDOW, len(bars) - HOLD - 1):
        t = bars[i]["t"]
        if not (DEC_FROM <= (t + _dst_offset(t)) % 86400 < DEC_TO):
            continue
        if i <= open_until:
            continue
        try:
            read = read_tape(bars[i - WINDOW + 1:i + 1], ticker=sym)
        except Exception:
            continue
        ds = (read or {}).get("day_shape")
        shape = bool(ds and ds.get("takeable"))
        trig = bool(read and read.get("stage") == "triggered" and read.get("bias"))
        if not shape and not trig:
            continue

        # side: the shape owns it when present, else the trigger's bias
        if shape:
            bull = ds["shape"].startswith("bull")
        else:
            bull = read["bias"] == "long"

        bucket = ("shape+trigger" if shape and trig
                  else "shape only" if shape else "trigger only")
        key = (et_day(t), bucket, bull)
        if key in seen:
            continue
        seen.add(key)

        entry = bars[i + 1]["o"]
        if entry <= 0 or et_day(bars[i + 1]["t"]) != et_day(t):
            continue
        move = (bars[i + 1 + HOLD]["c"] - entry) / entry * 10_000
        out.append({"sym": sym, "day": et_day(t), "bucket": bucket, "bull": bull,
                    "fwd": move if bull else -move})
        open_until = i + 1 + HOLD
    return out


def boot(xs: list[float], n: int = 5000) -> tuple[float, float]:
    if len(xs) < 3:
        return float("nan"), float("nan")
    rnd = random.Random(808)
    m = sorted(statistics.mean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return m[int(n * 0.025)], m[int(n * 0.975)]


def stats(rows: list[dict]) -> dict:
    by: dict[tuple, list[float]] = {}
    for r in rows:
        by.setdefault((r["day"], r["bull"]), []).append(r["fwd"])
    cl = [statistics.mean(v) for v in by.values()]
    if not cl:
        return {"n": 0}
    lo, hi = boot(cl)
    return {"n": len(cl), "raw": len(rows), "mean": statistics.mean(cl),
            "hit": sum(1 for x in cl if x > 0) / len(cl), "lo": lo, "hi": hi}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-05-15")
    ap.add_argument("--symbols", default="SPY,QQQ")
    a = ap.parse_args()
    _load_env()
    from scripts.backtest_tape import fetch_bars_alpaca  # noqa: E402

    rows = []
    for sym in a.symbols.split(","):
        sym = sym.strip().upper()
        bars = fetch_bars_alpaca(sym, a.start, a.end)
        r = scan(bars, sym)
        print(f"{sym}: {len(bars)} bars -> {len(r)} signals")
        rows += r

    print(f"\n{'=' * 68}")
    print(f"  Does the trigger CONFIRM a reversal day?   {a.start} to {a.end}")
    print(f"{'=' * 68}")
    print("  bucket            events   hit     mean 2h      CI")
    res = {}
    for b in ("shape only", "shape+trigger", "trigger only"):
        s = stats([r for r in rows if r["bucket"] == b])
        res[b] = s
        if not s["n"]:
            print(f"  {b:<16} {0:>6}   never fired")
            continue
        print(f"  {b:<16} {s['n']:>6}  {s['hit']:>5.0%}  {s['mean']:>+8.1f} bps"
              f"   [{s['lo']:+.1f}, {s['hi']:+.1f}]")

    base, combo = res["shape only"], res["shape+trigger"]
    print(f"\n{'=' * 68}")
    if not combo.get("n"):
        print("  VERDICT: INCONCLUSIVE - the two conditions never co-occur.")
    elif combo["n"] < MIN_EVENTS:
        print(f"  VERDICT: INCONCLUSIVE - {combo['n']} events, need {MIN_EVENTS}.")
        print(f"           (combo {combo['mean']:+.1f} vs shape-only "
              f"{base.get('mean', float('nan')):+.1f} bps, too thin to call)")
    else:
        gain = combo["mean"] - base["mean"]
        print(f"  shape only     {base['mean']:+.1f} bps over {base['n']} events")
        print(f"  shape+trigger  {combo['mean']:+.1f} bps over {combo['n']} events")
        print(f"  gain           {gain:+.1f} bps   (bar was +{MIN_GAIN})")
        print(f"\n  VERDICT: {'PASS - the trigger confirms' if gain >= MIN_GAIN else 'FAIL - the trigger adds nothing'}")
    print(f"{'=' * 68}")


if __name__ == "__main__":
    main()
