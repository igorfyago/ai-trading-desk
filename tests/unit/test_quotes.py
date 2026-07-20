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


def test_next_trading_day_skips_weekends_and_holidays():
    """The Friday bug: 'tomorrow' must resolve to the next TRADING day, never
    Saturday, and NYSE holidays are jumped too."""
    from datetime import date, timedelta

    def nxt(d):
        d = d + timedelta(days=1)
        while not quotes._is_trading_day(d):
            d += timedelta(days=1)
        return d

    assert nxt(date(2026, 7, 17)).isoformat() == "2026-07-20"   # Fri -> Mon
    assert nxt(date(2026, 7, 2)).isoformat() == "2026-07-06"    # Thu -> Mon (Jul 3 closed)
    assert nxt(date(2026, 12, 24)).isoformat() == "2026-12-28"  # Christmas + weekend
    assert not quotes._is_trading_day(date(2026, 7, 3))         # holiday
    assert not quotes._is_trading_day(date(2026, 7, 18))        # Saturday
    assert quotes._is_trading_day(date(2026, 7, 20))            # Monday

    c = quotes.trading_clock()                                  # live shape check
    assert set(c) >= {"today", "weekday", "market_state", "next_trading_day",
                      "next_trading_weekday", "is_trading_day"}
    assert quotes._is_trading_day(date.fromisoformat(c["next_trading_day"]))


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


def test_watch_quotes_rescues_prints_that_are_not_the_latest(monkeypatch):
    """What sends us hunting yahoo is 'the print we would SHOW is not the
    latest trade' — which is not the same question as 'is it old'.

    A real-time print frozen at the close is stale and gets rescued. A DELAYED
    print is never the latest trade at ANY age, so it stays eligible however
    long it sits. A real-time print on a market that has been shut for days IS
    the latest trade anyone has, so we ask nobody. Call volume is bounded by
    the per-symbol budget rather than by an age ceiling, because an age ceiling
    cannot tell the last two cases apart.
    """
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

    def alpaca_serves(**print_):
        monkeypatch.setattr(quotes, "_spots_alpaca",
                            lambda syms: {"SPY": {"ticker": "SPY", **print_}})
        quotes._watch_cache = None
        quotes._last_watch_row.clear()

    monkeypatch.setattr(quotes, "_spot_yahoo", fake_yahoo)
    monkeypatch.setattr(quotes, "_closes", lambda s: (None, None))

    # real time but frozen 3h at the close -> rescued by yahoo
    alpaca_serves(price=750.87, ts=(now - timedelta(hours=3)).isoformat(),
                  source="alpaca·iex", delayed=False, session="rth")
    quotes._rescue_at.clear()
    assert quotes.watch_quotes(["SPY"])[0]["price"] == 749.0
    assert calls["yahoo"] == 1

    # a 15-min delayed SIP print IS eligible (it is not the latest trade), but
    # the per-symbol budget from the rescue above still holds the call back
    alpaca_serves(price=750.2, ts=(now - timedelta(minutes=15)).isoformat(),
                  source="alpaca·sip15", delayed=True, session="post")
    assert quotes.watch_quotes(["SPY"])[0]["price"] == 750.2
    assert calls["yahoo"] == 1, "budget spent: ask again in 300s, not now"

    # ...and once the budget frees up, the same delayed print does get upgraded
    quotes._watch_cache = None
    quotes._rescue_at.clear()
    assert quotes.watch_quotes(["SPY"])[0]["price"] == 749.0
    assert calls["yahoo"] == 2

    # shut for days, and what we hold is REAL TIME: nothing anywhere is
    # fresher, so don't ask anyone
    alpaca_serves(price=748.0, ts=(now - timedelta(hours=40)).isoformat(),
                  source="alpaca·iex", delayed=False, session="post")
    quotes._rescue_at.clear()
    assert quotes.watch_quotes(["SPY"])[0]["price"] == 748.0
    assert calls["yahoo"] == 2, "a 40h real-time print is the latest there is"

    # shut for days, but what we hold is DELAYED: that is the SPY bug — the
    # print is wrong no matter how long it has sat, so we do ask, once
    alpaca_serves(price=748.0, ts=(now - timedelta(hours=40)).isoformat(),
                  source="alpaca·sip15", delayed=True, session="post")
    quotes._rescue_at.clear()
    assert quotes.watch_quotes(["SPY"])[0]["price"] == 749.0
    assert calls["yahoo"] == 3

    quotes._watch_cache = None
    quotes.watch_quotes(["SPY"])
    assert calls["yahoo"] == 3, "the budget, not an age window, bounds volume"


