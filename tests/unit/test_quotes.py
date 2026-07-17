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


def test_poll_spots_serves_cache_and_batches_stale(monkeypatch):
    """Side-channel polling: fresh cache entries cost zero provider calls;
    only the stale symbols go down the chain, in one batched pass."""
    monkeypatch.setenv("QUOTES_PROVIDER", "yahoo")
    calls = []

    def fake_yahoo(sym):
        calls.append(sym)
        return {"ticker": sym, "price": 42.0, "ts": None, "source": "yahoo",
                "delayed": False, "session": None}

    monkeypatch.setattr(quotes, "_spot_yahoo", fake_yahoo)
    quotes._spot_cache.clear()
    quotes._spot_cache["NOW"] = (time.monotonic(), {
        "ticker": "NOW", "price": 900.0, "ts": None, "source": "yahoo",
        "delayed": False, "session": None})

    out = quotes.poll_spots(["NOW", "META"], max_age_s=20.0)
    assert out["NOW"]["price"] == 900.0          # cache hit, no provider call
    assert calls == ["META"]

    out = quotes.poll_spots(["NOW", "META"], max_age_s=0.0)   # everything stale
    assert calls == ["META", "NOW", "META"] and out["NOW"]["price"] == 42.0


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


def test_symbol_dialects():
    assert quotes.clean_symbol("NASDAQ:TSLA") == "TSLA"
    assert quotes.clean_symbol("CME_MINI:ES1!") == "ES1!"
    assert quotes.clean_symbol(" spy ") == "SPY"
    assert quotes._alpaca_sym("BTCUSD") is None          # not on the stock feed
    assert quotes._alpaca_sym("BRK.B") == "BRK.B"
    assert quotes._yahoo_sym("BRK.B") == "BRK-B"
    assert quotes._yahoo_sym("ES1!") == "ES=F"
    assert quotes._yahoo_sym("VIX") == "^VIX"


def test_watch_quotes_batches_and_maps(monkeypatch):
    monkeypatch.setenv("QUOTES_PROVIDER", "yahoo")
    monkeypatch.setattr(quotes, "_spot_yahoo", lambda s: {
        "ticker": s, "price": 100.0, "ts": None, "source": "yahoo",
        "delayed": False, "session": "post"})
    monkeypatch.setattr(quotes, "_closes", lambda s: (80.0, 90.0))
    quotes._watch_cache = None
    quotes._last_watch_row.clear()
    rows = quotes.watch_quotes(["NASDAQ:TSLA", "BTCUSD", "TSLA"])   # dedup
    assert [r["sym"] for r in rows] == ["TSLA", "BTCUSD"]
    assert rows[0]["chg_pct"] == 25.0                    # vs prev close 80
    assert rows[0]["ext_pct"] == round((100 / 90 - 1) * 100, 2)     # vs reg close


