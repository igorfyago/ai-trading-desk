"""Live-spot blending: the engine and the GEX recompute must use the live
feed when it's up and degrade to the snapshot when it isn't."""

import pytest

from common import market, signals


def _fake_live(price, delayed=False):
    return {"ticker": "SPY", "price": price, "ts": "2026-07-16T14:00:00+00:00",
            "source": "test·iex", "delayed": delayed}


def test_engine_uses_snapshot_when_feed_is_down():
    rec = signals.recommend_trade("SPY")
    assert rec["spot_source"] == "snapshot"


def test_engine_prefers_live_spot(monkeypatch):
    snap = market.latest_snapshot("SPY")
    live_px = round(snap["spot"] * 1.004, 2)
    monkeypatch.setattr(market, "live_spot", lambda t: _fake_live(live_px))
    rec = signals.recommend_trade("SPY")
    assert rec["spot"] == live_px
    assert rec["spot_source"] == "test·iex"
    assert rec["execution"]["entry_underlying"] == live_px


def test_engine_ignores_delayed_feed(monkeypatch):
    snap = market.latest_snapshot("SPY")
    monkeypatch.setattr(market, "live_spot",
                        lambda t: _fake_live(snap["spot"] + 5, delayed=True))
    rec = signals.recommend_trade("SPY")
    assert rec["spot_source"] == "snapshot"
    assert rec["spot"] == snap["spot"]


def test_live_gex_recompute_reacts_to_spot(monkeypatch):
    snap = market.latest_snapshot("SPY")
    from common import quotes

    monkeypatch.setattr(quotes, "get_spot",
                        lambda t: _fake_live(round(snap["spot"] * 1.01, 2)))
    gl = market.live_gex("SPY")
    assert gl is not None
    assert gl["spot_live"] == round(snap["spot"] * 1.01, 2)
    assert gl["regime_live"] in ("positive_gamma", "negative_gamma")
    assert gl["side"] == ("above_flip" if gl["spot_live"] >= gl["gamma_flip"]
                          else "below_flip")
    assert gl["distance_to_flip"] == pytest.approx(
        gl["spot_live"] - gl["gamma_flip"], abs=0.01)


def test_live_gex_none_when_feed_down():
    assert market.live_gex("SPY") is None   # QUOTES_PROVIDER=off in the suite
