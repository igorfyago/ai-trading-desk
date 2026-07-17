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


def _reversal_bars():
    """A tape that genuinely walks the house long-reversal checklist (same
    script the tape tests use): mixed-delta bleed keeps RSI honest, a -2σ
    flush, a 4x-volume hammer, thick up candles through the wall — plus one
    extra push bar, which is what tips 'confirming' into 'triggered'."""
    def bar(t, o, h, l, c, v):
        return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}

    # 09:30 New York on an arbitrary day — one clean session, no VWAP reset
    bars, t, px = [], 20_000 * 86400 + 4 * 3600 + 34_200, 101.0
    for i in range(30):
        o = px
        c = px + (0.20 if i % 3 == 0 else -0.19)
        bars.append(bar(t, o, max(o, c) + 0.15, min(o, c) - 0.15, c, 1000))
        t += 900
        px = c
    for o, c, l, h, v in [(99.2, 98.6, 98.5, 99.3, 1500),
                          (98.6, 98.0, 97.9, 98.7, 1800),
                          (98.0, 97.7, 97.5, 98.1, 2000)]:
        bars.append(bar(t, o, h, l, c, v))
        t += 900
    bars.append(bar(t, 97.7, 99.4, 97.4, 99.2, 4000))     # the hammer
    t += 900
    for o, c, l, h, v in [(99.2, 99.9, 99.1, 100.0, 3000),
                          (99.9, 100.5, 99.8, 100.6, 2800),
                          (100.5, 100.9, 100.4, 101.0, 2600),
                          (100.9, 101.5, 100.8, 101.6, 2600)]:   # the push
        bars.append(bar(t, o, h, l, c, v))
        t += 900
    return bars


def test_triggered_tape_reversal_takes_the_trade(monkeypatch):
    """Igor's rework: a fired reversal checklist beats the structure lean —
    bearish structure + triggered long tape = CALLS with a local thesis and
    an achievable target, not puts into a rip."""
    real = market.latest_snapshot

    def fake(ticker, as_of=None):
        snap = real(ticker)
        snap["regime"] = "negative_gamma"
        snap["signal_score"] = -40          # structure says sellers...
        snap["gamma_flip"] = None
        return snap

    monkeypatch.setattr(signals.market, "latest_snapshot", fake)
    bars = _reversal_bars()
    from common import tape as tape_mod
    read = tape_mod.read_tape(bars)
    assert read["stage"] == "triggered" and read["bias"] == "long", read["plain"]

    r = signals.recommend_trade("SPY", tape_bars=bars)
    assert "reversal" in r["bias"]          # ...but the tape takes the trade
    assert r["execution"]["kind"] == "call"
    assert r["execution"]["thesis_label"] == "the session VWAP"
    assert r["execution"]["target"] is not None
    assert "TAPE says" in r["plain_english"]


def test_far_wall_demotes_to_local_line(monkeypatch):
    """Horizon discipline: a thesis wall beyond today's reach is context,
    never the working line."""
    real_snap = market.latest_snapshot
    real_walls = signals._latest_walls

    def fake(ticker, as_of=None):
        snap = real_snap(ticker)
        snap["regime"] = "negative_gamma"
        snap["signal_score"] = -40
        snap["gamma_flip"] = None
        return snap

    def far_walls(ticker, as_of=None):
        w = real_walls(ticker, as_of)
        spot = real_snap("SPY")["spot"]
        return {"call": {"strike": round(spot * 1.05), "strength": 1},
                "put": {"strike": round(spot * 0.95), "strength": 1}}

    monkeypatch.setattr(signals.market, "latest_snapshot", fake)
    monkeypatch.setattr(signals, "_latest_walls", far_walls)
    r = signals.recommend_trade("SPY")
    x = r["execution"]
    assert x["context_level"] is not None       # the far wall got demoted
    assert x["thesis_label"] != "the call wall"
    assert "beyond" in x["thesis_note"]


