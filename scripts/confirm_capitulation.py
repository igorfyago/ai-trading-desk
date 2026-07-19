"""The confirmation run for docs/PREREG_CAPITULATION.md.

Scores the pre-registered rule against data that no earlier script has read.
The rule and the pass bar are fixed in that document; this file only executes
them. If the numbers disappoint, the honest response is to record a FAIL, not
to look for a variant that clears the bar - searching the confirmation set
burns it exactly like the training set was burned.

    python scripts/confirm_capitulation.py
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.tape import RSI_MA_N, rsi, sma  # noqa: E402

# --- the pre-registered rule. Do not edit after the first run. --------------
VOL_MULT = 3.0
VOL_BASE = 20
RSI_MAX = 30.0
HOLD = 8
COOLDOWN = 8          # one position at a time

# --- the pre-registered pass bar --------------------------------------------
MIN_EVENTS = 30
MIN_EDGE_OVER_BASELINE = 5.0
MIN_HIT = 0.50


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def signals_for(bars: list[dict], sym: str) -> tuple[list[dict], list[float]]:
    """Fire the rule. Returns (events, every-down-bar baseline)."""
    closes = [b["c"] for b in bars]
    rsi_s = rsi(closes)
    sma(rsi_s, RSI_MA_N)          # parity with the desk's RSI pipeline
    events, baseline, last = [], [], -10_000
    for i in range(VOL_BASE + RSI_MA_N + 16, len(bars) - HOLD - 1):
        b = bars[i]
        if b["c"] >= b["o"]:
            continue
        base = [float(x.get("v") or 0) for x in bars[i - VOL_BASE:i]]
        avg = (sum(base) / len(base)) if base else 0.0
        v = float(b.get("v") or 0)
        if avg <= 0 or v <= 0:
            continue
        # fill at the NEXT bar's open, exit HOLD bars later
        entry = bars[i + 1]["o"]
        if entry <= 0:
            continue
        fwd = (bars[i + 1 + HOLD]["c"] - entry) / entry * 10_000
        baseline.append(fwd)
        r = rsi_s[i]
        if r is None or r > RSI_MAX or v < VOL_MULT * avg:
            continue
        if i - last < COOLDOWN:
            continue
        last = i
        events.append({"sym": sym, "i": i, "day": bars[i]["t"] // 86400,
                       "fwd": fwd, "vol_x": v / avg, "rsi": r})
    return events, baseline


def cluster(events: list[dict]) -> list[float]:
    """One draw per day: SPY and QQQ on the same day are not independent."""
    by_day: dict[int, list[float]] = {}
    for e in events:
        by_day.setdefault(e["day"], []).append(e["fwd"])
    return [statistics.mean(v) for v in by_day.values()]


def boot(xs: list[float], n: int = 5000) -> tuple[float, float]:
    if len(xs) < 3:
        return float("nan"), float("nan")
    rnd = random.Random(2027)
    m = sorted(statistics.mean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return m[int(n * 0.025)], m[int(n * 0.975)]


def score(name: str, events: list[dict], baseline: list[float]) -> bool:
    print(f"\n{'=' * 62}\n  {name}\n{'=' * 62}")
    if not events:
        print("  no signals fired")
        return False
    cl = cluster(events)
    mean = statistics.mean(cl)
    lo, hi = boot(cl)
    hit = sum(1 for x in cl if x > 0) / len(cl)
    base = statistics.mean(baseline) if baseline else 0.0
    edge = mean - base

    print(f"  raw signals      {len(events)}")
    print(f"  clustered events {len(cl)}   (one per day)")
    print(f"  mean 8-bar move  {mean:+.1f} bps    CI [{lo:+.1f}, {hi:+.1f}]")
    print(f"  hit rate         {hit:.1%}")
    print(f"  baseline         {base:+.1f} bps  (every down bar, same sample)")
    print(f"  edge over base   {edge:+.1f} bps")

    checks = [
        (f"CI excludes zero", lo == lo and lo > 0),
        (f"edge >= +{MIN_EDGE_OVER_BASELINE} bps", edge >= MIN_EDGE_OVER_BASELINE),
        (f"hit > {MIN_HIT:.0%}", hit > MIN_HIT),
        (f"events >= {MIN_EVENTS}", len(cl) >= MIN_EVENTS),
    ]
    print("\n  pass bar (fixed in advance):")
    for label, ok in checks:
        print(f"    [{'PASS' if ok else 'FAIL'}] {label}")
    if len(cl) < MIN_EVENTS:
        print("\n  VERDICT: INCONCLUSIVE - sample too thin to judge")
        return False
    verdict = all(ok for _, ok in checks)
    print(f"\n  VERDICT: {'PASS' if verdict else 'FAIL'}")
    return verdict


def main() -> None:
    ap = argparse.ArgumentParser()
    a = ap.parse_args()
    _load_env()
    from scripts.backtest_tape import fetch_bars_alpaca  # noqa: E402

    today = datetime.now(timezone.utc).date().isoformat()

    # SET A: IWM, never scanned by any script, at any point
    ev_a, base_a = [], []
    for sym in ("IWM",):
        bars = fetch_bars_alpaca(sym, "2022-01-01", today)
        print(f"{sym}: {len(bars)} bars")
        e, b = signals_for(bars, sym)
        ev_a += e
        base_a += b
    ok_a = score("SET A - IWM 2022-2026 (instrument never examined)", ev_a, base_a)

    # SET B: SPY/QQQ before the earliest window any search opened
    ev_b, base_b = [], []
    for sym in ("SPY", "QQQ"):
        bars = fetch_bars_alpaca(sym, "2018-01-01", "2022-05-31")
        print(f"\n{sym}: {len(bars)} bars (pre-2022-06)")
        e, b = signals_for(bars, sym)
        ev_b += e
        base_b += b
    ok_b = score("SET B - SPY+QQQ 2018 to 2022-05 (period never opened)", ev_b, base_b)

    print(f"\n{'=' * 62}")
    print(f"  SET A (IWM):        {'PASS' if ok_a else 'FAIL/INCONCLUSIVE'}")
    print(f"  SET B (pre-2022):   {'PASS' if ok_b else 'FAIL/INCONCLUSIVE'}")
    print(f"{'=' * 62}")


if __name__ == "__main__":
    main()
