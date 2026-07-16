"""The trade engine: every regime branch produces a coherent structure.

The engine is the piece that must never be wrong-shaped — the LLM narrates
whatever this returns.
"""

import pytest

from common import market, signals


def _force(monkeypatch, regime, score, spot_vs_flip="below"):
    real = market.latest_snapshot

    def fake(ticker):
        snap = real(ticker)
        snap["regime"] = regime
        snap["signal_score"] = score
        flip = snap["spot"] * (1.01 if spot_vs_flip == "below" else 0.99)
        snap["gamma_flip"] = round(flip, 2)
        return snap

    monkeypatch.setattr(signals.market, "latest_snapshot", fake)


def test_short_gamma_below_flip_is_put_debit_spread(monkeypatch):
    _force(monkeypatch, "negative_gamma", -40, spot_vs_flip="below")
    r = signals.recommend_trade("SPY")
    assert r["structure"] == "put debit spread"
    buy, sell = r["legs"]
    assert (buy["side"], sell["side"]) == ("buy", "sell")
    assert buy["kind"] == sell["kind"] == "put"
    assert buy["strike"] > sell["strike"]
    assert buy["indicative_price"] > sell["indicative_price"]  # debit


def test_short_gamma_above_flip_is_call_debit_spread(monkeypatch):
    _force(monkeypatch, "negative_gamma", 40, spot_vs_flip="above")
    r = signals.recommend_trade("SPY")
    assert r["structure"] == "call debit spread"
    buy, sell = r["legs"]
    assert buy["kind"] == sell["kind"] == "call"
    assert buy["strike"] < sell["strike"]


@pytest.mark.parametrize("score,structure,n_legs", [
    (50, "put credit spread", 2),
    (-50, "call credit spread", 2),
    (0, "iron condor", 4),
])
def test_long_gamma_branches(monkeypatch, score, structure, n_legs):
    _force(monkeypatch, "positive_gamma", score)
    r = signals.recommend_trade("SPY")
    assert r["structure"] == structure
    assert len(r["legs"]) == n_legs


def test_credit_spread_collects_premium(monkeypatch):
    _force(monkeypatch, "positive_gamma", 50)
    r = signals.recommend_trade("SPY")
    sell = next(leg for leg in r["legs"] if leg["side"] == "sell")
    buy = next(leg for leg in r["legs"] if leg["side"] == "buy")
    assert sell["indicative_price"] >= buy["indicative_price"]


def test_every_recommendation_carries_risk_fields(monkeypatch):
    _force(monkeypatch, "negative_gamma", -40)
    r = signals.recommend_trade("QQQ")
    assert r["invalidation"].startswith("Exit if")
    assert "1%" in r["sizing"]
    assert "disclaimer" not in r          # the game never breaks its own frame
    lo, hi = r["expected_move_band"]
    assert lo < r["spot"] < hi


def test_unknown_ticker_is_clean_error():
    assert "error" in signals.recommend_trade("TSLA")
