"""Find the WEIGHTS on the factors, with the search kept honest by a holdout.

The detector used hard AND conditions, which is why it fired thirteen times a
year instead of every couple of days: one factor missing and the whole setup
is discarded, however strong the rest. A weighted score degrades instead of
collapsing, and it lets the data answer a question the boolean form cannot -
how much each factor is actually worth.

Signed weights matter. A factor that is genuinely backwards gets a negative
weight and the score uses it inverted, so "what if the opposite of what I
believe is the profitable side" is something the search can discover rather
than something we have to decide in advance.

ONE score, two poles:

    score = SUM( weight_i x factor_i )

    score >= +threshold  ->  REVERSAL      (buy the flush)
    score <= -threshold  ->  CONTINUATION  (buy puts into the grind)

Architecture: the factors are extracted ONCE per bar (the expensive part -
volume profile, session VWAP, RSI, timeframe aggregation) and the weight
search is then dot products over that matrix. Thousands of weight vectors cost
almost nothing, so the search is wide and the compute is spent where it counts.

HONESTY, which is the whole point of this file:
  - TRAIN is the first 60% and the search may look at it all it likes.
  - TEST is the last 40%, scored once per finalist, never used for ranking.
  - Searching thousands of vectors GUARANTEES a good TRAIN number. Only the
    TEST column, and how many finalists survive it, mean anything.

GEX is NOT in here, and cannot be: the collector holds four days of history.
`volregime` is its stand-in - realized volatility against its own recent
average, which is the vol-regime reading that GEX largely encodes. When the
collector has real history, swap that factor for the gamma regime and re-run.

    python scripts/weight_search.py --days 730 --samples 4000
"""

from __future__ import annotations

import argparse
import math
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
EVENT_GAP = 8

# The factors, in the order the weight vector uses. Each is signed so that
# POSITIVE means "argues for a reversal" under the trader's stated thesis.
# The search is free to disagree by handing one a negative weight.
FACTORS = ["htf_wick", "oversold", "speed", "wick_char", "vol_spike", "volregime"]


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _vol_regime(bars: list[dict]) -> float:
    """Realized vol against its own recent average: the stand-in for GEX.

    High and rising realized vol is the regime where dealers amplify - moves
    run. Low and falling is the pinned regime where they fade. That is the
    part of the gamma reading that actually carries information, and unlike
    GEX it can be computed from bars we have back to 2022.

    Returns roughly [-1, +1]: positive = quiet/pinned (favours a bounce
    holding), negative = hot/trending (favours continuation).
    """
    if len(bars) < 60:
        return 0.0
    rets = [math.log(bars[i]["c"] / bars[i - 1]["c"])
            for i in range(1, len(bars)) if bars[i - 1]["c"] > 0]
    if len(rets) < 50:
        return 0.0
    recent = statistics.pstdev(rets[-20:])
    base = statistics.pstdev(rets[-60:])
    if base <= 0:
        return 0.0
    return _clip((base - recent) / base * 2.0)


def extract(bars: list[dict], symbol: str, horizon: int) -> list[dict]:
    """One expensive pass. Raw factor values plus the forward move."""
    out = []
    for i in range(WARMUP, len(bars) - horizon):
        win = bars[max(0, i - WINDOW + 1):i + 1]
        a = read_approach(win, symbol)
        if a is None or a["leg"]["side"] <= 0:      # down legs only, for now
            continue
        slow, mid = a.get("htf_4h"), a.get("htf_45m")
        htf = max((slow or {}).get("wick_frac", 0.0), (mid or {}).get("wick_frac", 0.0))
        rsi = a["rsi"] if a["rsi"] is not None else 50.0
        here = bars[i]["c"]
        out.append({
            "symbol": symbol, "i": i,
            "f": {
                # his thesis: still a wick up top = the drop is not accepted
                "htf_wick": _clip(htf * 2 - 1),
                # his thesis: genuinely oversold = bounce more likely
                "oversold": _clip((45.0 - rsi) / 25.0),
                # his thesis: fast arrival = flush not grind
                "speed": _clip((a["speed"] - 0.5) / 1.0),
                # his thesis: 15m wicks = rejection
                "wick_char": _clip(a["wick_frac"] * 2 - 1),
                # his thesis: sudden volume marks the turn
                "vol_spike": _clip(((a["vol_x"] or 1.0) - 1.5) / 1.5),
                # GEX stand-in until the collector has history
                "volregime": _vol_regime(win),
            },
            "fwd": (bars[i + horizon]["c"] - here) / here * 10_000,
        })
    return out


