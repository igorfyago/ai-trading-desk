"""The house tape read, fully offline: indicators, volume profile, and the
arm/confirm/trigger stage machine on scripted synthetic bars."""

from common import tape

# One NY session: (t - 4h) // 86400 constant for < 58 15m bars from the open.
_T0 = 20_000 * 86400 + 4 * 3600 + 34_200  # 09:30 New York, arbitrary day


def _bar(t, o, h, l, c, v):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def _falling_closes(n=40, start=100.0):
    # Mostly-down staircase: small up-wiggle every 3rd step keeps RSI honest.
    closes, px = [], start
    for i in range(n):
        px += 0.3 if i % 3 == 0 else -0.7
        closes.append(px)
    return closes


def test_rsi_bounds_and_falling_market():
    closes = _falling_closes()
    out = tape.rsi(closes)
    assert len(out) == len(closes)
    assert all(v is None for v in out[:tape.RSI_N])
    vals = [v for v in out if v is not None]
    assert vals and all(0.0 <= v <= 100.0 for v in vals)
    assert out[-1] < 50.0                       # persistent selling => weak RSI
    assert tape.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    assert tape.sma([None, 2, 4], 2) == [None, None, 3.0]


def test_volume_profile_two_bands_wall_gap_poc():
    bars, t = [], _T0
    # Heavy band A, peaked near 97 (the heavier of the two: POC lives here).
    for _ in range(20):
        bars.append(_bar(t, 96.7, 97.5, 96.5, 97.3, 6000)); t += 900
    for i in range(10):
        px = 95.5 + (i % 4) * 1.0                # spread 95.0..99.0
        bars.append(_bar(t, px - 0.4, px + 0.5, px - 0.5, px + 0.4, 1500)); t += 900
    # Quiet traverse 99 -> 101: the low-volume gap between the bands.
    for _ in range(2):
        bars.append(_bar(t, 99.0, 101.0, 99.0, 101.0, 40)); t += 900
    # Heavy band B, peaked near 103, lighter than A.
    for _ in range(20):
        bars.append(_bar(t, 102.7, 103.5, 102.5, 102.9, 5000)); t += 900
    for i in range(10):
        px = 101.5 + (i % 4) * 1.0               # spread 101.0..105.0
        bars.append(_bar(t, px - 0.4, px + 0.5, px - 0.5, px + 0.4, 1200)); t += 900

    prof = tape.volume_profile(bars, rows=20)
    assert len(prof["rows"]) == 20
    assert sum(r["total"] for r in prof["rows"]) > 0
    assert len(prof["walls"]) >= 2               # a wall in each band
    assert any(w < 99.0 for w in prof["walls"]) and any(w > 101.0 for w in prof["walls"])
    assert any(g["lo"] >= 98.4 and g["hi"] <= 101.6 for g in prof["gaps"])
    assert 96.0 <= prof["poc"] <= 98.0           # heavier band carries the POC


def test_heikin_body_ratio_bounded():
    bars, t, px = [], _T0, 100.0
    for i in range(25):
        o = px
        c = px + (0.8 if i % 2 == 0 else -0.6)
        h = max(o, c) + 0.3
        l = min(o, c) - 0.3
        bars.append(_bar(t, o, h, l, c, 1000)); t += 900; px = c
    ha = tape.heikin(bars)
    assert len(ha) == len(bars)
    assert all(0.0 <= h["body_ratio"] <= 1.0 for h in ha)
    # First bar seeds from its own raw open/close.
    assert ha[0]["o"] == (bars[0]["o"] + bars[0]["c"]) / 2
    assert ha[0]["c"] == sum(bars[0][k] for k in "ohlc") / 4


def _scripted_long_setup():
    """Downtrend -> -2 sigma flush -> high-volume hammer back into -1 ->
    thick up HA candles pushing through the lower wall."""
    bars, t, px = [], _T0, 101.0
    for i in range(30):                          # gentle bleed 101 -> ~99.2
        o = px                                   # mixed deltas keep RSI off 0
        c = px + (0.20 if i % 3 == 0 else -0.19)
        bars.append(_bar(t, o, max(o, c) + 0.15, min(o, c) - 0.15, c, 1000))
        t += 900; px = c
    flush = [(99.2, 98.6, 98.5, 99.3, 1500),     # (o, c, l, h, v): tags -2 band
             (98.6, 98.0, 97.9, 98.7, 1800),
             (98.0, 97.7, 97.5, 98.1, 2000)]
    for o, c, l, h, v in flush:
        bars.append(_bar(t, o, h, l, c, v)); t += 900
    bars.append(_bar(t, 97.7, 99.4, 97.4, 99.2, 4000)); t += 900   # hammer, 4x vol
    for o, c, l, h, v in [(99.2, 99.9, 99.1, 100.0, 3000),         # thick up candles
                          (99.9, 100.5, 99.8, 100.6, 2800),
                          (100.5, 100.9, 100.4, 101.0, 2600)]:
        bars.append(_bar(t, o, h, l, c, v)); t += 900
    return bars


