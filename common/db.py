"""Demo database for the desk: a normalized SQLite mirror of the
options-flow-analytics production schema (gex_dex_snapshots), seeded with
deterministic synthetic data so the repo runs for anyone with zero setup.

Set DATABASE_URL to a live options-flow-analytics Postgres to run against
real snapshots instead (Agent 2 detects it automatically).

Tables
------
snapshots       one row per (ticker, captured_at): spot, regime, totals, gamma flip
strike_levels   per-strike GEX / DEX profile for each snapshot
walls           call/put wall levels detected per snapshot
"""

import math
import os
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "desk.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL,          -- ISO timestamp (UTC)
    ticker TEXT NOT NULL,
    expiry TEXT NOT NULL,               -- option expiry date
    spot REAL NOT NULL,                 -- underlying price
    regime TEXT NOT NULL,               -- 'positive_gamma' | 'negative_gamma'
    net_gex_total REAL NOT NULL,        -- net dealer gamma exposure ($ per 1% move)
    abs_gex_total REAL NOT NULL,
    gamma_flip REAL,                    -- spot level where dealer gamma flips sign
    net_dex_total REAL,                 -- net dealer delta exposure
    atm_iv REAL,                        -- at-the-money implied volatility
    vix REAL,
    signal_score REAL,                  -- -100 (max bearish) .. +100 (max bullish)
    traffic_light TEXT                  -- 'green' | 'yellow' | 'red'
);

