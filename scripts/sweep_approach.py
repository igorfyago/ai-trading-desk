"""Search the approach detector's parameters WITHOUT fooling ourselves.

Iterating on thresholds until the backtest looks good will always succeed.
With a dozen knobs and two years of bars you can make a random rule show a
handsome curve; the search finds the noise that happens to fit. The only
defence is to keep a slice of the data away from the search entirely.

So: the first 60% of the span is TRAIN and the search may look at it as much
as it likes. The last 40% is TEST, scored exactly once per config, and never
consulted while ranking. What you read off the TEST column is the only number
with any claim on the future.

Two diagnostics do most of the work:

  degradation   train edge minus test edge. Large and positive is the
                signature of a fitted rule, and it is normal - a little is
                expected, a lot means the config learned this sample.
  survivors     how many of the top TRAIN configs stay positive on TEST.
                If the best train configs land randomly on test, the whole
                family is noise and no amount of further searching helps.

    python scripts/sweep_approach.py --days 730
"""

from __future__ import annotations

import argparse
import itertools
import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.approach import read_approach  # noqa: E402

WINDOW = 240
WARMUP = 60
EVENT_GAP = 8          # reads closer than this belong to one setup

# The grid. Deliberately small: every extra axis multiplies the number of
# chances to get lucky, and the correction for that is brutal.
GRID = {
    "rsi_oversold": [25.0, 30.0, 35.0, 40.0],
    "htf_wick_frac": [0.35, 0.45, 0.55],
    "htf_body_max": [0.30, 0.40, 0.55],
    "htf_slow": [8, 16],            # 2h or 4h on 15m bars
    "require_fast": [True, False],
    "require_spike": [True, False],
}


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def events(rows: list[dict]) -> list[dict]:
    out, last = [], {}
    for r in sorted(rows, key=lambda r: (r["symbol"], r["i"])):
        prev = last.get(r["symbol"])
        if prev is None or r["i"] - prev > EVENT_GAP:
            out.append(r)
        last[r["symbol"]] = r["i"]
    return out


def boot_lo(xs: list[float], n: int = 800) -> float:
    """Lower bound of a 95% percentile bootstrap on the mean."""
    if len(xs) < 3:
        return float("nan")
    rnd = random.Random(11)
    means = sorted(statistics.mean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(n * 0.025)]


def run(series: dict[str, list[dict]], cfg: dict, horizon: int,
        lo: float, hi: float) -> dict:
    """Score one config over a fractional slice [lo, hi) of each symbol."""
    rows = []
    for sym, bars in series.items():
        a, b = int(len(bars) * lo), int(len(bars) * hi)
        for i in range(max(a, WARMUP), min(b, len(bars) - horizon)):
            r = read_approach(bars[max(0, i - WINDOW + 1):i + 1], sym, cfg)
            if r is None or r["verdict"] != "flush_reversal":
                continue
            here = bars[i]["c"]
            rows.append({"symbol": sym, "i": i,
                         "fwd": (bars[i + horizon]["c"] - here) / here * 10_000})
    ev = events(rows)
    if len(ev) < 3:
        return {"n": len(ev), "mean": float("nan"), "hit": float("nan"),
                "ci_lo": float("nan")}
    f = [e["fwd"] for e in ev]
    return {"n": len(ev), "mean": statistics.mean(f),
            "hit": sum(1 for x in f if x > 0) / len(f), "ci_lo": boot_lo(f)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--horizon", type=int, default=8)
    ap.add_argument("--split", type=float, default=0.60)
    ap.add_argument("--min-events", type=int, default=12)
    a = ap.parse_args()

    _load_env()
    from scripts.backtest_tape import fetch_bars_alpaca  # noqa: E402

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=a.days)
    series = {}
    for sym in a.symbols.split(","):
        sym = sym.strip().upper()
        series[sym] = fetch_bars_alpaca(sym, start.isoformat(), end.isoformat())
        print(f"{sym}: {len(series[sym])} bars {start} -> {end}")

    keys = list(GRID)
    combos = [dict(zip(keys, v)) for v in itertools.product(*GRID.values())]
    print(f"\n{len(combos)} configs | TRAIN 0-{a.split:.0%} | TEST {a.split:.0%}-100% "
          f"(never seen while ranking)\n")

    scored = []
    for n, cfg in enumerate(combos, 1):
        tr = run(series, cfg, a.horizon, 0.0, a.split)
        if tr["n"] < a.min_events or tr["mean"] != tr["mean"]:
            continue
        scored.append((tr, cfg))
        if n % 20 == 0:
            print(f"  ...{n}/{len(combos)}")

    if not scored:
        print("no config produced enough TRAIN events - loosen the grid")
        return
    scored.sort(key=lambda x: -x[0]["mean"])

    print(f"\n{len(scored)} configs cleared {a.min_events}+ train events.")
    print("\n  rank   TRAIN                     TEST                      cfg")
    survivors = 0
    for rank, (tr, cfg) in enumerate(scored[:12], 1):
        te = run(series, cfg, a.horizon, a.split, 1.0)
        ok = te["mean"] == te["mean"] and te["mean"] > 0
        survivors += ok
        short = (f"rsi<={cfg['rsi_oversold']:g} wick>={cfg['htf_wick_frac']} "
                 f"body<{cfg['htf_body_max']} htf={cfg['htf_slow']} "
                 f"{'fast ' if cfg['require_fast'] else ''}"
                 f"{'spike' if cfg['require_spike'] else ''}")
        print(f"  {rank:>2}    n={tr['n']:<3} {tr['mean']:>+7.1f}bps {tr['hit']:>5.0%}"
              f"     n={te['n']:<3} {te['mean']:>+7.1f}bps {te['hit']:>5.0%}"
              f"  {'OK ' if ok else '   '} {short}")

    top = scored[:12]
    tr_mean = statistics.mean(t["mean"] for t, _ in top)
    te_scores = [run(series, c, a.horizon, a.split, 1.0) for _, c in top]
    te_valid = [t["mean"] for t in te_scores if t["mean"] == t["mean"]]
    print(f"\n  top-12 mean edge   TRAIN {tr_mean:+.1f} bps"
          f"   TEST {statistics.mean(te_valid):+.1f} bps" if te_valid else "")
    print(f"  survivors (top-12 still positive out of sample): {survivors}/12")
    print("\n  If TEST is near zero while TRAIN is high, the search fitted this\n"
          "  sample and none of these configs are worth trading.")


if __name__ == "__main__":
    main()
