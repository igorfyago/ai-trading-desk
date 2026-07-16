"""Market-data helpers shared by the research, analyst, and quoting agents.

Everything reads from the bundled demo DB so the whole repo works offline;
the functions are the seam where a live feed (the options-flow-analytics
API, a broker API) would plug in.
"""

import json
import math
from datetime import date, datetime, timezone

from common import db
from common.db import get_connection

# Production (options-flow-analytics) and demo scales differ; normalize both ways.
_LIGHTS = {"amber": "yellow"}

# Tickers served from another feed when no native rows exist. XSP (mini-SPX)
# rides SPY dealer positioning - same S&P complex, levels track ~1:1.
ALIASES = {"XSP": ("SPY", "levels read from SPY dealer positioning (same S&P complex)")}


def resolve_feed(ticker: str) -> tuple[str, str | None]:
    """(feed_ticker, note) - native if data exists, else the alias source."""
    t = ticker.upper()
    if t in ALIASES:
        probe = _native_latest(t)
        if probe is None:
            return ALIASES[t][0], ALIASES[t][1]
    return t, None


def _norm_score(score: float | None) -> float | None:
    """Production emits -1..1; the demo uses -100..100. Normalize to -100..100."""
    if score is None:
        return None
    return round(score * 100, 1) if abs(score) <= 1.5 else round(score, 1)


def _pg_latest_snapshot(ticker: str) -> dict | None:
    rows = db.run_readonly(
        "SELECT timestamp, ticker, expiry, spot, regime, net_gex_total, abs_gex_total,"
        " gamma_flip, net_delta_exposure, atm_iv, vix_current, signal_score, traffic_light"
        " FROM gex_dex_snapshots WHERE ticker = %s ORDER BY timestamp DESC LIMIT 1",
        (ticker.upper(),),
    )
    if not rows:
        return None
    (ts, tick, expiry, spot, regime, net_gex, abs_gex, flip, net_dex,
     atm_iv, vix, score, light) = rows[0]
    return {
        "captured_at": ts.isoformat(), "ticker": tick, "expiry": str(expiry),
        "spot": spot, "regime": regime, "net_gex_total": net_gex,
        "abs_gex_total": abs_gex, "gamma_flip": flip, "net_dex_total": net_dex,
        "atm_iv": atm_iv, "vix": vix, "signal_score": _norm_score(score),
        "traffic_light": _LIGHTS.get(light, light),
    }


def _native_latest(ticker: str) -> dict | None:
    """Most recent dealer-positioning snapshot for a ticker.

    Live options-flow-analytics Postgres when DATABASE_URL is set (production),
    the seeded demo SQLite otherwise (dev) — same dict shape either way.
    """
    if db.using_live_db():
        return _pg_latest_snapshot(ticker)
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM snapshots WHERE ticker = ? ORDER BY captured_at DESC LIMIT 1",
        (ticker.upper(),),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM snapshots LIMIT 0").description]
    conn.close()
    return dict(zip(cols, row))


def latest_snapshot(ticker: str) -> dict | None:
    """Most recent dealer-positioning snapshot; XSP falls back to the SPY feed
    (flagged via 'levels_note') until the collector carries it natively."""
    feed, note = resolve_feed(ticker)
    snap = _native_latest(feed)
    if snap and note:
        snap["ticker"] = ticker.upper()
        snap["levels_note"] = note
    return snap