def test_watch_last_is_the_post_market_print_not_the_regular_close(monkeypatch):
    """Saturday 2026-07-18, production. Alpaca's free SIP feed stamps Friday's
    16:00 regular close (743.29) at the SESSION BOUNDARY, 00:00:00.07Z, which
    is three seconds NEWER than yahoo's genuine 23:59:57Z post-market trade at
    742.49 that supersedes it. Ranked on age the wrong print wins, so SPY sat
    at the regular close with no ext_pct while QQQ — which alpaca does not
    answer for, so yahoo won by default — was correct all along.

    Last must be the true latest print; the product runs a deliberate 24h tape.
    """
    monkeypatch.setenv("QUOTES_PROVIDER", "auto")
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    yahoo_tape = {           # the real last post-market trade, both symbols
        "SPY": {"ticker": "SPY", "price": 742.49, "ts": "2026-07-17T23:59:57Z",
                "source": "yahoo", "delayed": False, "session": "post"},
        "QQQ": {"ticker": "QQQ", "price": 693.76, "ts": "2026-07-17T23:59:57Z",
                "source": "yahoo", "delayed": False, "session": "post"},
    }
    closes = {"SPY": (740.00, 743.29), "QQQ": (694.00, 695.33)}
    calls = {"yahoo": 0}

    def fake_yahoo(sym):
        calls["yahoo"] += 1
        return dict(yahoo_tape[sym])

    # alpaca answers for SPY only, exactly as it does in production
    def frozen_sip(syms):
        return {"SPY": {"ticker": "SPY", "price": 743.29,
                        "ts": "2026-07-18T00:00:00.07Z", "source": "alpaca·sip15",
                        "delayed": True, "session": "overnight"}}

    monkeypatch.setattr(quotes, "_spots_alpaca", frozen_sip)
    monkeypatch.setattr(quotes, "_spot_yahoo", fake_yahoo)
    monkeypatch.setattr(quotes, "_closes", lambda s: closes[s])
    quotes._watch_cache = None
    quotes._last_watch_row.clear()
    quotes._rescue_at.clear()

    spy, qqq = quotes.watch_quotes(["SPY", "QQQ"])
    assert spy["price"] == 742.49, "the post-market print, not the 16:00 close"
    assert spy["source"] == "yahoo" and spy["session"] == "post"
    assert spy["reg_close"] == 743.29
    assert spy["ext_pct"] == -0.11        # 742.49 vs the 743.29 regular close
    assert qqq["price"] == 693.76 and qqq["ext_pct"] == -0.23   # unchanged

    # the next round re-serves the frozen close: it must neither overwrite the
    # real print nor cost another provider call, all weekend long
    quotes._watch_cache = None
    quotes._rescue_at.clear()
    spent = calls["yahoo"]
    spy2 = quotes.watch_quotes(["SPY", "QQQ"])[0]
    assert spy2["price"] == 742.49 and spy2["ext_pct"] == -0.11
    assert calls["yahoo"] == spent, "we hold the latest print: ask nobody"


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
    from common import tickers as registry
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


