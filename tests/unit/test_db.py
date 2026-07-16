"""Seed determinism and schema shape of the demo database."""


def test_seed_is_deterministic_and_complete(db_conn):
    assert db_conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 90
    tickers = {r[0] for r in db_conn.execute("SELECT DISTINCT ticker FROM snapshots")}
    assert tickers == {"SPY", "QQQ", "IWM"}


def test_both_regimes_present(db_conn):
    regimes = {r[0] for r in db_conn.execute("SELECT DISTINCT regime FROM snapshots")}
    assert regimes == {"positive_gamma", "negative_gamma"}


def test_every_snapshot_has_strikes_and_walls(db_conn):
    orphan_strikes = db_conn.execute(
        "SELECT COUNT(*) FROM snapshots s WHERE NOT EXISTS "
        "(SELECT 1 FROM strike_levels sl WHERE sl.snapshot_id = s.id)"
    ).fetchone()[0]
    assert orphan_strikes == 0
    walls_per_snap = db_conn.execute(
        "SELECT COUNT(*) * 1.0 / (SELECT COUNT(*) FROM snapshots) FROM walls"
    ).fetchone()[0]
    assert walls_per_snap == 2.0  # exactly one call wall + one put wall each


def test_business_tables_exist(db_conn):
    tables = {r[0] for r in db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"snapshots", "strike_levels", "walls",
            "callbacks", "appointments", "quotes"} <= tables


def test_sane_value_ranges(db_conn):
    lo, hi = db_conn.execute("SELECT MIN(atm_iv), MAX(atm_iv) FROM snapshots").fetchone()
    assert 0.05 < lo and hi < 0.60
    lo, hi = db_conn.execute("SELECT MIN(signal_score), MAX(signal_score) FROM snapshots").fetchone()
    assert -100 <= lo and hi <= 100
