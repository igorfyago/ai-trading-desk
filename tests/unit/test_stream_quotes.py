"""The quote stream's side channel: a symbol the watch loop doesn't poll
(anything off WATCH_SYMBOLS) must keep ticking after the connect snapshot —
fetched down the provider chain with real ts/session/source — instead of
freezing while the bus only carries SPY/QQQ/IWM."""

import asyncio
import json

from common import quotes


def test_stream_side_polls_offwatch_symbols(monkeypatch):
    from web import server

    monkeypatch.setenv("QUOTES_PROVIDER", "yahoo")
    monkeypatch.setenv("WATCH_SYMBOLS", "SPY,QQQ,IWM")
    monkeypatch.setattr(server, "SIDE_POLL_S", 0.05)   # test-speed cadence
    calls = []

    def fake_yahoo(sym):
        calls.append(sym)
        return {"ticker": sym, "price": 100.0 + len(calls),
                "ts": f"2026-07-17T14:{len(calls) % 60:02d}:00+00:00",
                "source": "yahoo", "delayed": False, "session": "rth"}

    monkeypatch.setattr(quotes, "_spot_yahoo", fake_yahoo)
    quotes._spot_cache.clear()

    async def collect(n):
        resp = await server.stream_quotes("NOW")
        gen = resp.body_iterator          # the SSE generator itself
        out = []
        try:
            while len(out) < n:
                chunk = await asyncio.wait_for(gen.__anext__(), timeout=5)
                if chunk.startswith("data: "):
                    out.append(json.loads(chunk[len("data: "):]))
        finally:
            await gen.aclose()
        return out

    events = asyncio.run(collect(3))

    # connect snapshot + at least two side-poll ticks, all distinct prints
    assert [e["ticker"] for e in events] == ["NOW", "NOW", "NOW"]
    assert len({e["price"] for e in events}) == 3
    for e in events:                      # real fields, so the client's
        assert e["type"] == "quote"       # tick-ordering guard can work
        assert e["ts"] and e["source"] == "yahoo" and e["session"] == "rth"


def test_stream_watchlist_symbols_stay_on_the_bus(monkeypatch):
    """A stream of only watch-loop symbols must not side-poll at all."""
    from web import server

    monkeypatch.setenv("QUOTES_PROVIDER", "yahoo")
    monkeypatch.setenv("WATCH_SYMBOLS", "SPY,QQQ,IWM")
    monkeypatch.setattr(server, "SIDE_POLL_S", 0.05)
    calls = []

    def fake_yahoo(sym):
        calls.append(sym)
        return {"ticker": sym, "price": 500.0, "ts": None,
                "source": "yahoo", "delayed": False, "session": None}

    monkeypatch.setattr(quotes, "_spot_yahoo", fake_yahoo)
    quotes._spot_cache.clear()

    async def run_briefly():
        resp = await server.stream_quotes("SPY")
        gen = resp.body_iterator
        try:
            await asyncio.wait_for(gen.__anext__(), timeout=5)   # snapshot
            try:
                # keep the stream running across several would-be side-poll
                # periods: with no off-watchlist symbols it must stay silent
                await asyncio.wait_for(gen.__anext__(), timeout=0.3)
            except asyncio.TimeoutError:
                pass
        finally:
            await gen.aclose()

    asyncio.run(run_briefly())
    assert calls == ["SPY"]               # the connect snapshot, nothing more
