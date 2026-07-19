"""Does the capitulation entry work in the MORNING and PREMARKET?

The engine's day_shape needs 20 session bars and is therefore blind until
about 13:00 ET, which is why every prior result came from afternoon tape. The
capitulation rule has no such requirement - it reads a trailing 20-bar volume
average and RSI, both of which cross session boundaries - and the IEX feed
prints from 08:00 ET. So premarket and morning bars were in the data all
along, and nothing yet has looked at what the rule did in them.

Splits the same pre-registered entry by session and scores each independently:

    PRE    08:00-09:30 ET     premarket
    AM     09:30-12:45 ET     the morning, the window day_shape cannot see
    PM     12:45-16:00 ET     the afternoon, the only window measured so far
    POST   16:00+   ET        after hours

Same rule, same pass bar, same day-clustered bootstrap. Only the clock changes.

    python scripts/session_split.py --days 1500
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.tape import RSI_MA_N, _true_et_secs, rsi  # noqa: E402

VOL_MULT = 3.0
VOL_BASE = 20
RSI_MAX = 30.0
HOLD = 8
COOLDOWN = 8

SESSIONS = [
    ("PRE   08:00-09:30", 8 * 3600, 9 * 3600 + 1800),
    ("AM    09:30-12:45", 9 * 3600 + 1800, 12 * 3600 + 2700),
    ("PM    12:45-16:00", 12 * 3600 + 2700, 16 * 3600),
    ("POST  16:00+", 16 * 3600, 24 * 3600),
]


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def scan(bars: list[dict], sym: str) -> tuple[list[dict], list[dict]]:
    """Every fired signal, and every down bar (the per-session baseline)."""
    rsi_s = rsi([b["c"] for b in bars])
    events, allbars, last = [], [], -10_000
    for i in range(VOL_BASE + RSI_MA_N + 16, len(bars) - HOLD - 1):
        b = bars[i]
        if b["c"] >= b["o"]:
            continue
        base = [float(x.get("v") or 0) for x in bars[i - VOL_BASE:i]]
        avg = (sum(base) / len(base)) if base else 0.0
        v = float(b.get("v") or 0)
        if avg <= 0 or v <= 0:
            continue
        entry = bars[i + 1]["o"]
        if entry <= 0:
            continue
        fwd = (bars[i + 1 + HOLD]["c"] - entry) / entry * 10_000
        row = {"sym": sym, "et": _true_et_secs(b["t"]), "day": b["t"] // 86400,
               "fwd": fwd, "vol_x": v / avg}
        allbars.append(row)
        r = rsi_s[i]
        if r is None or r > RSI_MAX or v < VOL_MULT * avg:
            continue
        if i - last < COOLDOWN:
            continue
        last = i
        events.append(row)
    return events, allbars


def cluster(rows: list[dict]) -> list[float]:
    by_day: dict[int, list[float]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r["fwd"])
    return [statistics.mean(v) for v in by_day.values()]


def boot(xs: list[float], n: int = 5000) -> tuple[float, float]:
    if len(xs) < 3:
        return float("nan"), float("nan")
    rnd = random.Random(2027)
    m = sorted(statistics.mean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return m[int(n * 0.025)], m[int(n * 0.975)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1500)
    ap.add_argument("--symbols", default="SPY,QQQ")
    a = ap.parse_args()
    _load_env()
    from scripts.backtest_tape import fetch_bars_alpaca  # noqa: E402

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=a.days)
    events, allbars = [], []
    for sym in a.symbols.split(","):
        sym = sym.strip().upper()
        bars = fetch_bars_alpaca(sym, start.isoformat(), end.isoformat())
        e, ab = scan(bars, sym)
        print(f"{sym}: {len(bars)} bars, {len(e)} signals")
        events += e
        allbars += ab

    # where do the bars even exist? if a session has no bars, it was never
    # testable and saying "it failed there" would be a lie
    print("\n--- bar coverage by session (down bars only) ---")
    for name, lo, hi in SESSIONS:
        n = sum(1 for r in allbars if lo <= r["et"] < hi)
        print(f"  {name:<20} {n:>6} bars")

    print("\n--- the capitulation entry, by session ---")
    print("  session               events   mean      CI                hit    baseline  edge")
    for name, lo, hi in SESSIONS:
        ev = [r for r in events if lo <= r["et"] < hi]
        base_rows = [r for r in allbars if lo <= r["et"] < hi]
        if not base_rows:
            print(f"  {name:<20}   no bars in this window")
            continue
        base = statistics.mean(r["fwd"] for r in base_rows)
        cl = cluster(ev)
        if len(cl) < 10:
            print(f"  {name:<20} {len(cl):>6}   too few to judge"
                  f"                          {base:>+7.1f}")
            continue
        m = statistics.mean(cl)
        lo_ci, hi_ci = boot(cl)
        hit = sum(1 for x in cl if x > 0) / len(cl)
        print(f"  {name:<20} {len(cl):>6} {m:>+7.1f}  [{lo_ci:>+6.1f},{hi_ci:>+6.1f}]"
              f"   {hit:>4.0%}   {base:>+7.1f}  {m - base:>+7.1f}")

    print("\n  edge is mean minus that session's own baseline. A session only")
    print("  counts as tested if it has bars AND at least 10 clustered events.")


if __name__ == "__main__":
    main()
