"""Calibrate the approach detector against real bars, blindfolded.

The FAST/GRIND thresholds in common/approach.py started as guesses. Guessed
thresholds are how a detector ends up fitting the person who wrote it instead
of the tape. This measures the real distribution of approach speed and, more
importantly, whether speed separates what happens NEXT.

Blindfold: at bar i the detector sees bars[i-239 : i+1] only - the same 240-bar
window the live desk reads - and the outcome is graded from bars strictly after
i. Nothing downstream of i can reach the decision.

    python scripts/calibrate_approach.py --days 90
    python scripts/calibrate_approach.py --days 90 --symbols SPY,QQQ --horizon 8
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.approach import (FAST_SIGMA_PER_BAR, GRIND_SIGMA_PER_BAR,  # noqa: E402
                             read_approach)

WINDOW = 240      # live parity: get_tape_read pulls 240 bars
WARMUP = 60       # need enough history before the first honest read


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(int(len(s) * p), len(s) - 1)]


def _fmt(x: float) -> str:
    return "n/a" if x != x else f"{x:.3f}"


def scan(bars: list[dict], symbol: str, horizon: int) -> list[dict]:
    """Walk forward. Decide on the past, grade on the future."""
    rows = []
    for i in range(WARMUP, len(bars) - horizon):
        window = bars[max(0, i - WINDOW + 1):i + 1]
        a = read_approach(window, symbol)
        if a is None:
            continue
        here = bars[i]["c"]
        fwd = (bars[i + horizon]["c"] - here) / here * 10_000   # bps
        rows.append({
            "i": i, "t": bars[i]["t"], "symbol": symbol,
            "speed": a["speed"], "leg_side": a["leg"]["side"],
            "wick": a["wick_frac"], "body": a["body_frac"],
            "exhausted": a["exhausted"], "spiked": a["spiked"],
            "htf_unaccepted": a["htf_unaccepted"], "htf_accepted": a["htf_accepted"],
            "rsi": a["rsi"], "vol_x": a["vol_x"],
            "at_level": a["at_level"], "air": bool(a["air_ahead"]),
            "verdict": a["verdict"], "trade": a["trade"],
            "fwd_bps": fwd,
        })
    return rows


def report(rows: list[dict], horizon: int) -> None:
    if not rows:
        print("no reads produced - not enough bars?")
        return
    down = [r for r in rows if r["leg_side"] > 0]      # price fell into here
    speeds = [r["speed"] for r in down]

    print(f"\n{len(rows)} reads ({len(down)} on a down leg), horizon {horizon} bars")
    print("\n--- speed distribution on down legs (sigma/bar) ---")
    for p in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        print(f"  p{int(p*100):<3} {_pct(speeds, p):.3f}")
    print(f"  current thresholds: GRIND <= {GRIND_SIGMA_PER_BAR}, "
          f"FAST >= {FAST_SIGMA_PER_BAR}")
    slow_share = sum(1 for s in speeds if s <= GRIND_SIGMA_PER_BAR) / len(speeds)
    fast_share = sum(1 for s in speeds if s >= FAST_SIGMA_PER_BAR) / len(speeds)
    print(f"  -> {slow_share:.1%} classify slow, {fast_share:.1%} classify fast, "
          f"{1 - slow_share - fast_share:.1%} land in neither")

    # THE question: does speed separate what happens next?
    print(f"\n--- forward {horizon}-bar move by speed decile (down legs) ---")
    print("  decile   speed range        n     mean bps    median")
    ranked = sorted(down, key=lambda r: r["speed"])
    k = max(len(ranked) // 10, 1)
    for d in range(10):
        chunk = ranked[d * k:(d + 1) * k] if d < 9 else ranked[9 * k:]
        if not chunk:
            continue
        f = [c["fwd_bps"] for c in chunk]
        print(f"  {d+1:>2}       {chunk[0]['speed']:.2f}-{chunk[-1]['speed']:.2f}"
              f"      {len(chunk):>4}   {statistics.mean(f):>+8.1f}   "
              f"{statistics.median(f):>+7.1f}")

    # and does the wick/body character add on top of speed?
    print("\n--- fast down legs, split by character ---")
    fast = [r for r in down if r["speed"] >= FAST_SIGMA_PER_BAR]
    for name, sel in (("wick-rejected (>=50%)", lambda r: r["wick"] >= 0.5),
                      ("body-accepted (>=60%)", lambda r: r["body"] >= 0.6),
                      ("RSI oversold (<=30)", lambda r: r["exhausted"]),
                      ("volume spike", lambda r: r["spiked"]),
                      ("oversold AND spiked", lambda r: r["exhausted"] and r["spiked"]),
                      ("at a volume shelf", lambda r: r["at_level"]),
                      ("air ahead", lambda r: r["air"]),
                      ("HTF still a WICK", lambda r: r["htf_unaccepted"]),
                      ("HTF has ACCEPTED", lambda r: r["htf_accepted"]),
                      ("wick + HTF wick", lambda r: r["wick"] >= 0.5 and r["htf_unaccepted"]),
                      ("oversold + HTF wick", lambda r: r["exhausted"] and r["htf_unaccepted"])):
        s = [r["fwd_bps"] for r in fast if sel(r)]
        if s:
            print(f"  {name:<24} n={len(s):>4}  mean {statistics.mean(s):>+7.1f} bps"
                  f"   median {statistics.median(s):>+7.1f}")

    print(f"\n--- the detector as currently tuned (EVENTS, not bars) ---")
    for v in ("flush_reversal", "grind_followthru"):
        ev = _events([r for r in rows if r["verdict"] == v])
        if not ev:
            print(f"  {v:<18} never fired")
            continue
        # a flush is bought, a grind is sold: sign the move by the trade side
        signed = [r["fwd_bps"] * (1 if r["trade"] == "long" else -1) for r in ev]
        wins = sum(1 for x in signed if x > 0)
        lo, hi = _boot(signed)
        print(f"  {v:<18} {len(ev):>3} events  hit {wins/len(ev):.1%}   "
              f"mean {statistics.mean(signed):>+7.1f} bps   "
              f"CI [{lo:+.1f}, {hi:+.1f}]")


def _events(rows: list[dict], gap: int = 8) -> list[dict]:
    """Collapse overlapping reads into distinct events.

    Consecutive bars inside one flush each produce a read, so counting reads
    massively overstates the sample: 31 reads can be 5 setups. One event per
    run of reads on the same symbol separated by fewer than `gap` bars, and
    the FIRST read is the one that counts - that is when a live desk would
    actually have acted.
    """
    out, last = [], {}
    for r in sorted(rows, key=lambda r: (r["symbol"], r["i"])):
        prev = last.get(r["symbol"])
        if prev is None or r["i"] - prev > gap:
            out.append(r)
        last[r["symbol"]] = r["i"]
    return out


def _boot(xs: list[float], n: int = 2000) -> tuple[float, float]:
    """Percentile bootstrap CI on the mean. Small n stays honest this way."""
    import random
    if len(xs) < 2:
        return float("nan"), float("nan")
    rnd = random.Random(7)
    means = sorted(statistics.mean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(n * 0.025)], means[int(n * 0.975)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--horizon", type=int, default=8, help="bars forward (8 = 2h)")
    a = ap.parse_args()

    _load_env()
    from scripts.backtest_tape import fetch_bars_alpaca  # noqa: E402

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=a.days)
    allrows = []
    for sym in a.symbols.split(","):
        sym = sym.strip().upper()
        bars = fetch_bars_alpaca(sym, start.isoformat(), end.isoformat())
        print(f"{sym}: {len(bars)} bars {start} -> {end}")
        allrows += scan(bars, sym, a.horizon)
    report(allrows, a.horizon)


if __name__ == "__main__":
    main()