def test_read_tape_long_setup_end_to_end():
    bars = _scripted_long_setup()
    read = tape.read_tape(bars, ticker="SPY")
    assert read["bias"] == "long"
    assert read["stage"] in ("confirming", "triggered")
    assert read["target"] is not None
    assert read["spot"] == bars[-1]["c"]
    assert read["band_position"] in ("-2..-1", "-1..0", "0..+1", "+1..+2", "above_+2")
    assert read["ha"]["direction"] == "up"
    assert read["rsi"]["state"] in ("red_curling", "green")
    assert "SPY" in read["plain"] and str(read["spot"])[:3] in read["plain"]
    # Quiet tape stays flat: no arm without a band tag.
    quiet = [_bar(_T0 + i * 900, 100.0, 100.2, 99.8, 100.1, 1000) for i in range(40)]
    flat = tape.read_tape(quiet)
    assert flat["stage"] == "none" and flat["bias"] is None


def test_checklist_reports_the_four_checks_with_bar_times():
    """His four reversal indicators, numbered, each pinned to the LATEST bar
    it fired on — the UI circles those bars, the agents walk the count."""
    bars = _scripted_long_setup()
    cl = tape.read_tape(bars, ticker="SPY")["checklist"]
    assert cl["side"] == "long"
    assert [c["n"] for c in cl["checks"]] == [1, 2, 3, 4]
    assert cl["done"] == 4                       # a fired setup has all four in
    times = {b["t"] for b in bars}
    for c in cl["checks"]:
        assert c["ok"] and c["t"] in times       # every circle lands on a real bar
    # the -2σ tag and the thick cross are distinct moments of the story
    keyed = {c["key"]: c for c in cl["checks"]}
    assert keyed["band2"]["t"] <= keyed["thick1"]["t"]
    # a quiet tape still shows the list, mostly unchecked
    quiet = [_bar(_T0 + i * 900, 100.0, 100.2, 99.8, 100.1, 1000) for i in range(40)]
    qcl = tape.read_tape(quiet)["checklist"]
    assert qcl["done"] <= 2 and not qcl["checks"][3]["ok"]


def _gap_run_bars():
    """The FIG pattern, scripted: a fat volume hill builds around 100, a fast
    two-bar pop to 102 leaves a THIN zone overhead, price bases back at the
    hill with lower wicks between -1σ and VWAP, then a thick candle crosses
    the trigger into the empty book."""
    bars, t = [], _T0
    px = 100.0
    for i in range(20):                      # a WIDE fat hill: chop 99.6-100.4
        o = px
        c = 100.3 if i % 2 == 0 else 99.7
        bars.append(_bar(t, o, max(o, c) + 0.1, min(o, c) - 0.1, c, 2600)); t += 900; px = c
    for o, c in [(px, 100.9), (100.9, 101.2)]:   # fast pop: thin rows overhead
        bars.append(_bar(t, o, c + 0.1, o - 0.05, c, 300)); t += 900; px = c
    for o, c in [(px, 100.6), (100.6, 100.05)]:  # give it back fast (still thin)
        bars.append(_bar(t, o, o + 0.05, c - 0.1, c, 280)); t += 900; px = c
    for i in range(5):                       # basing: lower wicks, tight closes
        o = px
        c = 99.98 + 0.01 * i
        bars.append(_bar(t, o, max(o, c) + 0.04, min(o, c) - 0.26, c, 900)); t += 900; px = c
    bars.append(_bar(t, px, 100.25, px - 0.03, 100.22, 1600))  # the THICK trigger close
    return bars


def test_gap_run_detects_the_continuation_and_fires_on_the_thick_close():
    bars = _gap_run_bars()
    read = tape.read_tape(bars, ticker="FIG")
    g = read["gap_run"]
    assert g is not None, read["plain"]
    assert g["side"] == "long" and g["ready"]
    assert g["fired"], g
    assert g["target"] > g["trigger"] > 0
    assert read["action"]["stance"] == "enter"
    assert "GAP RUN" in read["plain"]

    # replace the thick trigger with a thin indecision candle at the base:
    # the setup is LOADED (a conditional), never an entry
    loaded_bars = bars[:-1] + [_bar(bars[-1]["t"], bars[-2]["c"],
                                    bars[-2]["c"] + 0.03, bars[-2]["c"] - 0.04,
                                    bars[-2]["c"] + 0.01, 700)]
    loaded = tape.read_tape(loaded_bars, ticker="FIG")
    g2 = loaded["gap_run"]
    assert g2 is not None and not g2["fired"], g2
    assert loaded["action"]["stance"] in ("conditional", "wait")


def test_action_now_never_quotes_out_of_reach_levels():
    """The mechanical now-state: one do_now plus the nearest line each side,
    every emitted level within reach — the voice reads this verbatim instead
    of composing its own levels (a wall 4$ away is never 'the confirmation')."""
    trig = tape.read_tape(_scripted_long_setup())
    act = trig["action"]
    assert act["stance"] in ("in_trade", "enter") and act["do_now"]  # actionable
    assert "NOW:" in trig["plain"]

    quiet = [_bar(_T0 + i * 900, 100.0, 100.2, 99.8, 100.1, 1000) for i in range(40)]
    qact = tape.read_tape(quiet)["action"]
    assert qact["stance"] == "wait"

    for read in (trig["action"], qact):
        for side in ("up", "down"):
            line = read[side]
            if line is not None:
                assert abs(line["dist"]) <= read["reach"] + 1e-6
                assert line["means"]
        assert read["reach"] < 105 * 0.05          # sane bound, not the whole chart


def test_get_tape_read_none_when_feed_off(monkeypatch):
    monkeypatch.setenv("QUOTES_PROVIDER", "off")   # conftest default, made explicit
    assert tape.get_tape_read("SPY") is None
