"""The live-quote layer, fully offline: provider selection, caching,
interval normalization, resampling, and the off switch the suite relies on."""

import time

import pytest

from common import quotes


def test_off_switch_means_no_providers(monkeypatch):
    monkeypatch.setenv("QUOTES_PROVIDER", "off")
    assert quotes.provider_order() == []
    assert quotes.get_spot("SPY") is None
    assert quotes.get_bars("SPY", "5m") is None


def test_provider_order_auto_without_keys(monkeypatch):
    monkeypatch.setenv("QUOTES_PROVIDER", "auto")
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    assert quotes.provider_order() == ["yahoo", "cboe"]


def test_provider_order_auto_with_keys(monkeypatch):
    monkeypatch.setenv("QUOTES_PROVIDER", "auto")
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    assert quotes.provider_order() == ["alpaca", "yahoo", "cboe"]


def test_interval_normalization_accepts_old_widget_values():
    assert quotes.normalize_interval("5m") == "5m"
    assert quotes.normalize_interval("240") == "4h"
    assert quotes.normalize_interval("D") == "D"
    assert quotes.normalize_interval("bogus") is None


def test_resample_aggregates_ohlcv():
    base = 1_700_000_000 - (1_700_000_000 % 180)
    bars = [
        {"t": base, "o": 10, "h": 12, "l": 9, "c": 11, "v": 100},
        {"t": base + 60, "o": 11, "h": 15, "l": 11, "c": 14, "v": 50},
        {"t": base + 120, "o": 14, "h": 14, "l": 8, "c": 9, "v": 25},
        {"t": base + 180, "o": 9, "h": 10, "l": 9, "c": 10, "v": 10},
    ]
    out = quotes._resample(bars, 3, 180)
    assert len(out) == 2
    first = out[0]
    assert (first["o"], first["h"], first["l"], first["c"], first["v"]) == (10, 15, 8, 9, 175)
    assert out[1]["t"] == base + 180


def test_spot_cache_serves_fresh_hits(monkeypatch):
    monkeypatch.setenv("QUOTES_PROVIDER", "yahoo")
    calls = {"n": 0}

    def fake_yahoo(symbol):
        calls["n"] += 1
        return {"ticker": symbol, "price": 100.0 + calls["n"], "ts": None,
                "source": "yahoo", "delayed": False}

    monkeypatch.setattr(quotes, "_spot_yahoo", fake_yahoo)
    quotes._spot_cache.clear()
    first = quotes.get_spot("SPY")
    second = quotes.get_spot("SPY")          # inside TTL: cached
    assert first == second and calls["n"] == 1


def test_fetch_spots_falls_through_providers(monkeypatch):
    monkeypatch.setenv("QUOTES_PROVIDER", "auto")
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)

    def broken_yahoo(symbol):
        raise RuntimeError("bot wall")

    def fake_cboe(symbol):
        return {"ticker": symbol, "price": 620.0, "ts": None,
                "source": "cboe·15m", "delayed": True}

    monkeypatch.setattr(quotes, "_spot_yahoo", broken_yahoo)
    monkeypatch.setattr(quotes, "_spot_cboe", fake_cboe)
    quotes._spot_cache.clear()
    out = quotes.fetch_spots(["SPY"])
    assert out["SPY"]["delayed"] is True and out["SPY"]["source"] == "cboe·15m"


def test_session_label_covers_the_24h_clock():
    # July = EDT (UTC-4); January = EST (UTC-5); weekend = the 24h tape
    assert quotes.session_label("2026-07-16T15:00:00Z") == "rth"        # 11:00 ET
    assert quotes.session_label("2026-07-16T21:30:00Z") == "post"      # 17:30 ET
    assert quotes.session_label("2026-07-17T01:00:00Z") == "overnight"  # 21:00 ET
    assert quotes.session_label("2026-07-16T12:00:00Z") == "pre"       # 08:00 ET
    assert quotes.session_label("2026-01-16T15:00:00Z") == "rth"       # 10:00 EST
    assert quotes.session_label("2026-07-19T15:00:00Z") == "overnight"  # Saturday
    assert quotes.session_label(None) is None


def test_fetch_spots_newest_print_wins(monkeypatch):
    """After the close a stale 'real-time' print must lose to a fresher tape."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("QUOTES_PROVIDER", "auto")
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(hours=3)).isoformat()   # IEX frozen at the close
    fresh = (now - timedelta(seconds=30)).isoformat()

    monkeypatch.setattr(quotes, "_spots_alpaca", lambda syms: {
        "SPY": {"ticker": "SPY", "price": 750.87, "ts": stale,
                "source": "alpaca·iex", "delayed": False, "session": "rth"}})
    monkeypatch.setattr(quotes, "_spot_yahoo", lambda sym: {
        "ticker": sym, "price": 749.82, "ts": fresh,
        "source": "yahoo", "delayed": False, "session": "post"})
    quotes._spot_cache.clear()
    out = quotes.fetch_spots(["SPY"])
    assert out["SPY"]["price"] == 749.82 and out["SPY"]["session"] == "post"
