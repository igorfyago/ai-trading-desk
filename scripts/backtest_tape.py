"""Blindfolded tape backtest: walk 60 days of 15m bars, hand the engine ONLY
the past at every step (the same trailing 240-bar window the live desk reads),
take the tape-driven trades, and grade them forward — model-free directional
moves AND the house option rules.

Clock honesty rules (the whole point):
  * decisions read bars[.. i] sliced to the live 240-bar window, scrubbed
    INSIDE the window only — the scrubber never sees a bar the desk hadn't;
  * entries fill at the NEXT bar's open, never the signal bar;
  * grading walks strictly forward from the entry bar.

No third-party deps — statistics/math/random only. Run:
  .venv/Scripts/python scripts/backtest_tape.py [--symbols SPY,QQQ] [--out DIR]
"""

import argparse
import csv
import json
import math
import os
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import tape  # noqa: E402
from common.quotes import _client, _scrub_bars, _yahoo_sym, _YAHOO  # noqa: E402

WINDOW = 240          # live parity: get_tape_read pulls limit=240 bars
STEP = 900            # 15m
ET_OFFSET = -4 * 3600  # session bucketing keeps the engine's fixed offset (parity)
DEC_FROM = 10 * 3600          # decisions 10:00 ..
DEC_TO = 15 * 3600 + 1800     # .. 15:30 ET (leaves the 2-3h horizon room)
RTH_END = 16 * 3600
HORIZONS = {"1h": 4, "2h": 8, "3h": 12}
TRIM_MULT = 1.5       # house rule: half off at +50% on the contract
DTE_YEARS = 2.5 / 365
IV_BUMP = 1.1         # implied trades a touch over realized
COST_HALF = 0.0075    # per-side friction on the option (spread half + commission)

_ALPACA = "https://data.alpaca.markets/v2/stocks"


def _dst_offset(t: int) -> int:
    """TRUE ET offset (manual US-DST): EDT from the second Sunday of March
    07:00 UTC to the first Sunday of November 06:00 UTC, else EST."""
    d = datetime.fromtimestamp(t, tz=timezone.utc)
    mar1 = datetime(d.year, 3, 1, tzinfo=timezone.utc)
    dst_on = mar1.replace(day=1 + (6 - mar1.weekday()) % 7 + 7, hour=7)
    nov1 = datetime(d.year, 11, 1, tzinfo=timezone.utc)
    dst_off = nov1.replace(day=1 + (6 - nov1.weekday()) % 7, hour=6)
    return -4 * 3600 if dst_on <= d < dst_off else -5 * 3600


def fetch_bars_alpaca(symbol: str, start: str, end: str) -> list[dict]:
    from dotenv import load_dotenv
    load_dotenv()
    hdrs = {"APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
    bars, token = [], None
    while True:
        params = {"timeframe": "15Min", "start": start, "end": end,
                  "limit": 10000, "adjustment": "raw", "feed": "iex"}
        if token:
            params["page_token"] = token
        r = _client.get(f"{_ALPACA}/{symbol}/bars", params=params, headers=hdrs)
        r.raise_for_status()
        d = r.json()
        for b in d.get("bars") or []:
            ts = int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp())
            bars.append({"t": ts, "o": b["o"], "h": b["h"], "l": b["l"],
                         "c": b["c"], "v": b.get("v") or 0})
        token = d.get("next_page_token")
        if not token:
            return bars


