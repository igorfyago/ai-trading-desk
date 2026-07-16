"""Market-data helpers shared by the research, analyst, and quoting agents.

Everything reads from the bundled demo DB so the whole repo works offline;
the functions are the seam where a live feed (the options-flow-analytics
API, a broker API) would plug in.
"""

import math
from datetime import date, datetime, timezone

from common.db import get_connection


def latest_snapshot(ticker: str) -> dict | None:
    """Most recent dealer-positioning snapshot for a ticker."""
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


def gex_profile(ticker: str) -> list[dict]:
    """Per-strike GEX/DEX profile of the latest snapshot."""
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
