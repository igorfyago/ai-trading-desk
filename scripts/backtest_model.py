"""Calibration study over the blindfolded tape backtest: does any mechanical
gate turn the raw signal into an edge, and does it HOLD OUT-OF-SAMPLE?

Split by day (first ~60% of trading days train, rest test) — a gate only
graduates if it improves train AND test. Run after backtest_tape.py:
  .venv/Scripts/python scripts/backtest_model.py
"""

import csv
import json
import math
import statistics
from pathlib import Path

ROWS = list(csv.DictReader(open(Path(__file__).parent / "out" / "tape_trades.csv")))
for r in ROWS:
    for k in ("mv_2h", "mv_eod", "opt_pnl_pct", "et_hour", "hold_bars",
              "capitulation_x", "checklist_done", "dist_sigma", "sess_bar"):
        r[k] = float(r[k]) if r.get(k) not in (None, "", "None") else None
    r["day"] = int(r["day"])

days = sorted({r["day"] for r in ROWS})
cut = days[int(len(days) * 0.6) - 1]
train = [r for r in ROWS if r["day"] <= cut]
test = [r for r in ROWS if r["day"] > cut]

GATES = {
    "all (no gate)":        lambda r: True,
    "afternoon (>=12:45)":  lambda r: r["et_hour"] is not None and r["et_hour"] >= 12.75,
    "hold_bars >= 2":       lambda r: (r["hold_bars"] or 0) >= 2,
    "hold_bars >= 3":       lambda r: (r["hold_bars"] or 0) >= 3,
    "capitulation >= 3x":   lambda r: (r["capitulation_x"] or 0) >= 3,
    "checklist 4/4":        lambda r: (r["checklist_done"] or 0) >= 4,
    "dist_sigma < 1.5":     lambda r: r["dist_sigma"] is not None and abs(r["dist_sigma"]) < 1.5,
    "afternoon OR hold>=3": lambda r: ((r["et_hour"] or 0) >= 12.75
                                       or (r["hold_bars"] or 0) >= 3),
    "morning (<12:45)":     lambda r: r["et_hour"] is not None and r["et_hour"] < 12.75,
}


def stats(rows):
    mv = [r["mv_2h"] for r in rows if r["mv_2h"] is not None]
    pnl = [r["opt_pnl_pct"] for r in rows if r["opt_pnl_pct"] is not None]
    if not mv:
        return None
    hits = sum(1 for m in mv if m > 0)
    return {"n": len(mv), "hit": round(hits / len(mv), 2),
            "z": round((hits - len(mv) * 0.5) / math.sqrt(len(mv) * 0.25), 2),
            "bps": round(statistics.mean(mv), 1),
            "opt": round(statistics.mean(pnl), 1) if pnl else None}


out = {}
for name, gate in GATES.items():
    out[name] = {"train": stats([r for r in train if gate(r)]),
                 "test": stats([r for r in test if gate(r)])}

print(f"days: {len(days)} (train <= day {cut}: {len(train)} trades, "
      f"test: {len(test)} trades)")
print(json.dumps(out, indent=1))
(Path(__file__).parent / "out" / "model_gates.json").write_text(json.dumps(out, indent=2))
