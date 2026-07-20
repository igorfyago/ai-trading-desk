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


def test_short_gamma_below_flip_is_a_long_put(monkeypatch):
    _force(monkeypatch, "negative_gamma", -40, spot_vs_flip="below")
    r = signals.recommend_trade("SPY")
    assert r["structure"] == "long put"
    (leg,) = r["legs"]
    assert (leg["side"], leg["kind"]) == ("buy", "put")


def test_short_gamma_above_flip_is_a_long_call(monkeypatch):
    _force(monkeypatch, "negative_gamma", 40, spot_vs_flip="above")
    r = signals.recommend_trade("SPY")
    assert r["structure"] == "long call"
    (leg,) = r["legs"]
    assert (leg["side"], leg["kind"]) == ("buy", "call")


@pytest.mark.parametrize("score,kind", [(50, "call"), (-50, "put"), (0, "call")])
def test_long_gamma_fades_the_range_with_one_contract(monkeypatch, score, kind):
    """Pinned tape: the score picks the side and the desk fades the range from
    its edge. It does NOT sell premium at the walls - it never did, whatever
    the old structure name claimed."""
    _force(monkeypatch, "positive_gamma", score)
    r = signals.recommend_trade("SPY")
    (leg,) = r["legs"]
    assert (leg["side"], leg["kind"]) == ("buy", kind)


@pytest.mark.parametrize("regime,score", [
    ("negative_gamma", -40), ("negative_gamma", 40),
    ("positive_gamma", 50), ("positive_gamma", -50), ("positive_gamma", 0),
])
def test_the_desk_never_sells_an_option(monkeypatch, regime, score):
    """ONE DIRECTION, ALWAYS. Sizing is budget/premium, the management rule is
    'sell half at +50%' and the doctrine is hold-to-zero: a spread or a condor
    cannot be sized, trimmed or held that way, so quoting one was describing a
    trade the desk would never place."""
    _force(monkeypatch, regime, score)
    r = signals.recommend_trade("SPY")
    assert len(r["legs"]) == 1
    assert r["legs"][0]["side"] == "buy"
    assert r["structure"].startswith("long ")


@pytest.mark.parametrize("regime,score", [("negative_gamma", -40), ("positive_gamma", 50)])
def test_legs_cannot_drift_from_execution(monkeypatch, regime, score):
    """The payload used to announce 'put debit spread' beside a single 744p:
    two answers to 'what am I buying' in one response. execution is the only
    authority now, and the structure has to agree with it."""
    _force(monkeypatch, regime, score)
    r = signals.recommend_trade("SPY")
    (leg,), ex = r["legs"], r["execution"]
    assert leg["kind"] == ex["kind"]
    assert leg["strike"] == ex["strike"]
    assert ex["kind"] in r["structure"]


def test_every_strike_is_spoken_in_both_tickers(monkeypatch):
    """The desk analyses SPY and fills XSP, so a strike quoted in one alone is
    half a fact. 'XSP 744p' has to arrive as 'XSP 744p (= SPY 742)' or the
    caller is left converting in their head mid-call."""
    _force(monkeypatch, "negative_gamma", -40)
    cp = signals.recommend_trade("SPY")["execution"]["contract_plan"]
    assert cp["contract_ticker"] == "XSP"
    spoken = cp["contract_spoken"]
    assert spoken.startswith(f"XSP {cp['contract_strike']:g}")
    assert f"SPY {cp['analysis_strike']:g}" in spoken
    # and the two strikes really are the house offset apart, not a typo
    assert cp["contract_strike"] - cp["analysis_strike"] == signals.XSP_OFFSET


def test_copy_trade_is_the_order_and_nothing_else(monkeypatch):
    """What the caller copies into a broker, composed in code so no model can
    pad it. Facts only: contract, price, size, trim, the add level."""
    _force(monkeypatch, "negative_gamma", -40)
    r = signals.recommend_trade("SPY")
    line = r["copy_trade"]
    assert len(line.split()) <= 40, f"copy_trade got wordy: {line}"
    assert line.startswith(("Buy ", "Nothing on yet"))
    assert line.endswith(".")
    # the order names both tickers, because the caller thinks in one and
    # fills in the other
    cp = r["execution"]["contract_plan"]
    if cp.get("contracts_now"):
        assert f"{cp['contract_strike']:g}" in line
        assert f"{cp['analysis_ticker']} {cp['analysis_strike']:g}" in line
    # and carries none of the narration
    for noise in ("GEX says", "dealers", "thesis", "regime"):
        assert noise not in line


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