def test_a_realtime_print_beats_a_delayed_one_however_it_is_stamped(monkeypatch):
    """The SPY bug. Alpaca's free SIP feed stamps its 15-minute-delayed bars at
    the bar boundary, so a delayed print stamped 00:00:00 looked NEWER than a
    genuine 23:59:57 trade and won the merge on age alone. blendable_spot then
    discarded it for being delayed, so SPY had no live gamma at all while QQQ,
    which Alpaca did not answer for, worked fine."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("QUOTES_PROVIDER", "auto")
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    now = datetime.now(timezone.utc)
    boundary = now.isoformat()                                  # delayed, but newest
    real_print = (now - timedelta(seconds=90)).isoformat()      # real time, older

    monkeypatch.setattr(quotes, "_spots_alpaca", lambda syms: {
        "SPY": {"ticker": "SPY", "price": 743.29, "ts": boundary,
                "source": "alpaca·sip15", "delayed": True, "session": "overnight"}})
    monkeypatch.setattr(quotes, "_spot_yahoo", lambda sym: {
        "ticker": sym, "price": 744.10, "ts": real_print,
        "source": "yahoo", "delayed": False, "session": "post"})
    quotes._spot_cache.clear()
    quotes._last_watch_row.clear()

    out = quotes.fetch_spots(["SPY"])
    assert out["SPY"]["delayed"] is False, "a delayed print must not win"
    assert out["SPY"]["source"] == "yahoo" and out["SPY"]["price"] == 744.10


def test_a_delayed_print_is_still_better_than_nothing(monkeypatch):
    """Preferring real time must not mean discarding the only quote there is."""
    monkeypatch.setenv("QUOTES_PROVIDER", "auto")
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setattr(quotes, "_spots_alpaca", lambda syms: {
        "SPY": {"ticker": "SPY", "price": 743.29, "ts": None,
                "source": "alpaca·sip15", "delayed": True, "session": "rth"}})
    monkeypatch.setattr(quotes, "_spot_yahoo", lambda sym: None)
    monkeypatch.setattr(quotes, "_spot_cboe", lambda sym: None)
    quotes._spot_cache.clear()
    quotes._last_watch_row.clear()

    out = quotes.fetch_spots(["SPY"])
    assert out["SPY"]["price"] == 743.29 and out["SPY"]["delayed"] is True


def test_partial_tail_folds_on_intraday_only():
    """Yahoo appends a live stub stamped off-boundary. The resampled charts
    (3m/45m/4h) absorb it because _resample floors every timestamp; the raw
    ones (1m/5m/15m) used to keep it as its own o==h==l==c candle. Two charts
    of the same tape then reported different OHLC and a different change."""
    step = 900
    t0 = 1_750_000_000 // step * step
    aligned = [{"t": t0, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 10},
               {"t": t0 + step, "o": 100.5, "h": 102.0, "l": 100.0, "c": 101.0, "v": 20}]
    stub = {"t": t0 + step + 137, "o": 101.4, "h": 101.4, "l": 101.4,
            "c": 101.4, "v": 3}

    out = quotes._fold_partial_tail(aligned + [stub], step)
    assert len(out) == 2                       # the sliver is not its own bar
    last = out[-1]
    assert last["t"] == t0 + step              # the bucket keeps its boundary
    assert last["o"] == 100.5                  # ...and its open
    assert last["c"] == 101.4                  # but takes the live close
    assert last["h"] == 102.0 and last["l"] == 100.0
    assert last["v"] == 23                     # volume is summed, not dropped

    # an already-aligned tail is a real bar and must survive untouched
    assert quotes._fold_partial_tail(aligned, step) == aligned


def test_live_last_stamps_one_price_on_every_interval():
    """CHANGE IN TIME DOES NOT CHANGE THE MARKET PRICE. The last candle's
    close must be the same number on 5m, 15m, 45m, 4h and 1D — the latest
    trade — regardless of when each provider cut its series."""
    from datetime import datetime, timezone
    now = int(datetime.now(timezone.utc).timestamp())
    live = {"price": 741.5, "ts": datetime.fromtimestamp(now, timezone.utc).isoformat()}

    for step in (300, 900, 2700, 14400):
        bucket = now - (now % step)
        bars = [{"t": bucket, "o": 740.0, "h": 741.0, "l": 739.0, "c": 740.5, "v": 10}]
        out = quotes._live_last(bars, step, live)
        assert out[-1]["c"] == 741.5, f"step {step}: close must be the live print"
        assert out[-1]["h"] == 741.5 and out[-1]["l"] == 739.0
        assert out[-1]["o"] == 740.0           # the open is history, untouched

    # daily: same-session print moves the close; a stale bar from YESTERDAY
    # gets today's forming candle appended via the fold
    day = 86400
    today = now // day * day
    yday = {"t": today - day + 4 * 3600, "o": 745.0, "h": 746.0, "l": 744.0,
            "c": 745.5, "v": 100}
    out = quotes._live_last([yday], day, live)
    assert out[-1]["c"] == 741.5 and len(out) == 1 or out[-1]["c"] == 741.5

    # no live print, no touch
    assert quotes._live_last(bars, 300, None) == bars


def test_partial_tail_folds_daily_same_session_only():
    """The daily must carry the LIVE print, like every intraday chart: after
    the bell alpaca's 1D bar is frozen at the 16:00 official close (stamped
    04:00Z) while yahoo's live print moves. Two bars stamped on the SAME
    calendar day are one session — fold them, keep the session stamp. Bars
    from different days are distinct sessions and must NEVER merge."""
    day = 86400
    base = 1_750_000_000 // day * day                      # a midnight UTC
    alpaca_bar = {"t": base + 4 * 3600, "o": 747.04, "h": 748.69,
                  "l": 741.56, "c": 742.15, "v": 1000}     # 04:00Z, 16:00 close
    prior_day = {"t": base - day + 13 * 3600 + 1800, "o": 742.17,
                 "h": 747.25, "l": 740.8, "c": 743.28, "v": 900}
    live_stub = {"t": base + 22 * 3600 + 4800, "o": 741.16, "h": 741.16,
                 "l": 741.16, "c": 741.16, "v": 0}         # the post-market print

    out = quotes._fold_partial_tail([prior_day, alpaca_bar, live_stub], day)
    assert len(out) == 2
    last = out[-1]
    assert last["t"] == live_stub["t"]       # today's session stamp wins
    assert last["o"] == 747.04               # the session open is preserved
    assert last["c"] == 741.16               # the LIVE print is the close
    assert last["h"] == 748.69 and last["l"] == 741.16
    assert out[0] == prior_day               # yesterday untouched

    # different calendar days = different sessions: never fold
    two_days = quotes._fold_partial_tail([prior_day, alpaca_bar], day)
    assert len(two_days) == 2
