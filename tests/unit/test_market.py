"""Property tests for the pricing math — the numbers agents narrate."""

import math

import pytest

from common import market


SPOT, IV, DTE, RATE = 600.0, 0.20, 10.0, 0.045


def test_put_call_parity():
    c = market.black_scholes(SPOT, 600, DTE, IV, "call")["price"]
    p = market.black_scholes(SPOT, 600, DTE, IV, "put")["price"]
    t = DTE / 365
    parity = SPOT - 600 * math.exp(-RATE * t)
    assert c - p == pytest.approx(parity, abs=0.02)  # legs rounded to cents


def test_atm_call_delta_near_half():
    d = market.black_scholes(SPOT, 600, DTE, IV, "call")["delta"]
    assert 0.45 < d < 0.60


def test_delta_bounds_and_signs():
    for strike in (540, 580, 600, 620, 660):
        c = market.black_scholes(SPOT, strike, DTE, IV, "call")
        p = market.black_scholes(SPOT, strike, DTE, IV, "put")
        assert 0.0 <= c["delta"] <= 1.0
        assert -1.0 <= p["delta"] <= 0.0
        assert c["gamma"] > 0 and p["theta_per_day"] <= 0


def test_call_price_decreases_with_strike():
    prices = [market.black_scholes(SPOT, k, DTE, IV, "call")["price"]
              for k in (580, 590, 600, 610, 620)]
    assert prices == sorted(prices, reverse=True)


def test_deep_itm_call_worth_at_least_intrinsic():
    price = market.black_scholes(SPOT, 500, DTE, IV, "call")["price"]
    assert price >= SPOT - 500  # discounting makes it slightly above


def test_expected_move_scales_with_sqrt_time():
    one = market.expected_move(SPOT, IV, 1)
    four = market.expected_move(SPOT, IV, 4)
    assert four == pytest.approx(2 * one, rel=0.01)


def test_latest_snapshot_shape():
    snap = market.latest_snapshot("SPY")
    assert snap["ticker"] == "SPY"
    assert {"spot", "regime", "gamma_flip", "atm_iv", "signal_score"} <= snap.keys()
    assert market.latest_snapshot("NOPE") is None


def test_gex_profile_sorted_and_nonempty():
    prof = market.gex_profile("QQQ")
    assert len(prof) == 17
    strikes = [row["strike"] for row in prof]
    assert strikes == sorted(strikes)
