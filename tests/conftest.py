"""Shared test setup.

Three invariants for the whole suite:
1. No test ever touches the real OpenAI API unless explicitly opted in
   (RUN_LIVE=1): a dummy key is planted BEFORE any langchain import, and
   load_dotenv(override=False) in the app modules won't replace it.
2. No test touches the real demo database: DB_PATH is redirected to a
   temp file for the whole session (seeded once, deterministically).
3. No test touches the live quote feed: QUOTES_PROVIDER=off, so agents fall
   back to snapshot spots and stay deterministic.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "test-key-not-real")
os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("QUOTES_PROVIDER", "off")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def isolated_db(tmp_path_factory):
    """Point every get_connection() at a throwaway, freshly-seeded DB."""
    import common.db as db

    original = db.DB_PATH
    db.DB_PATH = tmp_path_factory.mktemp("data") / "test-desk.db"
    yield
    db.DB_PATH = original


@pytest.fixture()
def db_conn():
    from common.db import get_connection

    conn = get_connection()
    yield conn
    conn.close()


def live_only():
    return pytest.mark.skipif(
        os.getenv("RUN_LIVE") != "1",
        reason="live API test — set RUN_LIVE=1 (spends real tokens)",
    )