def fetch_bars(symbol: str) -> list[dict]:
    r = _client.get(f"{_YAHOO}/{_yahoo_sym(symbol)}",
                    params={"interval": "15m", "range": "60d",
                            "includePrePost": "true"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    return [{"t": int(t), "o": q["open"][i], "h": q["high"][i],
             "l": q["low"][i], "c": q["close"][i], "v": q["volume"][i] or 0}
            for i, t in enumerate(res.get("timestamp") or [])
            if q["open"][i] is not None and q["close"][i] is not None]


def et_secs(t: int) -> int:
    return (t + ET_OFFSET) % 86400


def et_day(t: int) -> int:
    return (t + ET_OFFSET) // 86400


# ------------------------------------------------------ Black-Scholes ------

def _cnd(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot, strike, iv, t_years, kind):
    if t_years <= 0:
        pay = spot - strike if kind == "call" else strike - spot
        return max(pay, 0.0)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t_years) / (iv * math.sqrt(t_years))
    d2 = d1 - iv * math.sqrt(t_years)
    if kind == "call":
        return spot * _cnd(d1) - strike * _cnd(d2)
    return strike * _cnd(-d2) - spot * _cnd(-d1)


def realized_iv(bars, i, days=5):
    """Annualized close-to-close 15m realized vol over ~days, from the PAST."""
    lo = max(1, i - days * 26)
    rets = [math.log(bars[k]["c"] / bars[k - 1]["c"])
            for k in range(lo, i + 1) if bars[k - 1]["c"] > 0]
    if len(rets) < 20:
        return 0.15
    sd = statistics.pstdev(rets)
    return max(sd * math.sqrt(26 * 252) * IV_BUMP, 0.05)


# ------------------------------------------------------ the walk -----------

def run_symbol(symbol: str, source="yahoo", start=None, end=None) -> dict:
    raw = (fetch_bars(symbol) if source == "yahoo"
           else fetch_bars_alpaca(symbol, start, end))
    trades, decisions = [], 0
    base_moves = defaultdict(list)      # unconditional forward moves (control)
    seen = set()
    open_until = -1                     # one position at a time

    for i in range(WINDOW, len(raw) - 1):
        t = raw[i]["t"]
        # the decision-hour gate uses TRUE ET; session bucketing stays fixed -4h
        true_et = (t + _dst_offset(t)) % 86400
        if not (DEC_FROM <= true_et < DEC_TO):
            continue
        decisions += 1
        # control set: unconditional forward move from every decision bar
        for hz, nb in HORIZONS.items():
            j = i + nb
            if j < len(raw) and et_day(raw[j]["t"]) == et_day(t):
                base_moves[hz].append(raw[j]["c"] / raw[i]["c"] - 1)

        if i <= open_until:
            continue
        past = _scrub_bars([dict(b) for b in raw[max(0, i - WINDOW + 1):i + 1]])
        try:
            read = tape.read_tape(past, ticker=symbol)
        except Exception:
            continue
        ds = read.get("day_shape")
        if read["stage"] == "triggered" and read["bias"]:
            kind, side = "tape_triggered", read["bias"]
        elif ds:
            kind = "reversal_day"
            side = "long" if ds["shape"].startswith("bull") else "short"
        else:
            continue
        key = (et_day(t), kind, side)
        if key in seen:
            continue
        seen.add(key)

        nxt = raw[i + 1]
        if et_day(nxt["t"]) != et_day(t):
            continue                     # signal at the close fills tomorrow: skip
        entry = nxt["o"]
        day = et_day(t)
        day_bars = [b for b in raw[i + 1:] if et_day(b["t"]) == day
                    and et_secs(b["t"]) < RTH_END]
        if len(day_bars) < 2:
            continue
        dirn = 1 if side == "long" else -1

        # model features, all from the PAST window only
        vws = tape.session_vwap(past)
        sess_idx = [k for k in range(len(past)) if et_day(past[k]["t"]) == day]
        hold = 0
        for k in reversed(sess_idx):
            if (past[k]["c"] > vws[k]["vwap"]) == (side == "long"):
                hold += 1
            else:
                break
        sig_last = vws[-1]["sigma"]
        cap_t = (ds or {}).get("capitulation_t")
        row = {"symbol": symbol, "t": t, "day": day, "kind": kind, "side": side,
               "cap_age_h": None if not cap_t else round((t - cap_t) / 3600, 2),
               "entry": round(entry, 2), "et_hour": round(true_et / 3600, 2),
               "capitulation_x": (ds or {}).get("capitulation_x"),
               "checklist_done": (read.get("checklist") or {}).get("done"),
               "hold_bars": hold, "sess_bar": len(sess_idx),
               "dist_sigma": None if not sig_last else
               round((past[-1]["c"] - vws[-1]["vwap"]) / sig_last, 2),
               "target": read.get("target")}
        # model-free forward moves (direction-signed, raw + sigma units)
        sig15 = statistics.pstdev([math.log(past[k]["c"] / past[k - 1]["c"])
                                   for k in range(1, len(past))]) or 1e-6
        for hz, nb in HORIZONS.items():
            j = i + nb
            ok = j < len(raw) and et_day(raw[j]["t"]) == day
            mv = (raw[j]["c"] / entry - 1) * dirn if ok else None
            row[f"mv_{hz}"] = None if mv is None else round(mv * 1e4, 1)   # bps
            row[f"sg_{hz}"] = None if mv is None else round(mv / (sig15 * math.sqrt(nb)), 2)
        row["mv_eod"] = round((day_bars[-1]["c"] / entry - 1) * dirn * 1e4, 1)

        # house option sim: ATM, ~2.5 DTE, IV from past realized
        iv = realized_iv(raw, i)
        strike = round(entry)
        okind = "call" if side == "long" else "put"
        prem0 = bs_price(entry, strike, iv, DTE_YEARS, okind) * (1 + COST_HALF)
        if prem0 > 0.05:
            trimmed, cash, half = None, 0.0, 0.5
            tt = DTE_YEARS
            for b in day_bars:
                tt -= STEP / (365 * 86400)
                mark = bs_price(b["c"], strike, iv, max(tt, 1e-6), okind) * (1 - COST_HALF)
                if trimmed is None and mark >= TRIM_MULT * prem0:
                    cash += half * mark
                    trimmed = b["t"]
                    row["trim_et"] = round(et_secs(b["t"]) / 3600, 2)
            final = bs_price(day_bars[-1]["c"], strike, iv,
                             max(tt, 1e-6), okind) * (1 - COST_HALF)
            cash += (half if trimmed else 1.0) * final
            row["opt_pnl_pct"] = round((cash / prem0 - 1) * 100, 1)
            row["trimmed"] = trimmed is not None
        else:
            row["opt_pnl_pct"] = None
            row["trimmed"] = None
        open_until = i + max(HORIZONS.values())
        trades.append(row)

    return {"symbol": symbol, "bars": len(raw), "decisions": decisions,
            "trades": trades, "base_moves": {k: v for k, v in base_moves.items()}}


# ------------------------------------------------------ statistics ---------

def binom_z(hits, n, p=0.5):
    if n == 0:
        return 0.0
    return (hits - n * p) / math.sqrt(n * p * (1 - p))


def boot_ci(vals, stat=statistics.mean, n=4000, seed=7):
    if not vals:
        return (None, None)
    rng = random.Random(seed)
    reps = sorted(stat(rng.choices(vals, k=len(vals))) for _ in range(n))
    return (round(reps[int(0.025 * n)], 2), round(reps[int(0.975 * n)], 2))


def summarize(all_trades, base_moves):
    out = {}
    for hz in list(HORIZONS) + ["eod"]:
        mvs = [t[f"mv_{hz}"] for t in all_trades if t.get(f"mv_{hz}") is not None]
        if not mvs:
            continue
        hits = sum(1 for m in mvs if m > 0)
        base = base_moves.get(hz, [])
        base_up = (sum(1 for m in base if m > 0) / len(base)) if base else None
        out[hz] = {"n": len(mvs), "hit": round(hits / len(mvs), 3),
                   "z_vs_coin": round(binom_z(hits, len(mvs)), 2),
                   "mean_bps": round(statistics.mean(mvs), 1),
                   "ci95_bps": boot_ci(mvs),
                   "base_up_rate": None if base_up is None else round(base_up, 3)}
    pnls = [t["opt_pnl_pct"] for t in all_trades if t.get("opt_pnl_pct") is not None]
    if pnls:
        wins = [p for p in pnls if p > 0]
        losses = [-p for p in pnls if p < 0]
        out["option_sim"] = {
            "n": len(pnls), "win_rate": round(len(wins) / len(pnls), 3),
            "mean_pnl_pct": round(statistics.mean(pnls), 1),
            "median_pnl_pct": round(statistics.median(pnls), 1),
            "profit_factor": round(sum(wins) / sum(losses), 2) if losses else None,
            "trim_rate": round(sum(1 for t in all_trades if t.get("trimmed")) /
                               len(pnls), 3),
            "ci95_mean_pnl": boot_ci(pnls)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--source", default="yahoo", choices=["yahoo", "alpaca"])
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--out", default="scripts/out")
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    all_trades, base_moves = [], defaultdict(list)
    for sym in args.symbols.split(","):
        r = run_symbol(sym.strip(), args.source, args.start, args.end)
        print(f"{r['symbol']}: {r['bars']} bars, {r['decisions']} decision points, "
              f"{len(r['trades'])} trades")
        all_trades += r["trades"]
        for k, v in r["base_moves"].items():
            base_moves[k] += [x * 1e4 for x in v]

    report = {"total_trades": len(all_trades),
              "overall": summarize(all_trades, base_moves)}
    for dim in ("kind", "side", "symbol"):
        report[f"by_{dim}"] = {
            val: summarize([t for t in all_trades if t[dim] == val], base_moves)
            for val in sorted({t[dim] for t in all_trades})}
    report["by_hour"] = {
        h: summarize([t for t in all_trades if int(t["et_hour"]) == h], base_moves)
        for h in sorted({int(t["et_hour"]) for t in all_trades})}

    with open(outdir / "tape_trades.csv", "w", newline="") as f:
        if all_trades:
            w = csv.DictWriter(f, fieldnames=sorted({k for t in all_trades for k in t}))
            w.writeheader()
            w.writerows(all_trades)
    (outdir / "tape_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report["overall"], indent=2))
    print("by_kind:", json.dumps(report["by_kind"], indent=2))


if __name__ == "__main__":
    main()