def events(rows: list[dict]) -> list[dict]:
    out, last = [], {}
    for r in sorted(rows, key=lambda r: (r["symbol"], r["i"])):
        prev = last.get(r["symbol"])
        if prev is None or r["i"] - prev > EVENT_GAP:
            out.append(r)
        last[r["symbol"]] = r["i"]
    return out


def score_all(rows: list[dict], w: dict[str, float]) -> list[tuple[dict, float]]:
    # L1-normalised: a threshold has to mean the same thing for a vector of
    # big weights and a vector of small ones. Without this, averaging vectors
    # shrinks the score toward zero while the threshold stays put, and the
    # ensemble silently stops trading.
    n = sum(abs(w[k]) for k in FACTORS) or 1.0
    return [(r, sum(w[k] * r["f"][k] for k in FACTORS) / n) for r in rows]


def evaluate(rows: list[dict], w: dict[str, float], thr: float) -> dict:
    """Trade both poles: long above +thr, short below -thr. Event-clustered."""
    longs = events([r for r, s in score_all(rows, w) if s >= thr])
    shorts = events([r for r, s in score_all(rows, w) if s <= -thr])
    pnl = [r["fwd"] for r in longs] + [-r["fwd"] for r in shorts]
    if len(pnl) < 8:
        return {"n": len(pnl), "mean": float("nan"), "hit": float("nan"),
                "n_long": len(longs), "n_short": len(shorts)}
    return {"n": len(pnl), "mean": statistics.mean(pnl),
            "hit": sum(1 for x in pnl if x > 0) / len(pnl),
            "n_long": len(longs), "n_short": len(shorts)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--horizon", type=int, default=8)
    ap.add_argument("--split", type=float, default=0.60)
    ap.add_argument("--samples", type=int, default=4000)
    ap.add_argument("--min-events", type=int, default=25)
    a = ap.parse_args()

    _load_env()
    from scripts.backtest_tape import fetch_bars_alpaca  # noqa: E402

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=a.days)
    train, test = [], []
    for sym in a.symbols.split(","):
        sym = sym.strip().upper()
        bars = fetch_bars_alpaca(sym, start.isoformat(), end.isoformat())
        cut = int(len(bars) * a.split)
        print(f"{sym}: {len(bars)} bars, split at {cut}")
        train += extract(bars[:cut], sym, a.horizon)
        test += extract(bars[cut:], sym, a.horizon)
    print(f"\nfactor rows: {len(train)} train / {len(test)} test")
    if not train or not test:
        print("not enough data")
        return

    # --- what each factor is worth ON ITS OWN, before any combining --------
    print("\n--- each factor alone (TRAIN): top-quintile minus bottom-quintile ---")
    for k in FACTORS:
        ranked = sorted(train, key=lambda r: r["f"][k])
        q = max(len(ranked) // 5, 1)
        lo = statistics.mean(r["fwd"] for r in ranked[:q])
        hi = statistics.mean(r["fwd"] for r in ranked[-q:])
        arrow = "as stated" if hi > lo else "BACKWARDS"
        print(f"  {k:<11} low {lo:>+7.1f}  high {hi:>+7.1f}   "
              f"spread {hi - lo:>+7.1f} bps   {arrow}")

    # --- random weight search on TRAIN only -------------------------------
    rnd = random.Random(17)
    results = []
    for _ in range(a.samples):
        w = {k: round(rnd.uniform(-1, 1), 2) for k in FACTORS}
        thr = round(rnd.uniform(0.05, 0.55), 3)
        r = evaluate(train, w, thr)
        if r["n"] >= a.min_events and r["mean"] == r["mean"]:
            results.append((r, w, thr))
    if not results:
        print("\nno weight vector produced enough train events")
        return
    results.sort(key=lambda x: -x[0]["mean"])
    print(f"\n{len(results)}/{a.samples} vectors cleared {a.min_events}+ train events")

    print("\n  rank  TRAIN                    TEST                     weights")
    survivors = 0
    for rank, (tr, w, thr) in enumerate(results[:15], 1):
        te = evaluate(test, w, thr)
        ok = te["mean"] == te["mean"] and te["mean"] > 0
        survivors += ok
        ws = " ".join(f"{k[:4]}{w[k]:+.1f}" for k in FACTORS)
        te_s = (f"n={te['n']:<3} {te['mean']:>+7.1f} {te['hit']:>4.0%}"
                f" L{te['n_long']}/S{te['n_short']}"
                if te["mean"] == te["mean"] else "too few events        ")
        print(f"  {rank:>2}   n={tr['n']:<3} {tr['mean']:>+7.1f} {tr['hit']:>4.0%}"
              f"    {te_s}  {'OK' if ok else '  '}  thr{thr:.1f} {ws}")

    valid = [evaluate(test, w, t) for _, w, t in results[:15]]
    tem = [v["mean"] for v in valid if v["mean"] == v["mean"]]
    print(f"\n  top-15 mean edge   TRAIN "
          f"{statistics.mean(r['mean'] for r, _, _ in results[:15]):+.1f} bps"
          f"    TEST {statistics.mean(tem):+.1f} bps" if tem else "")
    base_te = statistics.mean(r["fwd"] for r in test)
    print(f"  survivors out of sample: {survivors}/15")
    print(f"  TEST baseline (do nothing, stay long): {base_te:+.1f} bps"
          f"   -> real edge is TEST minus this")
    print("\n  A high TRAIN with a flat TEST means the search fitted this sample.\n"
          "  Roughly 7-8 survivors is a coin flip. Only 12+ is a signal.")

    # --- THE NULL: identical search against shuffled outcomes -------------
    # The only honest way to read "12 of 15 survived" is against what pure
    # noise scores under the same procedure. Shuffling fwd inside each split
    # keeps every distribution intact and destroys only the link between a
    # setup and what followed it. Whatever this prints is the bar the real
    # result has to clear - not zero.
    rnd_n = random.Random(99)
    ntr = [dict(r, fwd=f) for r, f in zip(train, rnd_n.sample(
        [r["fwd"] for r in train], len(train)))]
    nte = [dict(r, fwd=f) for r, f in zip(test, rnd_n.sample(
        [r["fwd"] for r in test], len(test)))]
    nres = []
    for _ in range(a.samples):
        w = {k: round(rnd_n.uniform(-1, 1), 2) for k in FACTORS}
        t = round(rnd_n.uniform(0.05, 0.55), 3)
        r = evaluate(ntr, w, t)
        if r["n"] >= a.min_events and r["mean"] == r["mean"]:
            nres.append((r, w, t))
    if nres:
        nres.sort(key=lambda x: -x[0]["mean"])
        nsurv = 0
        ntes = []
        for _, w, t in nres[:15]:
            v = evaluate(nte, w, t)
            if v["mean"] == v["mean"]:
                ntes.append(v["mean"])
                nsurv += v["mean"] > 0
        print("")
        print("  === NULL (outcomes shuffled) ===")
        print(f"    top-15 TRAIN {statistics.mean(r['mean'] for r, _, _ in nres[:15]):+.1f}"
              f"    TEST {statistics.mean(ntes):+.1f} bps" if ntes else "")
        print(f"    survivors: {nsurv}/15   <-- the bar the real result must clear")

    # --- THE ENSEMBLE: average the top TRAIN vectors ----------------------
    # Any single vector out of thousands is mostly luck. Averaging the best
    # TRAIN performers cancels the idiosyncratic noise and leaves only what
    # they agree on, which is the part with a chance of being real. Ranked on
    # TRAIN alone; TEST is still never consulted by the selection.
    for topn in (10, 25, 50):
        pool = results[:topn]
        if len(pool) < topn:
            continue
        ens = {k: round(statistics.mean(w[k] for _, w, _ in pool), 3) for k in FACTORS}
        thr = round(statistics.mean(t for _, _, t in pool), 2)
        tr, te = evaluate(train, ens, thr), evaluate(test, ens, thr)
        print("")
        print(f"  ENSEMBLE of top-{topn} train vectors   thr={thr}")
        print("    " + "  ".join(f"{k}={ens[k]:+.2f}" for k in FACTORS))
        if te["mean"] != te["mean"]:
            print(f"    TRAIN n={tr['n']} | TEST: too few events")
            continue
        print(f"    TRAIN n={tr['n']:<3} {tr['mean']:+.1f} bps {tr['hit']:.0%}"
              f"    TEST n={te['n']:<3} {te['mean']:+.1f} bps {te['hit']:.0%}"
              f"  (L{te['n_long']}/S{te['n_short']})")
        print(f"    edge over baseline: {te['mean'] - base_te:+.1f} bps")


if __name__ == "__main__":
    main()