def test_watch_quotes_rescues_frozen_prints(monkeypatch):
    """IEX frozen at the close (hours stale) must lose to a fresher yahoo
    print; a 15-min delayed-SIP print is fresh enough and must NOT trigger
    a yahoo call."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("QUOTES_PROVIDER", "auto")
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    now = datetime.now(timezone.utc)
    calls = {"yahoo": 0}

    def fake_yahoo(sym):
        calls["yahoo"] += 1
        return {"ticker": sym, "price": 749.0,
                "ts": (now - timedelta(seconds=30)).isoformat(),
                "source": "yahoo", "delayed": False, "session": "post"}

    monkeypatch.setattr(quotes, "_spot_yahoo", fake_yahoo)
    monkeypatch.setattr(quotes, "_closes", lambda s: (None, None))

    # frozen: 3h-old print -> rescued by yahoo
    monkeypatch.setattr(quotes, "_spots_alpaca", lambda syms: {
        "SPY": {"ticker": "SPY", "price": 750.87,
                "ts": (now - timedelta(hours=3)).isoformat(),
                "source": "alpaca·iex", "delayed": False, "session": "rth"}})
    quotes._watch_cache = None
    quotes._last_watch_row.clear()
    quotes._rescue_at.clear()
    rows = quotes.watch_quotes(["SPY"])
    assert rows[0]["price"] == 749.0 and calls["yahoo"] == 1

    # fresh enough: 15-min sip print -> no yahoo call
    monkeypatch.setattr(quotes, "_spots_alpaca", lambda syms: {
        "SPY": {"ticker": "SPY", "price": 750.2,
                "ts": (now - timedelta(minutes=15)).isoformat(),
                "source": "alpaca·sip15", "delayed": True, "session": "post"}})
    quotes._watch_cache = None
    quotes._last_watch_row.clear()
    rows = quotes.watch_quotes(["SPY"])
    assert rows[0]["price"] == 750.2 and calls["yahoo"] == 1

    # market closed for days (weekend): don't ask anyone
    monkeypatch.setattr(quotes, "_spots_alpaca", lambda syms: {
        "SPY": {"ticker": "SPY", "price": 748.0,
                "ts": (now - timedelta(hours=40)).isoformat(),
                "source": "alpaca·sip15", "delayed": True, "session": "post"}})
    quotes._watch_cache = None
    quotes._last_watch_row.clear()
    quotes._rescue_at.clear()
    rows = quotes.watch_quotes(["SPY"])
    assert rows[0]["price"] == 748.0 and calls["yahoo"] == 1


def test_scrub_kills_phantom_wicks_keeps_real_spikes():
    """An isolated 4%-away wick (off-exchange print) gets clamped; a spike
    the neighboring closes confirm is a real move and survives."""
    base = [{"t": i * 60, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0, "v": 1}
            for i in range(60)]
    bad = dict(base[30], h=112.0)                 # phantom: neighbors at 100
    bars = base[:30] + [bad] + base[31:]
    out = quotes._scrub_bars(bars)
    assert out[30]["h"] < 102.0                   # clamped near the body

    real = [dict(b) for b in base]                # a real squeeze: closes move
    for j, px in ((40, 104.0), (41, 108.0), (42, 111.0), (43, 109.0)):
        real[j] = {"t": j * 60, "o": px - 1, "h": px + 1, "l": px - 2, "c": px, "v": 1}
    out2 = quotes._scrub_bars(real)
    assert out2[42]["h"] == 112.0                 # untouched: neighbors confirm


def test_extract_tickers_universe_and_ambiguity():
    import os
    os.environ.setdefault("QUOTES_PROVIDER", "off")
    from web import registry
    assert registry.extract_tickers("thoughts on nvda?") == ["NVDA"]
    assert registry.extract_tickers("is now a good time?") == []      # adverb
    assert registry.extract_tickers("how is NOW doing") == ["NOW"]    # ticker
    assert registry.extract_tickers("check $now") == ["NOW"]
    assert "ES1!" in registry.extract_tickers("es1! overnight")
    assert registry.extract_tickers("spy leads") == ["SPY"]


def test_watch_rows_never_regress_to_an_older_print(monkeypatch):
    """The 24h contract: once the extended tape has printed, a provider round
    that re-serves the FROZEN close (older ts) must not blank ext or flip the
    session dot back - the freshest known print wins across rounds."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("QUOTES_PROVIDER", "auto")
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(minutes=1)).isoformat()
    frozen = (now - timedelta(hours=4)).isoformat()
    monkeypatch.setattr(quotes, "_closes", lambda s: (330.0, 333.25))
    quotes._watch_cache = None
    quotes._last_watch_row.clear()

    monkeypatch.setattr(quotes, "_spots_alpaca", lambda syms: {
        "AAPL": {"ticker": "AAPL", "price": 333.75, "ts": fresh,
                 "source": "yahoo", "delayed": False, "session": "post"}})
    r1 = quotes.watch_quotes(["AAPL"])[0]
    assert r1["session"] == "post" and r1["ext_pct"] is not None

    quotes._watch_cache = None
    monkeypatch.setattr(quotes, "_spots_alpaca", lambda syms: {
        "AAPL": {"ticker": "AAPL", "price": 333.25, "ts": frozen,
                 "source": "alpaca", "delayed": False, "session": "rth"}})
    monkeypatch.setattr(quotes, "_spot_yahoo",
                        lambda s: (_ for _ in ()).throw(RuntimeError("down")))
    r2 = quotes.watch_quotes(["AAPL"])[0]
    assert r2["price"] == 333.75 and r2["session"] == "post"
    assert r2["ext_pct"] == r1["ext_pct"]

    # and a genuinely NEWER print flows through normally
    quotes._watch_cache = None
    newer = (now + timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(quotes, "_spots_alpaca", lambda syms: {
        "AAPL": {"ticker": "AAPL", "price": 334.10, "ts": newer,
                 "source": "yahoo", "delayed": False, "session": "post"}})
    r3 = quotes.watch_quotes(["AAPL"])[0]
    assert r3["price"] == 334.10
