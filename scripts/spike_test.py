"""Does a capitulation volume bar mark the bottom? Test it properly.

The weight search reported the volume spike as a NEGATIVE contributor, which
contradicts the oldest observation on the desk: at a bottom there is a sudden
enormous volume bar. Both can be true, and the reason matters.

Three ways the first measurement could be asking the wrong question:

  THRESHOLD   1.8x a 20-bar average is an ordinary uptick, not capitulation.
              It fires many times a day. Real flushes are multiples of that,
              so a test that lumps them together measures mostly noise.

  TIMING      the spike marks the turn, but rarely IS the turn. If the low
              prints one or two bars after the flush, entering on the spike
              bar buys the last of the selling.

  DIRECTION   every bottom has a volume spike does not mean every volume
              spike is a bottom. Stops cascading through a broken level look
              identical on the volume histogram and resolve the other way.
              Pooling both cancels the effect out.

So: bucket by how big the spike actually is, and measure the forward move
from several bars AFTER it, not from it.

    python scripts/spike_test.py --days 730
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.tape import RSI_MA_N, rsi, session_vwap, sma  # noqa: E402

WARMUP = 60
VOL_BASE = 20          # bars of average volume a spike is measured against
BUCKETS = [(1.0, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 99.0)]


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def scan(bars: list[dict], sym: str, horizon: int, lags: tuple[int, ...]) -> list[dict]:
    vw = session_vwap(bars)
    closes = [b["c"] for b in bars]
    rsi_s = rsi(closes)
    ma_s = sma(rsi_s, RSI_MA_N)
    out = []
    for i in range(WARMUP, len(bars) - horizon - max(lags)):
        base = [float(b.get("v") or 0) for b in bars[i - VOL_BASE:i]]
        avg = (sum(base) / len(base)) if base else 0.0
        v = float(bars[i].get("v") or 0)
        if avg <= 0 or v <= 0:
            continue
        # only DOWN bars: a capitulation is selling, not a breakout
        if bars[i]["c"] >= bars[i]["o"]:
            continue
        sig = vw[i]["sigma"]
        if sig <= 0:
            continue
        r = rsi_s[i]
        out.append({
            "sym": sym, "i": i,
            "vol_x": v / avg,
            "rsi": r if r is not None else 50.0,
            # how far below VWAP: a flush at the highs is not a bottom
            "band": (bars[i]["c"] - vw[i]["vwap"]) / sig,
            "fwd": {lag: (bars[i + lag + horizon]["c"] - bars[i + lag]["c"])
                    / bars[i + lag]["c"] * 10_000 for lag in lags},
        })
    return out


def report(rows: list[dict], lags: tuple[int, ...], horizon: int) -> None:
    print(f"\n{len(rows)} down bars examined, forward horizon {horizon} bars")

    print("\n--- forward move by SPIKE SIZE, entering N bars after the spike ---")
    head = "  spike size      n     " + "".join(f"lag{l:<2}      " for l in lags)
    print(head)
    for lo, hi in BUCKETS:
        sel = [r for r in rows if lo <= r["vol_x"] < hi]
        if len(sel) < 20:
            continue
        cells = "".join(
            f"{statistics.mean(r['fwd'][l] for r in sel):>+7.1f}    " for l in lags)
        print(f"  {lo:>4.1f}-{hi:<4.1f}   {len(sel):>5}   {cells}")

    print("\n--- BIG spikes only (>=3x), split by where they happen ---")
    big = [r for r in rows if r["vol_x"] >= 3.0]
    if not big:
        print("  none found")
        return
    for name, sel in (
        ("all >=3x", big),
        ("...and oversold (RSI<=30)", [r for r in big if r["rsi"] <= 30]),
        ("...and stretched (<=-1.5 sigma)", [r for r in big if r["band"] <= -1.5]),
        ("...and BOTH", [r for r in big if r["rsi"] <= 30 and r["band"] <= -1.5]),
        ("...near VWAP (>-0.5 sigma)", [r for r in big if r["band"] > -0.5]),
    ):
        if len(sel) < 10:
            print(f"  {name:<32} n={len(sel):<4} (too few)")
            continue
        cells = "  ".join(
            f"lag{l}:{statistics.mean(r['fwd'][l] for r in sel):>+6.1f}" for l in lags)
        hit = sum(1 for r in sel if r["fwd"][lags[-1]] > 0) / len(sel)
        print(f"  {name:<32} n={len(sel):<4} {cells}   hit {hit:.0%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--horizon", type=int, default=8)
    a = ap.parse_args()
    lags = (0, 1, 2, 3)

    _load_env()
    from scripts.backtest_tape import fetch_bars_alpaca  # noqa: E402

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=a.days)
    rows = []
    for sym in a.symbols.split(","):
        sym = sym.strip().upper()
        bars = fetch_bars_alpaca(sym, start.isoformat(), end.isoformat())
        print(f"{sym}: {len(bars)} bars")
        rows += scan(bars, sym, a.horizon, lags)
    report(rows, lags, a.horizon)
    print("\n  lag0 = enter on the spike bar. lag2 = wait two bars, then enter.")
    print("  If the big buckets turn positive at higher lag, the signal is real")
    print("  and the entry was simply early.")


if __name__ == "__main__":
    main()