def gex_profile(ticker: str) -> list[dict]:
    """Per-strike GEX/DEX profile of the latest snapshot."""
    ticker, _ = resolve_feed(ticker)
    if db.using_live_db():
        rows = db.run_readonly(
            "SELECT gex_per_strike FROM gex_dex_snapshots WHERE ticker = %s"
            " ORDER BY timestamp DESC LIMIT 1", (ticker.upper(),))
        if not rows:
            return []
        raw = rows[0][0]
        strikes = raw if isinstance(raw, list) else json.loads(raw)
        return sorted(
            ({"strike": s.get("strike"), "gex": s.get("net_gex"), "dex": s.get("net_dex"),
              "call_oi": 0, "put_oi": 0} for s in strikes),
            key=lambda s: s["strike"] or 0,
        )
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT sl.strike, sl.gex, sl.dex, sl.call_oi, sl.put_oi
        FROM strike_levels sl
        JOIN snapshots s ON s.id = sl.snapshot_id
        WHERE s.ticker = ?
          AND s.captured_at = (SELECT MAX(captured_at) FROM snapshots WHERE ticker = ?)
        ORDER BY sl.strike
        """,
        (ticker.upper(), ticker.upper()),
    ).fetchall()
    conn.close()
    return [
        {"strike": r[0], "gex": r[1], "dex": r[2], "call_oi": r[3], "put_oi": r[4]}
        for r in rows
    ]


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black_scholes(spot: float, strike: float, dte_days: float, iv: float,
                  kind: str, rate: float = 0.045) -> dict:
    """Black-Scholes price and greeks for a European option.

    Good enough for indicative quotes; a real desk would use a vol surface.
    """
    t = max(dte_days, 0.5) / 365.0
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + iv**2 / 2) * t) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    pdf_d1 = math.exp(-d1**2 / 2) / math.sqrt(2 * math.pi)

    if kind.lower().startswith("c"):
        price = spot * _norm_cdf(d1) - strike * math.exp(-rate * t) * _norm_cdf(d2)
        delta = _norm_cdf(d1)
    else:
        price = strike * math.exp(-rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1

    return {
        "price": round(price, 2),
        "delta": round(delta, 3),
        "gamma": round(pdf_d1 / (spot * iv * sqrt_t), 5),
        "theta_per_day": round(-(spot * pdf_d1 * iv) / (2 * sqrt_t) / 365, 3),
        "vega_per_pt": round(spot * pdf_d1 * sqrt_t / 100, 3),
    }


def expected_move(spot: float, iv: float, dte_days: float) -> float:
    """One-sigma expected move in dollars over the given horizon."""
    return round(spot * iv * math.sqrt(max(dte_days, 0.5) / 365.0), 2)


def days_to(expiry_iso: str) -> float:
    d = date.fromisoformat(expiry_iso)
    return max((d - datetime.now(timezone.utc).date()).days, 0.5)


# Live spot may only be blended into snapshot STRUCTURE when the two agree
# (2% band). Wider gap = the structure is stale or it's demo seed data —
# mixing a live 751 spot with 620-level walls would produce nonsense trades.
SANE_SPOT_GAP = 0.02


def live_spot(ticker: str) -> dict | None:
    """Freshest spot from the shared live feed (None when no feed is up).
    The alias rule applies: XSP reads the SPY feed."""
    from common import quotes

    feed, _ = resolve_feed(ticker)
    return quotes.get_spot(feed)


def blendable_spot(ticker: str, snap: dict) -> dict | None:
    """live_spot() only if it's real-time AND coherent with the snapshot."""
    live = live_spot(ticker)
    if (live and not live["delayed"] and snap.get("spot")
            and abs(live["price"] / snap["spot"] - 1) < SANE_SPOT_GAP):
        return live
    return None


def live_gex(ticker: str) -> dict | None:
    """The dealer-gamma picture recomputed against the LIVE spot.

    Open interest only changes overnight, so intraday the structure (walls,
    flip) is slow while gamma itself moves with spot. Between collector
    snapshots we re-scale each strike's stored GEX by
    gamma(strike, live_spot) / gamma(strike, snap_spot) x (S1/S0)^2 —
    the same approximation the retail GEX products ship as "live".
    """
    snap = latest_snapshot(ticker)
    if snap is None:
        return None
    live = blendable_spot(ticker, snap)
    if live is None:
        return None
    s0, s1 = snap["spot"], live["price"]
    iv = snap["atm_iv"] or 0.15
    dte = days_to(str(snap["expiry"]))
    profile = gex_profile(ticker)

    net = 0.0
    if profile and s0:
        for row in profile:
            k = row.get("strike")
            if not k:
                continue
            g0 = black_scholes(s0, k, dte, iv, "call")["gamma"]
            g1 = black_scholes(s1, k, dte, iv, "call")["gamma"]
            ratio = (g1 * s1 * s1) / (g0 * s0 * s0) if g0 > 1e-9 else 1.0
            net += (row.get("gex") or 0) * min(max(ratio, 0.0), 10.0)
    else:
        net = snap["net_gex_total"]

    flip = snap["gamma_flip"] or s1
    return {
        "ticker": snap["ticker"],
        "spot_live": s1, "spot_source": live["source"], "spot_delayed": live["delayed"],
        "structure_as_of": snap["captured_at"],
        "net_gex_total_live": round(net),
        "regime_live": "positive_gamma" if net >= 0 else "negative_gamma",
        "gamma_flip": flip,
        "side": "above_flip" if s1 >= flip else "below_flip",
        "distance_to_flip": round(s1 - flip, 2),
        "note": "structure (OI/walls/flip) from the latest chain snapshot; "
                "gamma re-marked to the live spot",
    }