def _double_bottom_day():
    """Today's shape, scripted: selloff to a morning low, bounce, second leg
    to a matching low on CAPITULATION volume, then a reclaim through VWAP."""
    def bar(t, o, h, l, c, v):
        return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}

    bars, t, px = [], 20_000 * 86400 + 4 * 3600 + 34_200, 101.0
    for i in range(12):                      # leg one down
        o = px
        c = px - 0.28
        bars.append(bar(t, o, o + 0.1, c - 0.15, c, 1200)); t += 900; px = c
    lo1 = px
    for i in range(10):                      # bounce
        o = px
        c = px + 0.22
        bars.append(bar(t, o, c + 0.1, o - 0.1, c, 900)); t += 900; px = c
    for i in range(10):                      # leg two down toward the low
        o = px
        c = px - 0.24
        bars.append(bar(t, o, o + 0.1, c - 0.1, c, 1000)); t += 900; px = c
    bars.append(bar(t, px, px + 0.2, lo1 - 0.1, px + 0.1, 6000))   # capitulation
    t += 900; px += 0.1
    for i in range(6):                       # the reclaim through VWAP (fresh:
        o = px                               # capitulation stays under 2h old)
        c = px + 0.7
        bars.append(bar(t, o, c + 0.15, o - 0.05, c, 2400)); t += 900; px = c
    return bars


def test_reversal_day_flips_the_engine(monkeypatch):
    """Capitulation + double bottom + VWAP reclaim = bullish reversal day —
    the engine stops leaning short even when the structure says sellers."""
    from common import tape as tape_mod

    bars = _double_bottom_day()
    ds = tape_mod.day_shape(bars)
    assert ds and ds["shape"] == "bullish_reversal_day", ds
    assert ds["capitulation_x"] >= 2.5

    real = market.latest_snapshot

    def fake(ticker, as_of=None):
        snap = real(ticker)
        snap["regime"] = "negative_gamma"
        snap["signal_score"] = -40
        snap["gamma_flip"] = None
        return snap

    monkeypatch.setattr(signals.market, "latest_snapshot", fake)
    r = signals.recommend_trade("SPY", tape_bars=bars)
    if "reversal - tape triggered" not in r["bias"]:   # trigger outranks; else day
        assert "reversal day" in r["bias"]
        assert r["execution"]["kind"] == "call"
        assert r["execution"]["thesis_label"] == "the session VWAP"
        assert "reversal day" in r["plain_english"]


def test_morning_day_shape_never_fights_the_day(monkeypatch):
    """The backtested gate (docs/BACKTEST.md) + the transcript lesson: a shape
    outside the entry window is not a market entry — but the desk NEVER fades
    it either. Opposing structure ⇒ pullback-only on the day's side, not puts
    into a rip; agreeing structure ⇒ the structure trade stands."""
    from common import tape as tape_mod

    bars = [{**b, "t": b["t"] - 7 * 3600} for b in _double_bottom_day()]
    ds = tape_mod.day_shape(bars)
    assert ds and ds["shape"] == "bullish_reversal_day"     # shape still detected

    read = tape_mod.read_tape(bars)
    assert read["day_shape"]["takeable"] is False           # ...but gated
    assert "FORMING" in read["plain"]

    real = market.latest_snapshot

    def fake(score):
        def _f(ticker, as_of=None):
            snap = real(ticker)
            snap["regime"] = "negative_gamma"
            snap["signal_score"] = score
            snap["gamma_flip"] = None
            return snap
        return _f

    monkeypatch.setattr(signals.market, "latest_snapshot", fake(-40))
    r = signals.recommend_trade("SPY", tape_bars=bars)
    if "tape triggered" not in r["bias"]:                    # trigger may outrank
        assert "pullback only" in r["bias"]                  # never counter-trend
        assert r["execution"]["kind"] == "call"              # the day's side
        assert "pullback" in r["plain_english"] or "no chase" in r["plain_english"]

    monkeypatch.setattr(signals.market, "latest_snapshot", fake(40))
    r2 = signals.recommend_trade("SPY", tape_bars=bars)
    if "tape triggered" not in r2["bias"]:
        assert "pullback only" not in r2["bias"]             # agreeing structure runs