CREATE TABLE IF NOT EXISTS strike_levels (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    strike REAL NOT NULL,
    gex REAL NOT NULL,                  -- gamma exposure at this strike
    dex REAL NOT NULL,                  -- delta exposure at this strike
    call_oi INTEGER NOT NULL,
    put_oi INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS walls (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    kind TEXT NOT NULL,                 -- 'call' | 'put'
    strike REAL NOT NULL,
    strength REAL NOT NULL              -- absolute GEX concentration at the wall
);

CREATE TABLE IF NOT EXISTS callbacks (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    caller_name TEXT NOT NULL,
    contact TEXT NOT NULL,
    topic TEXT NOT NULL,
    preferred_time TEXT
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    patient_name TEXT NOT NULL,
    contact TEXT NOT NULL,
    service TEXT NOT NULL,
    slot TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    customer TEXT NOT NULL,
    contact TEXT NOT NULL,
    project TEXT NOT NULL,
    low_usd REAL NOT NULL,
    high_usd REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_time ON snapshots (ticker, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_levels_snapshot ON strike_levels (snapshot_id);
"""

TICKERS = {"SPY": 620.0, "QQQ": 555.0, "IWM": 225.0}
DAYS = 10
SNAPSHOTS_PER_DAY = 3


def _seed(conn: sqlite3.Connection) -> None:
    rng = random.Random(42)  # deterministic: same demo data for everyone
    base_day = datetime(2026, 7, 1, 14, 30)

    for ticker, base_spot in TICKERS.items():
        spot = base_spot
        for day in range(DAYS):
            for snap in range(SNAPSHOTS_PER_DAY):
                captured = base_day + timedelta(days=day, hours=2 * snap)
                spot *= 1 + rng.gauss(0, 0.004)
                regime_roll = rng.random()
                regime = "positive_gamma" if regime_roll > 0.35 else "negative_gamma"
                sign = 1 if regime == "positive_gamma" else -1
                net_gex = sign * rng.uniform(0.3e9, 2.5e9)
                abs_gex = abs(net_gex) * rng.uniform(1.2, 1.9)
                flip = spot * (1 - sign * rng.uniform(0.002, 0.015))
                atm_iv = rng.uniform(0.11, 0.16) if sign > 0 else rng.uniform(0.16, 0.28)
                vix = atm_iv * 100 * rng.uniform(0.95, 1.15)
                score = sign * rng.uniform(10, 80) + rng.gauss(0, 10)
                light = "green" if score > 25 else ("red" if score < -25 else "yellow")
                expiry = (captured + timedelta(days=(4 - captured.weekday()) % 7)).date()

                cur = conn.execute(
                    "INSERT INTO snapshots (captured_at, ticker, expiry, spot, regime,"
                    " net_gex_total, abs_gex_total, gamma_flip, net_dex_total, atm_iv,"
                    " vix, signal_score, traffic_light)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        captured.isoformat(),
                        ticker,
                        expiry.isoformat(),
                        round(spot, 2),
                        regime,
                        round(net_gex),
                        round(abs_gex),
                        round(flip, 2),
                        round(sign * rng.uniform(0.1e9, 1.0e9)),
                        round(atm_iv, 4),
                        round(vix, 2),
                        round(max(-100, min(100, score)), 1),
                        light,
                    ),
                )
                snapshot_id = cur.lastrowid

                # Per-strike profile: GEX concentrates near ATM, calls above / puts below.
                step = round(base_spot * 0.005, 0) or 1.0
                strikes = [round(spot + i * step, 0) for i in range(-8, 9)]
                best = {"call": (0.0, None), "put": (0.0, None)}
                for strike in strikes:
                    dist = abs(strike - spot) / spot
                    weight = math.exp(-(dist / 0.01) ** 2)
                    strike_gex = (1 if strike >= spot else -1) * weight * abs_gex / 6 * rng.uniform(0.5, 1.5)
                    strike_dex = -strike_gex * rng.uniform(0.2, 0.6)
                    conn.execute(
                        "INSERT INTO strike_levels (snapshot_id, strike, gex, dex, call_oi, put_oi)"
                        " VALUES (?,?,?,?,?,?)",
                        (
                            snapshot_id,
                            strike,
                            round(strike_gex),
                            round(strike_dex),
                            int(weight * rng.uniform(5_000, 60_000)),
                            int(weight * rng.uniform(5_000, 60_000)),
                        ),
                    )
                    kind = "call" if strike >= spot else "put"
                    if abs(strike_gex) > best[kind][0]:
                        best[kind] = (abs(strike_gex), strike)

                for kind, (strength, strike) in best.items():
                    if strike is not None:
                        conn.execute(
                            "INSERT INTO walls (snapshot_id, kind, strike, strength) VALUES (?,?,?,?)",
                            (snapshot_id, kind, strike, round(strength)),
                        )
    conn.commit()


def get_connection() -> sqlite3.Connection:
    """Open the demo DB, creating and seeding it on first use."""
    DATA_DIR.mkdir(exist_ok=True)
    fresh = not DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    if fresh:
        _seed(conn)
    return conn


def using_live_db() -> bool:
    """True when DATABASE_URL points at a live options-flow-analytics Postgres."""
    return bool(os.getenv("DATABASE_URL"))


def pg_connection():
    import psycopg

    return psycopg.connect(os.getenv("DATABASE_URL"))


def run_readonly(sql: str, params: tuple = ()) -> list[tuple]:
    """One read-only query against whichever DB is active (live PG or demo SQLite)."""
    if using_live_db():
        with pg_connection() as conn:
            return conn.execute(sql, params).fetchall()
    conn = get_connection()
    try:
        return conn.execute(sql.replace("%s", "?"), params).fetchall()
    finally:
        conn.close()


def describe_schema() -> str:
    """Human/LLM-readable schema description used by the SQL agents."""
    if using_live_db():
        rows = run_readonly(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' ORDER BY table_name, ordinal_position"
        )
        tables: dict[str, list[str]] = {}
        for table, col, dtype in rows:
            tables.setdefault(table, []).append(f"{col} {dtype}")
        return "\n\n".join(f"TABLE {t} (\n  " + ",\n  ".join(cols) + "\n)"
                           for t, cols in tables.items())
    conn = get_connection()
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    conn.close()
    return "\n\n".join(sql for _, sql in rows if sql)


if __name__ == "__main__":
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    print(f"desk.db ready at {DB_PATH} with {n} snapshots")
    conn.close()