def test_confluence_board_is_the_model(monkeypatch):
    """The desk model is a CONFLUENCE scorecard, not a precedence chain:
    GEX + the checklist agreeing = full clip; a triggered tape against the
    structure (the OOS coin-flip case) can only ever be half size."""
    real = market.latest_snapshot

    def fake(score):
        def _f(ticker, as_of=None):
            snap = real(ticker)
            snap["regime"] = "negative_gamma"
            snap["signal_score"] = score
            snap["gamma_flip"] = None
            return snap
        return _f

    bars = _reversal_bars()

    # long reversal fires but dealers lean SHORT: support without agreement
    monkeypatch.setattr(signals.market, "latest_snapshot", fake(-40))
    r = signals.recommend_trade("SPY", tape_bars=bars)
    c = r["confluence"]
    assert len(c["boxes"]) == 5 and {b["state"] for b in c["boxes"]} <= {"green", "red", "gray"}
    assert c["verdict"] != "full_confluence"          # structure box is red
    assert r["execution"]["contract_plan"]["plan"] != "full clip"

    # same tape with dealers BEHIND it: every box lines up, full clip
    monkeypatch.setattr(signals.market, "latest_snapshot", fake(40))
    r2 = signals.recommend_trade("SPY", tape_bars=bars)
    c2 = r2["confluence"]
    assert c2["verdict"] == "full_confluence", c2
    assert r2["execution"]["contract_plan"]["plan"] == "full clip"

    # no tape at all: structure alone is never full conviction
    monkeypatch.setattr(signals.market, "latest_snapshot", fake(-40))
    r3 = signals.recommend_trade("SPY")
    assert r3["confluence"]["verdict"] in ("partial", "wait")


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


def test_capitulation_needs_flush_and_oversold(monkeypatch):
    """Both conditions, or nothing. A heavy bar that is not oversold, or an
    oversold tape without the flush, is not this setup - each measured worse
    on its own than the pair."""
    from common import tape as T

    t0 = 1_750_000_000 - (1_750_000_000 - 4 * 3600) % 86400 + 14 * 3600
    def series(last_vol, drift):
        bars, p = [], 600.0
        for i in range(70):
            o = p; p -= drift
            bars.append({"t": t0 + i * 900, "o": o, "h": o + 0.05,
                         "l": p - 0.05, "c": p, "v": 1000})
        o = p; p -= 1.2
        bars.append({"t": t0 + 70 * 900, "o": o, "h": o + 0.05,
                     "l": p - 0.4, "c": p, "v": last_vol})
        return bars

    hit = T.capitulation(series(4200, 0.14))          # flush + oversold
    assert hit and hit["side"] == "long" and hit["vol_x"] >= 3.0

    assert T.capitulation(series(1100, 0.14)) is None   # oversold, no flush
    assert T.capitulation(series(4200, -0.14)) is None  # flush, not oversold


def test_capitulation_is_long_only(monkeypatch):
    """The short mirror lost money in every test that touched it, so the
    detector has no bearish branch to accidentally reach."""
    from common import tape as T

    t0 = 1_750_000_000 - (1_750_000_000 - 4 * 3600) % 86400 + 14 * 3600
    bars, p = [], 600.0
    for i in range(70):
        o = p; p -= 0.14
        bars.append({"t": t0 + i * 900, "o": o, "h": o + 0.05,
                     "l": p - 0.05, "c": p, "v": 1000})
    o = p; p -= 1.2
    bars.append({"t": t0 + 70 * 900, "o": o, "h": o + 0.05,
                 "l": p - 0.4, "c": p, "v": 4200})
    assert T.capitulation(bars)["side"] == "long"


# --------------------------------------------------------------- armed gate
# Three live calls died on the same shape: the caller was read a fillable
# order, challenged it, and got "no setup armed" out of the SAME payload.
# Each fix so far patched whichever ladder spoke last. These pin the rule
# itself: one authority, and every order-shaped field derives from it.

def _flat_zero_volume_bars(n=80, px=746.0):
    """A pre-market tape: real prices, NO volume. Yahoo genuinely returns 0
    volume for every extended-hours bar, so this is the normal state before
    the open, not a synthetic edge case."""
    t0 = 20_000 * 86400 + 4 * 3600 + 34_200
    bars, p = [], px
    for i in range(n):
        o = p
        p = px + (0.10 if i % 2 else -0.08)
        bars.append({"t": t0 + i * 900, "o": o, "h": max(o, p) + 0.05,
                     "l": min(o, p) - 0.05, "c": p, "v": 0})
    return bars


def test_zero_volume_session_reports_itself_instead_of_becoming_price():
    """With no volume there is no VWAP. The old code silently substituted
    hlc3, so 'the session VWAP at 747.01' WAS the last bar's close: a gate
    the price crosses seconds after it is spoken, which is exactly what a
    caller was told to wait for on a live call."""
    from common import tape as T

    bars = _flat_zero_volume_bars()
    read = T.read_tape(bars, ticker="SPY")
    assert read["bands_ok"] is False
    # the placeholder is just this bar's own hlc3 - no averaging happened at
    # all. On the real feed the last pre-market bar is a one-tick sliver
    # (o==h==l==c), so this lands exactly on spot and reads like a level.
    b = bars[-1]
    assert read["vwap"] == round((b["h"] + b["l"] + b["c"]) / 3.0, 4)
    assert read["bands"]["u1"] == read["bands"]["d1"] == read["bands"]["vwap"]
    assert (read["action"] or {})["stance"] == "wait"
    assert "no VWAP" in read["action"]["do_now"]  # ...and the tape says so
    assert read["stage"] == "none"               # nothing can arm without bands

    # and a real session still measures a VWAP that is NOT merely spot
    ok = T.read_tape(_double_bottom_day(), ticker="SPY")
    assert ok["bands_ok"] is True


