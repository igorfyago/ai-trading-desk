"""Replay & backtest: the desk re-decides at a past moment WITHOUT seeing the
future, then the tape grades it by the house rules.

Blindfold: signals.recommend_trade(ticker, as_of=T) reads only snapshots at or
before T — no live feed, no future bars, no exceptions.

Grading (the desk's own management, applied mechanically):
  entry  = the engine's BS-estimated contract price at T
  marks  = Black-Scholes on each later bar close, IV frozen at the snapshot's
           ATM vol, DTE decaying in real time
  trim   = sell HALF at the first mark >= 1.5x entry
  runner = rides to expiry (intrinsic) or the end of available tape
  no stop, ever — the premium is the risk

Honesty: marks are model estimates, not NBBO prints; IV is frozen; history
starts when the collector started. This validates the LOGIC of the desk —
direction, levels, rule behavior — it is not an execution-quality study.
"""

from datetime import datetime, timedelta, timezone

from common import market, quotes, signals


def _parse(ts: str) -> datetime:
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def moments(ticker: str, limit: int = 800) -> list[str]:
    return market.snapshot_moments(ticker, limit)


def _bars_for(ticker: str, interval: str = "15m") -> list[dict]:
    feed, _ = market.resolve_feed(ticker)
    payload = quotes.get_bars(feed, interval, limit=1000)
    return (payload or {}).get("bars") or []


def score_path(rec: dict, bars: list[dict]) -> dict:
    """Walk the tape FORWARD from the decision and apply the house rules."""
    x = rec["execution"]
    t0 = _parse(rec["as_of"]).timestamp()
    entry = x["entry_option_price_est"]
    strike, kind = x["strike"], x["kind"]
    iv = rec.get("snapshot_iv") or 0.2
    expiry_dt = datetime.fromisoformat(str(x["expiry"])).replace(
        hour=20, minute=0, tzinfo=timezone.utc)          # ~16:00 ET close
    cp = x.get("contract_plan") or {}
    contracts = cp.get("contracts_now") or 1
    clip = cp.get("now_usd") or cp.get("budget_usd") or 2000

    path = [b for b in bars if b["t"] > t0 and b["t"] <= expiry_dt.timestamp()]
    if not path:
        return {"gradable": False,
                "note": "no tape after this moment yet - pick an older one"}
    if abs(path[0]["c"] / x["entry_underlying"] - 1) > 0.05:
        return {"gradable": False,
                "note": "tape and snapshot disagree by >5% (demo data or a "
                        "stale feed) - grading this would be fiction"}

    marks, trim, last = [], None, None
    for b in path:
        rem_days = max((expiry_dt.timestamp() - b["t"]) / 86400.0, 0.02)
        px = market.black_scholes(b["c"], strike, rem_days, iv, kind)["price"]
        marks.append({"t": b["t"], "px": round(px, 2), "spot": b["c"]})
        if trim is None and px >= 1.5 * entry:
            trim = {"t": b["t"], "px": round(px, 2), "spot": b["c"]}
        last = {"t": b["t"], "px": round(px, 2), "spot": b["c"]}

    expired = path[-1]["t"] >= expiry_dt.timestamp() - 1800
    if expired:   # settle the runner at intrinsic
        s = path[-1]["c"]
        intrinsic = max(s - strike, 0.0) if kind == "call" else max(strike - s, 0.0)
        last = {"t": path[-1]["t"], "px": round(intrinsic, 2), "spot": s}

    if trim:
        half = contracts / 2.0
        pnl = (trim["px"] - entry) * 100 * half + (last["px"] - entry) * 100 * (contracts - half)
    else:
        pnl = (last["px"] - entry) * 100 * contracts
    invested = entry * 100 * contracts

    return {
        "gradable": True,
        "entry": {"t": t0, "px": entry, "spot": x["entry_underlying"]},
        "trim": trim, "final": last, "expired": expired,
        "contracts": contracts, "invested_usd": round(invested, 2),
        "pnl_usd": round(pnl, 2),
        "pnl_pct": round(pnl / invested * 100, 1) if invested else 0.0,
        "clip_usd": clip,
        "marks": marks[:: max(1, len(marks) // 120)],   # thin for the wire
    }


def run(ticker: str, at: str, interval: str = "15m") -> dict:
    rec = signals.recommend_trade(ticker, as_of=at)
    if "error" in rec:
        return {"error": rec["error"]}
    out = {
        "at": at, "ticker": rec["ticker"],
        "decided_from": rec["as_of"],                   # the snapshot actually used
        "bias": rec["bias"], "structure": rec["structure"],
        "plain_english": rec["plain_english"],
        "execution": {k: rec["execution"].get(k) for k in
                      ("kind", "strike", "expiry", "entry_option_price_est",
                       "entry_underlying", "thesis_label", "thesis_reference",
                       "contract_plan")},
        "verdict": score_path(rec, _bars_for(ticker, interval)),
    }
    return out


def sweep(ticker: str, start: str, end: str, step_minutes: int = 60,
          max_runs: int = 120) -> dict:
    """One decision per step across the range; aggregate how the rules fared."""
    t_start, t_end = _parse(start), _parse(end)
    bars = _bars_for(ticker)
    results = []
    seen_snapshots: set[str] = set()
    t = t_start
    while t <= t_end and len(results) < max_runs:
        at = t.isoformat()
        rec = signals.recommend_trade(ticker, as_of=at)
        t += timedelta(minutes=step_minutes)
        if "error" in rec:
            continue
        if rec["as_of"] in seen_snapshots:
            continue                       # same snapshot = same decision
        seen_snapshots.add(rec["as_of"])
        v = score_path(rec, bars)
        if not v.get("gradable"):
            continue
        results.append({
            "at": at, "decided_from": rec["as_of"], "bias": rec["bias"],
            "kind": rec["execution"]["kind"],
            "strike": rec["execution"]["strike"],
            "trimmed": bool(v["trim"]), "expired": v["expired"],
            "pnl_usd": v["pnl_usd"], "pnl_pct": v["pnl_pct"],
        })
    n = len(results)
    wins = sum(1 for r in results if r["pnl_usd"] > 0)
    trims = sum(1 for r in results if r["trimmed"])
    total = round(sum(r["pnl_usd"] for r in results), 2)
    return {
        "ticker": ticker.upper(), "n": n,
        "wins": wins, "win_rate": round(wins / n * 100, 1) if n else None,
        "trim_rate": round(trims / n * 100, 1) if n else None,
        "total_pnl_usd": total,
        "avg_pnl_usd": round(total / n, 2) if n else None,
        "results": results,
    }
