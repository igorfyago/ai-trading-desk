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
    # The flip must come from the CHAIN, never from the quote we just moved.
    # Comparing side against gl["gamma_flip"] would pass even if the two were
    # the same number, which is exactly the bug this file failed to catch.
    assert gl["gamma_flip"] == snap["gamma_flip"]
    if snap["gamma_flip"] is not None:
        assert gl["gamma_flip"] != gl["spot_live"]
        assert gl["side"] == ("above_flip" if gl["spot_live"] >= snap["gamma_flip"]
                              else "below_flip")
        assert gl["distance_to_flip"] == pytest.approx(
            gl["spot_live"] - snap["gamma_flip"], abs=0.01)


def test_a_chain_with_no_flip_says_so_instead_of_quoting_spot(monkeypatch):
    """A one-sided chain has no zero-cross and the collector stores NULL. That
    is a market fact, not a gap to paper over: the desk used to substitute the
    live spot, so it reported a flip sitting exactly on the price, side always
    'above_flip' and distance always 0.00, in every market condition."""
    from common import quotes

    snap = dict(market.latest_snapshot("SPY"))
    snap["gamma_flip"] = None
    monkeypatch.setattr(market, "latest_snapshot", lambda t, **kw: snap)
    # must stay coherent with the snapshot or blendable_spot refuses the quote
    live_px = round(snap["spot"] * 1.01, 2)
    monkeypatch.setattr(quotes, "get_spot", lambda t: _fake_live(live_px))

    gl = market.live_gex("SPY")
    assert gl is not None
    assert gl["gamma_flip"] is None          # never the spot
    assert gl["side"] is None                # no direction without a level
    assert gl["distance_to_flip"] is None    # not a confident 0.00
    assert gl["flip_note"] and "no gamma flip" in gl["flip_note"]


def test_live_gex_none_when_feed_down():
    assert market.live_gex("SPY") is None   # QUOTES_PROVIDER=off in the suite
