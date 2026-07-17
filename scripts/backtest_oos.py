"""Pre-registered out-of-sample evaluation (see docs/BACKTEST.md Phase 2).

H1: afternoon (>=12:45 ET) reversal-day entries carry the edge; morning don't.
H2: capitulation age >= 2h behaves like the afternoon set (maturity, not clock).

Clusters: (day, side) — SPY and QQQ on the same day/side are ONE draw.
  .venv/Scripts/python scripts/backtest_oos.py [--dir scripts/out_oos]
"""

import argparse
import csv
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--dir", default="scripts/out_oos")
args = ap.parse_args()

ROWS = list(csv.DictReader(open(Path(args.dir) / "tape_trades.csv")))
for r in ROWS:
    for k in ("mv_2h", "mv_eod", "opt_pnl_pct", "et_hour", "cap_age_h"):
        r[k] = float(r[k]) if r.get(k) not in (None, "", "None") else None
    r["day"] = int(r["day"])


def clusters(rows):
    """Average within (day, side) so correlated index trades count once."""
    by = defaultdict(list)
    for r in rows:
        by[(r["day"], r["side"])].append(r)
    out = []
    for grp in by.values():
        mv = [g["mv_2h"] for g in grp if g["mv_2h"] is not None]
        op = [g["opt_pnl_pct"] for g in grp if g["opt_pnl_pct"] is not None]
        if mv:
            out.append({"mv": statistics.mean(mv),
                        "op": statistics.mean(op) if op else None})
    return out


def boot_ci(vals, n=6000, seed=11):
    rng = random.Random(seed)
    reps = sorted(statistics.mean(rng.choices(vals, k=len(vals))) for _ in range(n))
    return round(reps[int(0.025 * n)], 2), round(reps[int(0.975 * n)], 2)


def report(rows, name):
    cl = clusters(rows)
    if not cl:
        return {"set": name, "clusters": 0}
    mv = [c["mv"] for c in cl]
    op = [c["op"] for c in cl if c["op"] is not None]
    hits = sum(1 for m in mv if m > 0)
    return {"set": name, "trades": len(rows), "clusters": len(cl),
            "hit": round(hits / len(mv), 3),
            "binom_z": round((hits - len(mv) * 0.5) / math.sqrt(len(mv) * 0.25), 2),
            "mean_bps": round(statistics.mean(mv), 1),
            "ci95_bps": boot_ci(mv),
            "opt_net_pct": round(statistics.mean(op), 1) if op else None,
            "opt_ci95": boot_ci(op) if op else None}


rd = [r for r in ROWS if r["kind"] == "reversal_day"]
sets = {
    "ALL reversal_day": rd,
    "H1 afternoon (>=12:45)": [r for r in rd if (r["et_hour"] or 0) >= 12.75],
    "H1 morning (<12:45)": [r for r in rd if (r["et_hour"] or 99) < 12.75],
    "H2 cap_age >= 2h": [r for r in rd if (r["cap_age_h"] or 0) >= 2],
    "H2 cap_age < 2h": [r for r in rd if r["cap_age_h"] is not None
                        and r["cap_age_h"] < 2],
    "tape_triggered (all)": [r for r in ROWS if r["kind"] == "tape_triggered"],
}
out = [report(v, k) for k, v in sets.items()]
print(json.dumps(out, indent=1))
(Path(args.dir) / "oos_verdict.json").write_text(json.dumps(out, indent=2))
