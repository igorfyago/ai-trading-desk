"""Both setups under the HOUSE management rules, 2024-2026.

The earlier option sim held whatever survived the trim to the session close
with no management at all. That is not how the desk trades it:

    size for zero        the whole premium is the risk, no stop before the trim
    trim at +50%         sell half, the trade is now free
    runner stop          the other half comes off at ENTRY if it round-trips
    runner target        otherwise let it ride to +400%
    session close        anything still open is flat at the bell

The breakeven stop truncates the left tail after a trim, and the +400% target
lets the right tail actually pay. Both change expectancy in opposite
directions, so neither can be assumed.

Runs the capitulation entry and the reversal day side by side under identical
management, so the comparison is like for like.

CAVEAT, stated because it matters: 2024-2026 SPY/QQQ is the window the
capitulation rule was FOUND in. This is an in-sample measurement of that rule,
not a confirmation. The reversal day is out-of-sample here only in the weak
sense that its own study ran through 2026-05.

    python scripts/house_rules.py
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.tape import RSI_MA_N, read_tape, rsi  # noqa: E402

TRIM_AT = 1.5        # sell half at +50%
RUNNER_STOP = 1.0    # the other half comes off at entry
RUNNER_TARGET = 5.0  # ...or rides to +400%
VOL_MULT, VOL_BASE, RSI_MAX = 3.0, 20, 30.0
COOLDOWN = 8
WINDOW = 240
DEC_FROM, DEC_TO = 10 * 3600, 15 * 3600 + 1800


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def manage(bars_fwd, entry, iv, kind, mods, dte_days=2.5, cost=0.0075):
    """One trade under the house rules. Returns P&L as a % of premium."""
    bs_price, _ignored, _c, STEP = mods
    COST_HALF = cost
    DTE_YEARS = dte_days / 365.0
    strike = round(entry)
    prem0 = bs_price(entry, strike, iv, DTE_YEARS, kind) * (1 + COST_HALF)
    if prem0 <= 0.05:
        return None
    cash, held, tt, trimmed = 0.0, 1.0, DTE_YEARS, False
    for b in bars_fwd:
        tt -= STEP / (365 * 86400)
        mark = bs_price(b["c"], strike, iv, max(tt, 1e-6), kind) * (1 - COST_HALF)
        if not trimmed:
            if mark >= TRIM_AT * prem0:      # bank half, trade is now free
                cash += 0.5 * mark
                held, trimmed = 0.5, True
            continue                          # no stop before the trim: size for zero
        if mark <= RUNNER_STOP * prem0:       # runner round-tripped to entry
            cash += held * mark
            held = 0.0
            break
        if mark >= RUNNER_TARGET * prem0:     # +400%
            cash += held * mark
            held = 0.0
            break
    if held > 0:                              # still open at the bell
        mark = bs_price(bars_fwd[-1]["c"], strike, iv, max(tt, 1e-6), kind) * (1 - COST_HALF)
        cash += held * mark
    return (cash / prem0 - 1) * 100


def scan(bars, sym, rule, dte=2.5, cost=0.0075):
    from scripts.backtest_tape import (COST_HALF, DTE_YEARS, RTH_END, STEP,
                                       bs_price, _dst_offset, et_day, et_secs,
                                       realized_iv)
    mods = (bs_price, DTE_YEARS, COST_HALF, STEP)
    rsi_s = rsi([b["c"] for b in bars])
    out, last, seen = [], -10_000, set()
    for i in range(WINDOW, len(bars) - 2):
        t = bars[i]["t"]
        if i - last < COOLDOWN:
            continue
        side = None
        if rule == "capitulation":
            b = bars[i]
            base = [float(x.get("v") or 0) for x in bars[i - VOL_BASE:i]]
            avg = (sum(base) / len(base)) if base else 0.0
            r = rsi_s[i]
            if (b["c"] < b["o"] and avg > 0 and float(b.get("v") or 0) >= VOL_MULT * avg
                    and r is not None and r <= RSI_MAX):
                side = "long"
        else:
            if not (DEC_FROM <= (t + _dst_offset(t)) % 86400 < DEC_TO):
                continue
            try:
                read = read_tape(bars[i - WINDOW + 1:i + 1], ticker=sym)
            except Exception:
                continue
            ds = (read or {}).get("day_shape")
            if ds and ds.get("takeable"):
                side = "long" if ds["shape"].startswith("bull") else "short"
        if side is None:
            continue
        day = et_day(t)
        if (day, side) in seen:
            continue
        entry = bars[i + 1]["o"]
        if entry <= 0 or et_day(bars[i + 1]["t"]) != day:
            continue
        fwd = [x for x in bars[i + 1:]
               if et_day(x["t"]) == day and et_secs(x["t"]) < RTH_END]
        if len(fwd) < 2:
            continue
        seen.add((day, side))
        last = i
        pnl = manage(fwd, entry, realized_iv(bars, i),
                     "call" if side == "long" else "put", mods, dte, cost)
        if pnl is not None:
            out.append({"day": day, "pnl": pnl})
    return out


def boot(xs, n=5000):
    if len(xs) < 3:
        return float("nan"), float("nan")
    rnd = random.Random(31337)
    m = sorted(statistics.mean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return m[int(n * 0.025)], m[int(n * 0.975)]


def report(name, rows):
    if len(rows) < 5:
        print(f"\n  {name}: {len(rows)} trades - too few")
        return
    by = {}
    for r in rows:
        by.setdefault(r["day"], []).append(r["pnl"])
    cl = [statistics.mean(v) for v in by.values()]
    lo, hi = boot(cl)
    p = [r["pnl"] for r in rows]
    wins = [x for x in p if x > 0]
    losses = [x for x in p if x <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) else float("inf")
    big = sum(1 for x in p if x >= 300)
    print(f"\n  {name}")
    print(f"    trades {len(rows):<4} clusters {len(cl)}")
    print(f"    mean {statistics.mean(cl):>+7.1f}%   median {statistics.median(p):>+7.1f}%"
          f"   CI [{lo:+.1f}, {hi:+.1f}]")
    print(f"    win {len(wins) / len(p):>5.1%}   avg win {statistics.mean(wins) if wins else 0:>+6.1f}%"
          f"   avg loss {statistics.mean(losses) if losses else 0:>+6.1f}%")
    print(f"    profit factor {pf:>4.2f}   trades >= +300%: {big}"
          f"   worst {min(p):>+6.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-07-20")
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--dte", type=float, default=2.5)
    ap.add_argument("--cost", type=float, default=0.0075,
                    help="per-side friction as a fraction of premium")
    a = ap.parse_args()
    _load_env()
    from scripts.backtest_tape import fetch_bars_alpaca  # noqa: E402

    cap, rev = [], []
    for sym in a.symbols.split(","):
        sym = sym.strip().upper()
        bars = fetch_bars_alpaca(sym, a.start, a.end)
        c, r = scan(bars, sym, "capitulation", a.dte, a.cost), scan(bars, sym, "reversal", a.dte, a.cost)
        print(f"{sym}: {len(bars)} bars -> {len(c)} capitulation, {len(r)} reversal")
        cap += c
        rev += r
    print(f"\n{'=' * 64}")
    print(f"  HOUSE RULES  trim 50% / runner stop at entry / target +400%")
    print(f"  {a.start} to {a.end}   DTE={a.dte}   friction={a.cost:.1%}/side")
    print(f"{'=' * 64}")
    report("capitulation (3x vol + RSI<=30)", cap)
    report("reversal day (double bottom + flush + VWAP reclaim)", rev)
    # BOTH: either entry fires, one position per day/side so a day both
    # setups flag is counted once rather than doubling the book
    merged, seen_ds = [], set()
    for r in cap + rev:
        k = (r["day"], r.get("side", "long"))
        if k in seen_ds:
            continue
        seen_ds.add(k)
        merged.append(r)
    report("BOTH (either entry, one position per day)", merged)
    print()


if __name__ == "__main__":
    main()
