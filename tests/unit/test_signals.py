"""The trade engine: every regime branch produces a coherent structure.

The engine is the piece that must never be wrong-shaped — the LLM narrates
whatever this returns.
"""

import pytest

from common import market, signals


def _force(monkeypatch, regime, score, spot_vs_flip="below"):
    real = market.latest_snapshot

    def fake(ticker, as_of=None):
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


def test_degenerate_flip_never_mirrors_spot(monkeypatch):
    """A missing flip (or one sitting on price) must NOT default to spot —
    that made 'above the flip' a tautology and every day 'bullish'. The desk
    signal picks the side; a real wall carries the thesis."""
    real = market.latest_snapshot

    def fake_none(ticker, as_of=None):
        snap = real(ticker)
        snap["regime"] = "negative_gamma"
        snap["signal_score"] = -40
        snap["gamma_flip"] = None
        return snap

    monkeypatch.setattr(signals.market, "latest_snapshot", fake_none)
    r = signals.recommend_trade("SPY")
    assert r["bias"] == "bearish momentum"           # score decides, not spot>=spot
    x = r["execution"]
    assert x["thesis_kind"] == "wall"
    assert x["thesis_reference"] != round(r["spot"], 2)
    assert x["kind"] == "put"
    assert "wall" in r["plain_english"]

    def fake_onprice(ticker, as_of=None):
        snap = real(ticker)
        snap["regime"] = "negative_gamma"
        snap["signal_score"] = -40
        snap["gamma_flip"] = snap["spot"]            # the chicken-and-egg case
        return snap

    monkeypatch.setattr(signals.market, "latest_snapshot", fake_onprice)
    r2 = signals.recommend_trade("SPY")
    assert r2["bias"] == "bearish momentum"
    assert r2["execution"]["thesis_kind"] == "wall"


def test_replay_blindfold_and_grading(monkeypatch):
    """as_of must pin the snapshot to the past (no live blend), and the
    grader must apply the house rules: trim half at +50%, runner rides."""
    from common import replay

    ms = replay.moments("SPY")
    assert len(ms) >= 5
    mid = ms[len(ms) // 2]
    rec = signals.recommend_trade("SPY", as_of=mid)
    assert rec["as_of"] <= mid                    # never a future snapshot

    # synthetic tape FROM the real decision moment (clock consistency matters:
    # a fake earlier t0 would inflate remaining DTE and fake a trim)
    x = rec["execution"]
    t0 = int(replay._parse(rec["as_of"]).timestamp())
    s0 = x["entry_underlying"]
    bars = [{"t": t0 + i * 900,
             "c": s0 - (i * 0.8 if x["kind"] == "put" else -i * 0.8),
             "o": s0, "h": s0, "l": s0, "v": 1} for i in range(1, 40)]
    v = replay.score_path(rec, bars)
    assert v["gradable"] and v["trim"] is not None
    assert v["pnl_usd"] > 0                       # winner graded as a winner

    flat = [{"t": t0 + i * 900, "c": s0, "o": s0, "h": s0, "l": s0, "v": 1}
            for i in range(1, 40)]
    v2 = replay.score_path(rec, flat)
    assert v2["gradable"] and v2["trim"] is None  # theta bleeds, no trim
    assert v2["pnl_usd"] < 0
