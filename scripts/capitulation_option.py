"""The capitulation rule, graded on the OPTION instead of the underlying.

Every earlier test of this rule measured the underlying move in bps. That is
linear, and the desk does not trade linear. Losses cap at the premium, the
+50% trim banks half a winner, and gamma makes the right tail fat - which is
why the reversal day reads +12.1 bps on the underlying with a CI touching zero
and +17.7% on the option. A rule can look flat in bps and still pay.

So this reruns the pre-registered entry through the harness's own option sim:
ATM strike, ~2.5 DTE, IV from past realized, 0.75% friction per side, sell
half at +50%, hold the rest to the session close.

    ENTRY (unchanged from docs/PREREG_CAPITULATION.md)
      down bar, volume >= 3x the prior 20-bar average, RSI(14) <= 30
      fill at the next bar's open, long only

    python scripts/capitulation_option.py
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.tape import RSI_MA_N, rsi  # noqa: E402

VOL_MULT, VOL_BASE, RSI_MAX = 3.0, 20, 30.0
COOLDOWN = 8


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def scan(bars: list[dict], sym: str) -> list[dict]:
    from scripts.backtest_tape import (COST_HALF, DTE_YEARS, RTH_END, STEP,
                                       TRIM_MULT, bs_price, et_day, et_secs,
                                       realized_iv)

    rsi_s = rsi([b["c"] for b in bars])
    out, last = [], -10_000
    for i in range(VOL_BASE + RSI_MA_N + 16, len(bars) - 2):
        b = bars[i]
        if b["c"] >= b["o"] or i - last < COOLDOWN:
            continue
        base = [float(x.get("v") or 0) for x in bars[i - VOL_BASE:i]]
        avg = (sum(base) / len(base)) if base else 0.0
        v = float(b.get("v") or 0)
        r = rsi_s[i]
        if avg <= 0 or v < VOL_MULT * avg or r is None or r > RSI_MAX:
            continue
        entry = bars[i + 1]["o"]
        day = et_day(b["t"])
        if entry <= 0 or et_day(bars[i + 1]["t"]) != day:
            continue
        day_bars = [x for x in bars[i + 1:]
                    if et_day(x["t"]) == day and et_secs(x["t"]) < RTH_END]
        if len(day_bars) < 2:
            continue
        last = i

        # the house option sim, lifted from backtest_tape verbatim
        iv = realized_iv(bars, i)
        strike = round(entry)
        prem0 = bs_price(entry, strike, iv, DTE_YEARS, "call") * (1 + COST_HALF)
        if prem0 <= 0.05:
            continue
        trimmed, cash, half, tt = None, 0.0, 0.5, DTE_YEARS
        for x in day_bars:
            tt -= STEP / (365 * 86400)
            mark = bs_price(x["c"], strike, iv, max(tt, 1e-6), "call") * (1 - COST_HALF)
            if trimmed is None and mark >= TRIM_MULT * prem0:
                cash += half * mark
                trimmed = True
        final = bs_price(day_bars[-1]["c"], strike, iv, max(tt, 1e-6),
                         "call") * (1 - COST_HALF)
        cash += (half if trimmed else 1.0) * final
        out.append({"sym": sym, "day": day,
                    "opt": (cash / prem0 - 1) * 100,
                    "und": (day_bars[-1]["c"] - entry) / entry * 10_000,
                    "trimmed": bool(trimmed)})
    return out


def boot(xs: list[float], n: int = 5000) -> tuple[float, float]:
    if len(xs) < 3:
        return float("nan"), float("nan")
    rnd = random.Random(555)
    m = sorted(statistics.mean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return m[int(n * 0.025)], m[int(n * 0.975)]


def report(name: str, rows: list[dict]) -> None:
    if len(rows) < 5:
        print(f"\n  {name}: {len(rows)} trades - too few")
        return
    by: dict[int, list[float]] = {}
    for r in rows:
        by.setdefault(r["day"], []).append(r["opt"])
    cl = [statistics.mean(v) for v in by.values()]
    lo, hi = boot(cl)
    wins = [r["opt"] for r in rows if r["opt"] > 0]
    losses = [r["opt"] for r in rows if r["opt"] <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) else float("inf")
    print(f"\n  {name}")
    print(f"    trades {len(rows):<4} clusters {len(cl)}")
    print(f"    OPTION  mean {statistics.mean(cl):>+7.1f}%   median "
          f"{statistics.median([r['opt'] for r in rows]):>+7.1f}%"
          f"   CI [{lo:+.1f}, {hi:+.1f}]")
    print(f"    win rate {len(wins) / len(rows):>5.1%}   "
          f"avg win {statistics.mean(wins) if wins else 0:>+6.1f}%   "
          f"avg loss {statistics.mean(losses) if losses else 0:>+6.1f}%")
    print(f"    profit factor {pf:>4.2f}   trim rate "
          f"{sum(1 for r in rows if r['trimmed']) / len(rows):.0%}")
    print(f"    (underlying for comparison: "
          f"{statistics.mean(r['und'] for r in rows):+.1f} bps)")


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
        print(f"{sym}: {len(bars)} bars -> {len(r)} trades")
        rows += r
    print(f"\n{'=' * 62}")
    print("  CAPITULATION RULE, graded on the OPTION")
    print(f"{'=' * 62}")
    report("3x volume + RSI<=30, long calls", rows)
    print(f"\n{'=' * 62}")
    print("  Reference: reversal day = +17.7% option EV, 61% hit")
    print(f"{'=' * 62}")


if __name__ == "__main__":
    main()