def test_unarmed_tape_cannot_produce_a_fillable_order(monkeypatch):
    """THE regression. GEX has a sign on nearly every session, so structure
    alone used to hand over a real clip on demand regardless of the tape."""
    real = market.latest_snapshot

    def fake(ticker, as_of=None):
        snap = real(ticker)
        snap["regime"] = "negative_gamma"
        snap["signal_score"] = -40          # structure fully committed...
        snap["gamma_flip"] = None
        return snap

    monkeypatch.setattr(signals.market, "latest_snapshot", fake)
    bars = _flat_zero_volume_bars()          # ...but the tape has nothing
    r = signals.recommend_trade("SPY", tape_bars=bars)

    cp = r["execution"]["contract_plan"]
    assert cp["plan"] == "plan"
    assert cp["now_usd"] == 0 and cp["contracts_now"] == 0
    # the copy line is the caller's whole instruction: it must not be an order
    assert r["copy_trade"].startswith("Nothing on yet")
    assert "Buy " not in r["copy_trade"]


def test_armed_is_the_only_authority_no_field_disagrees(monkeypatch):
    """A model handed this payload must not be ABLE to assemble an order:
    suppressing copy_trade alone left execution/legs/plain_english still
    speaking one, which is how Marcus re-acquired a trade after admitting
    nothing was armed."""
    real = market.latest_snapshot

    def fake(ticker, as_of=None):
        snap = real(ticker)
        snap["regime"] = "negative_gamma"
        snap["signal_score"] = 40
        snap["gamma_flip"] = None
        return snap

    monkeypatch.setattr(signals.market, "latest_snapshot", fake)
    r = signals.recommend_trade("SPY", tape_bars=_flat_zero_volume_bars())
    assert signals.tape_armed(r.get("tape")) is False
    cp = r["execution"]["contract_plan"]
    assert cp["contracts_now"] == 0
    # every field that could imply a fill agrees with the gate
    assert cp["now_usd"] == 0
    assert "not an order" in (cp["add_trigger"] or "")


def test_degenerate_vwap_is_never_named_as_the_thesis(monkeypatch):
    """Naming the placeholder 'the session VWAP' is what produced an add
    trigger the price had already crossed when it was spoken."""
    real = market.latest_snapshot

    def fake(ticker, as_of=None):
        snap = real(ticker)
        snap["regime"] = "negative_gamma"
        snap["signal_score"] = -40
        snap["gamma_flip"] = None
        return snap

    monkeypatch.setattr(signals.market, "latest_snapshot", fake)
    r = signals.recommend_trade("SPY", tape_bars=_flat_zero_volume_bars())
    x = r["execution"]
    assert x["thesis_label"] != "the session VWAP"
    # and the thesis is a real level, not a mirror of spot
    assert x["thesis_reference"] != round(r["spot"], 2)


def test_location_box_never_scores_the_opposite_side_green(monkeypatch):
    """The box fell back to the other side's line and scored it green, so a
    long was handed the bearish line as its entry and the spoken explanation
    was false when the caller asked which level it meant."""
    real = market.latest_snapshot

    def fake(ticker, as_of=None):
        snap = real(ticker)
        snap["regime"] = "negative_gamma"
        snap["signal_score"] = -40
        snap["gamma_flip"] = None
        return snap

    monkeypatch.setattr(signals.market, "latest_snapshot", fake)
    r = signals.recommend_trade("SPY", tape_bars=_flat_zero_volume_bars())
    loc = [b for b in r["confluence"]["boxes"] if b["name"] == "location"][0]
    assert loc["state"] == "gray", loc


def test_the_stated_condition_is_never_already_satisfied(monkeypatch):
    """Igor, on a live call: 'why are you recommending me 746.48, like if it
    has to cross it, if we have already crossed it?' - spot was 746.53. A
    condition the price has already met is not a condition, and being told to
    wait for it twice is what destroyed the call. Whatever level the wait
    message names must sit on the far side of spot."""
    import re

    real = market.latest_snapshot

    for score in (-40, 40):
        def fake(ticker, as_of=None, _s=score):
            snap = real(ticker)
            snap["regime"] = "negative_gamma"
            snap["signal_score"] = _s
            snap["gamma_flip"] = None
            return snap

        monkeypatch.setattr(signals.market, "latest_snapshot", fake)
        r = signals.recommend_trade("SPY", tape_bars=_flat_zero_volume_bars())
        cp = r["execution"]["contract_plan"]
        assert cp["contracts_now"] == 0
        nums = [float(n) for n in re.findall(r"\d+\.\d+", cp["add_trigger"] or "")]
        spot = r["spot"]
        for n in nums:
            if r["execution"]["kind"] == "call":
                assert n > spot, f"call waits on {n} but spot is already {spot}"
            else:
                assert n < spot, f"put waits on {n} but spot is already {spot}"
