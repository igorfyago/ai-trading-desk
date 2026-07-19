"""Independent replication of the reversal-day out-of-sample claim.

docs/BACKTEST.md reports, for the deployed policy (afternoon + capitulation
under two hours old), on Alpaca IEX 15m SPY+QQQ from 2022-01-01 to 2026-05-15:

    112 trades / 62 clusters   61.3% hit   +10.8 bps      (all reversal-day)
     92 trades / 46 clusters   67.4% hit   +21.1 bps      (age < 2h)
     20 trades / 18 clusters   44.4% hit   -15.6 bps      (age >= 2h)

Re-running scripts/backtest_tape.py would only prove that script is
deterministic. This is a separate implementation: it calls the ENGINE's own
day_shape - that is the thing under test - but does its own blindfolding,
entry, grading, clustering and bootstrap. If the two agree, the result is real
and reproducible. If they disagree, one of them has a bug and that matters
more than any number produced today.

Blindfold: at bar i the detector sees bars[i-239 : i+1] only. Entry fills at
the NEXT bar's open. Grading walks strictly forward.

    python scripts/replicate_dayshape.py
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

WINDOW = 240      # live parity
HOLD = 8          # 2 hours on 15m bars
WARMUP = 60


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def scan(bars: list[dict], sym: str) -> list[dict]:
    """One trade per day/kind/side, one position at a time - as reported."""
    out, open_until, seen = [], -1, set()
    for i in range(WARMUP, len(bars) - HOLD - 1):
        if i < open_until:
            continue
        # read_tape, NOT day_shape: age_h and takeable are stamped by the
        # former. Calling day_shape directly returns a dict without them, and
        # a takeable check against a key that never exists silently discards
        # every trade - which is exactly what it did on the first attempt.
        read = read_tape(bars[max(0, i - WINDOW + 1):i + 1], ticker=sym)
        ds = (read or {}).get("day_shape")
        if not ds or not ds.get("takeable"):
            continue
        bull = ds["shape"].startswith("bull")
        day = bars[i]["t"] // 86400
        key = (sym, day, bull)
        if key in seen:                       # one per day/side/symbol
            continue
        seen.add(key)
        entry = bars[i + 1]["o"]
        if entry <= 0:
            continue
        move = (bars[i + 1 + HOLD]["c"] - entry) / entry * 10_000
        out.append({"sym": sym, "day": day, "bull": bull, "age_h": ds["age_h"],
                    # signed the way the trade is taken: a bullish shape is bought
                    "fwd": move if bull else -move})
        open_until = i + 1 + HOLD
    return out


def boot(xs: list[float], n: int = 5000) -> tuple[float, float]:
    if len(xs) < 3:
        return float("nan"), float("nan")
    rnd = random.Random(4242)
    m = sorted(statistics.mean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return m[int(n * 0.025)], m[int(n * 0.975)]


def score(name: str, tr: list[dict], claim: str) -> None:
    if not tr:
        print(f"\n  {name:<26} no trades")
        return
    by_day: dict[tuple, list[float]] = {}
    for t in tr:
        by_day.setdefault((t["day"], t["bull"]), []).append(t["fwd"])
    cl = [statistics.mean(v) for v in by_day.values()]
    m = statistics.mean(cl)
    lo, hi = boot(cl)
    hit = sum(1 for x in cl if x > 0) / len(cl)
    print(f"\n  {name}")
    print(f"    trades {len(tr):<4} clusters {len(cl):<4} hit {hit:>5.1%}"
          f"   mean {m:>+7.1f} bps   CI [{lo:+.1f}, {hi:+.1f}]")
    print(f"    reported: {claim}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-05-15")
    ap.add_argument("--symbols", default="SPY,QQQ")
    a = ap.parse_args()
    _load_env()
    from scripts.backtest_tape import fetch_bars_alpaca  # noqa: E402

    trades = []
    for sym in a.symbols.split(","):
        sym = sym.strip().upper()
        bars = fetch_bars_alpaca(sym, a.start, a.end)
        t = scan(bars, sym)
        print(f"{sym}: {len(bars)} bars -> {len(t)} trades")
        trades += t

    print(f"\n{'=' * 66}")
    print(f"  REPLICATION - reversal day, {a.start} to {a.end}")
    print(f"{'=' * 66}")
    score("all reversal-day", trades,
          "112 trades / 62 clusters, 61.3% hit, +10.8 bps")
    score("capitulation age < 2h", [t for t in trades if t["age_h"] < 2.0],
          "92 trades / 46 clusters, 67.4% hit, +21.1 bps")
    score("capitulation age >= 2h", [t for t in trades if t["age_h"] >= 2.0],
          "20 trades / 18 clusters, 44.4% hit, -15.6 bps")
    print("\n  A replication is trade counts in the same neighbourhood, the same"
          "\n  sign, and the same ordering between the two age buckets. Exact"
          "\n  equality is not expected - the vendor revises bars, and the"
          "\n  original run was a different day's pull.")


if __name__ == "__main__":
    main()
