"""Opt-in live tests — real API calls, real (small) cost.

Run with:  RUN_LIVE=1 pytest tests/integration/test_live.py -v
Reads the real keys from .env directly (the dummy key from conftest is
replaced only inside these tests).
"""

import os

import pytest
from dotenv import dotenv_values

pytestmark = pytest.mark.skipif(os.getenv("RUN_LIVE") != "1",
                                reason="live API test — set RUN_LIVE=1 (spends real tokens)")


@pytest.fixture(autouse=True)
def real_keys(monkeypatch):
    env = dotenv_values(".env")
    key = env.get("OPENAI_API_KEY")
    if not key:
        pytest.skip("no OPENAI_API_KEY in .env")
    monkeypatch.setenv("OPENAI_API_KEY", key)


def test_realtime_session_mints_ephemeral_secret():
    import httpx

    resp = httpx.post(
        "https://api.openai.com/v1/realtime/client_secrets",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={"session": {"type": "realtime", "model": "gpt-realtime-2.1",
                          "audio": {"output": {"voice": "cedar"}}}},
        timeout=20,
    )
    assert resp.status_code == 200
    assert resp.json()["value"].startswith("ek_")
