"""Agent 2's run_sql tool: the LLM is never trusted with write access."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("agent_sql", ROOT / "agents/02_text_to_sql/main.py")
agent_sql = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_sql)

run = lambda sql: agent_sql.run_sql.invoke({"sql": sql})  # noqa: E731


def test_select_allowed():
    out = run("SELECT ticker, COUNT(*) FROM snapshots GROUP BY ticker")
    assert out.startswith("OK") and "SPY" in out


def test_cte_allowed():
    out = run("WITH t AS (SELECT ticker FROM snapshots) SELECT COUNT(*) FROM t")
    assert out.startswith("OK")


def test_dml_and_ddl_rejected():
    for evil in (
        "INSERT INTO snapshots (ticker) VALUES ('HACK')",
        "DELETE FROM snapshots",
        "DROP TABLE snapshots",
        "UPDATE snapshots SET spot = 0",
        "PRAGMA writable_schema = 1",
    ):
        assert run(evil).startswith("REJECTED")


def test_forbidden_keyword_inside_select_rejected():
    assert run("SELECT 1; DROP TABLE snapshots").startswith("REJECTED")


def test_multi_statement_rejected():
    assert run("SELECT 1; SELECT 2").startswith("REJECTED")


def test_bad_sql_returns_error_for_self_correction():
    out = run("SELECT no_such_column FROM snapshots")
    assert out.startswith("SQL ERROR")  # feedback, not an exception


def test_row_cap():
    out = run("SELECT * FROM strike_levels")
    assert "truncated" in out


def test_writes_did_not_happen(db_conn):
    assert db_conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE ticker = 'HACK'").fetchone()[0] == 0
