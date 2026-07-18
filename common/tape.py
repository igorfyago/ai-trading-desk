"""The house tape read: VWAP bands + RSI posture + Heikin-Ashi conviction +
volume profile, fused into one stage machine every agent can quote.

House method (no attribution — it's how this desk reads intraday):
  ARM      price tags/pierces the -2 sigma VWAP band while RSI sits red
           under its MA (mirror: +2 band with RSI green for shorts).
  CONFIRM  price wicks back into/through the -1 band on >1.5x average
           volume, RSI curls, and Heikin-Ashi bodies thicken in the trade
           direction — 'meaningful' candles, not drift.
  TRIGGER  price crosses the nearest volume-profile wall in the trade
           direction; past a wall the low-volume gap travels fast toward
           the NEXT wall, which often sits near VWAP. VWAP is the magnet
           price revisits (trend days excepted).

All functions below are pure (bars in, dicts out) so agents and tests never
touch the network; get_tape_read() is the only seam to common.quotes.
Bars are {t,o,h,l,c,v} dicts, epoch seconds UTC, ascending.
"""

import math
import statistics
from datetime import datetime, timezone


def _true_et_secs(t: int) -> int:
    """Seconds since midnight REAL Eastern Time (manual US-DST: EDT from the
    second Sunday of March 07:00 UTC to the first Sunday of November 06:00
    UTC). Session bucketing elsewhere keeps the fixed -4h convention; this is
    only for clock-of-day policy gates."""
    d = datetime.fromtimestamp(t, tz=timezone.utc)
    mar1 = datetime(d.year, 3, 1, tzinfo=timezone.utc)
    dst_on = mar1.replace(day=1 + (6 - mar1.weekday()) % 7 + 7, hour=7)
    nov1 = datetime(d.year, 11, 1, tzinfo=timezone.utc)
    dst_off = nov1.replace(day=1 + (6 - nov1.weekday()) % 7, hour=6)
    off = -4 * 3600 if dst_on <= d < dst_off else -5 * 3600
    return (t + off) % 86400

_TINY = 1e-9

RSI_N = 14           # Wilder lookback
RSI_MA_N = 9         # SMA over RSI: the 'ma' the state machine compares against
ARM_LOOKBACK = 40    # bars scanned for a -2/+2 band tag
CONFIRM_WINDOW = 6   # confirm must print within the freshest bars
VOL_SPIKE = 1.5      # last bar vs 20-bar average volume
WALL_MULT = 1.4      # local max must clear 1.4x median row volume
GAP_MULT = 0.55      # rows under 0.55x median form a low-volume gap
MEANINGFUL_BODY = 0.45  # HA body_ratio floor for a 'meaningful' candle


# ------------------------------------------------------------ indicators ----

def heikin(bars: list[dict]) -> list[dict]:
    """Heikin-Ashi transform. o/h/l/c are the HA values; body_ratio in [0,1]
    (body over full HA range) is the conviction gauge the confirm step reads."""
    out: list[dict] = []
    for b in bars:
        ha_c = (b["o"] + b["h"] + b["l"] + b["c"]) / 4.0
        ha_o = (b["o"] + b["c"]) / 2.0 if not out else (out[-1]["o"] + out[-1]["c"]) / 2.0
        ha_h = max(b["h"], ha_o, ha_c)
        ha_l = min(b["l"], ha_o, ha_c)
        out.append({"t": b["t"], "o": ha_o, "h": ha_h, "l": ha_l, "c": ha_c,
                    "v": b.get("v", 0),
                    "body_ratio": abs(ha_c - ha_o) / max(ha_h - ha_l, _TINY)})
    return out


def rsi(closes: list[float], n: int = RSI_N) -> list[float | None]:
    """Wilder-smoothed RSI; None until the seed window fills."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n
    out[n] = 100.0 if avg_l < _TINY else 100.0 - 100.0 / (1 + avg_g / avg_l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if avg_l < _TINY else 100.0 - 100.0 / (1 + avg_g / avg_l)
    return out


def sma(values: list[float | None], n: int) -> list[float | None]:
    """Simple MA; windows containing None stay None (rsi warm-up passes through)."""
    out: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        win = values[i - n + 1:i + 1]
        if any(v is None for v in win):
            continue
        out[i] = sum(win) / n
    return out


def session_vwap(bars: list[dict]) -> list[dict]:
    """Per-bar session VWAP + volume-weighted sigma of hlc3, with the 1/2
    sigma bands. Session = NY day: (t - 4h) // 86400. Resets each session."""
    out: list[dict] = []
    session = None
    cum_v = cum_pv = cum_p2v = 0.0
    for b in bars:
        s = (b["t"] - 4 * 3600) // 86400
        if s != session:
            session, cum_v, cum_pv, cum_p2v = s, 0.0, 0.0, 0.0
        p = (b["h"] + b["l"] + b["c"]) / 3.0
        v = float(b.get("v") or 0)
        cum_v += v
        cum_pv += p * v
        cum_p2v += p * p * v
        if cum_v > _TINY:
            vwap = cum_pv / cum_v
            var = max(cum_p2v / cum_v - vwap * vwap, 0.0)
        else:
            vwap, var = p, 0.0   # zero-volume session start: degenerate bands
        sig = math.sqrt(var)
        out.append({"vwap": vwap, "sigma": sig,
                    "u1": vwap + sig, "d1": vwap - sig,
                    "u2": vwap + 2 * sig, "d2": vwap - 2 * sig})
    return out


def volume_profile(bars: list[dict], rows: int = 55) -> dict:
    """Horizontal volume histogram. Each bar's volume is spread across the
    rows its H-L overlaps, proportional to overlap. Walls = local maxima
    > 1.4x median row volume; gaps = runs of rows < 0.55x median."""
    lo = min(b["l"] for b in bars)
    hi = max(b["h"] for b in bars)
    step = max(hi - lo, _TINY) / rows
    up = [0.0] * rows
    down = [0.0] * rows
    for b in bars:
        v = float(b.get("v") or 0)
        if v <= 0:
            continue
        bl, bh = b["l"], b["h"]
        bucket = up if b["c"] >= b["o"] else down
        if bh - bl < _TINY:   # flat bar: all volume in one row
            i = min(int((bl - lo) / step), rows - 1)
            bucket[i] += v
            continue
        i0 = max(int((bl - lo) / step), 0)
        i1 = min(int((bh - lo) / step), rows - 1)
        for i in range(i0, i1 + 1):
            r_lo, r_hi = lo + i * step, lo + (i + 1) * step
            overlap = min(bh, r_hi) - max(bl, r_lo)
            if overlap > 0:
                bucket[i] += v * overlap / (bh - bl)
    out_rows = [{"lo": round(lo + i * step, 4), "hi": round(lo + (i + 1) * step, 4),
                 "up": round(up[i], 2), "down": round(down[i], 2),
                 "total": round(up[i] + down[i], 2)}
                for i in range(rows)]
    totals = [r["total"] for r in out_rows]
    median_total = statistics.median(totals)
    poc_i = max(range(rows), key=lambda i: totals[i])
    walls = []
    for i in range(rows):
        left = totals[i - 1] if i > 0 else 0.0
        right = totals[i + 1] if i < rows - 1 else 0.0
        if totals[i] > WALL_MULT * median_total and totals[i] >= left and totals[i] >= right:
            walls.append(round(lo + (i + 0.5) * step, 4))
    gaps = []
    run = None
    for i in range(rows):
        if totals[i] < GAP_MULT * median_total:
            run = i if run is None else run
        elif run is not None:
            gaps.append({"lo": out_rows[run]["lo"], "hi": out_rows[i - 1]["hi"]})
            run = None
    if run is not None:
        gaps.append({"lo": out_rows[run]["lo"], "hi": out_rows[rows - 1]["hi"]})
    return {"rows": out_rows, "poc": round(lo + (poc_i + 0.5) * step, 4),
            "median_total": median_total, "walls": walls, "gaps": gaps}


# ------------------------------------------------------- the house read ----

def _rsi_state(rsi_s: list, ma_s: list, k: int) -> str | None:
    """red / red_curling below the MA, green / green_fading above; slope of
    RSI itself decides curl vs fade. None during warm-up."""
    r, m = rsi_s[k], ma_s[k]
    if r is None or m is None:
        return None
    prev = rsi_s[k - 1] if k > 0 else None
    rising = prev is not None and r > prev
    if r < m:
        return "red_curling" if rising else "red"
    return "green" if rising else "green_fading"


def _vol_spike(bars: list[dict], k: int) -> bool:
    """Bar k volume vs the average of the 20 bars preceding it."""
    prior = [b.get("v") or 0 for b in bars[max(0, k - 20):k]]
    if not prior:
        return False
    return (bars[k].get("v") or 0) > VOL_SPIKE * (sum(prior) / len(prior))


def _thickening(ha: list[dict], k: int) -> bool:
    """HA bodies growing over 3 bars AND the last one 'meaningful'."""
    if k < 2:
        return False
    r0, r2 = ha[k - 2]["body_ratio"], ha[k]["body_ratio"]
    return r2 > r0 and r2 >= MEANINGFUL_BODY


def _band_position(spot: float, band: dict) -> str:
    if spot < band["d2"]:
        return "below_-2"
    if spot < band["d1"]:
        return "-2..-1"
    if spot < band["vwap"]:
        return "-1..0"
    if spot < band["u1"]:
        return "0..+1"
    if spot < band["u2"]:
        return "+1..+2"
    return "above_+2"


def _fmt(x) -> str:
    return f"{x:.2f}" if x is not None else "none"


def day_shape(bars: list[dict]) -> dict | None:
    """The day-level read: capitulation + a double bottom (or top) that
    reclaimed (or lost) the session VWAP = a REVERSAL DAY. This outranks the
    structure lean for the rest of the session — after capitulation you stop
    leaning with the old trend.

      bullish_reversal_day: two swing lows within ~0.35% (or an undercut),
        climax volume (>=3x median) at the second, and spot back above VWAP
      bearish_reversal_day: the mirror off two swing highs
    """
    if len(bars) < 30:
        return None
    day = (bars[-1]["t"] - 4 * 3600) // 86400
    off = next(i for i, b in enumerate(bars)
               if (b["t"] - 4 * 3600) // 86400 == day)
    sess = bars[off:]
    if len(sess) < 20:
        return None
    vw = session_vwap(bars)
    spot = bars[-1]["c"]
    vols = [b["v"] for b in sess]
    # median over NONZERO volumes: prepost rows often print v=0, and a ~1
    # median would make every bar look like capitulation
    nz = sorted(v for v in vols if v > 0)
    if len(nz) < 5:
        return None
    med_v = nz[len(nz) // 2]

    def swings(key, cmp):
        pts = []
        for i in range(3, len(sess) - 2):
            win = sess[max(0, i - 3):i + 3]
            if cmp(sess[i][key], (min if cmp is _le else max)(b[key] for b in win)):
                if not pts or i - pts[-1] >= 5:
                    pts.append(i)
        return pts

    def _le(a, b):
        return a <= b

    def _ge(a, b):
        return a >= b

    lows = swings("l", _le)
    if len(lows) >= 2:
        i1, i2 = lows[0], lows[-1]
        lo1, lo2 = sess[i1]["l"], sess[i2]["l"]
        near_or_under = lo2 <= lo1 * 1.0035
        capit = max(vols[max(0, i2 - 1):i2 + 2]) >= 2.5 * med_v
        if near_or_under and capit and spot > vw[-1]["vwap"] and spot > sess[i2]["c"]:
            return {"shape": "bullish_reversal_day",
                    "low1": round(lo1, 2), "low2": round(lo2, 2),
                    "capitulation_t": sess[i2]["t"],
                    "capitulation_x": round(
                        min(max(vols[max(0, i2 - 1):i2 + 2]) / med_v, 99.0), 1)}

    highs = swings("h", _ge)
    if len(highs) >= 2:
        i1, i2 = highs[0], highs[-1]
        hi1, hi2 = sess[i1]["h"], sess[i2]["h"]
        near_or_over = hi2 >= hi1 * 0.9965
        capit = max(vols[max(0, i2 - 1):i2 + 2]) >= 2.5 * med_v
        if near_or_over and capit and spot < vw[-1]["vwap"] and spot < sess[i2]["c"]:
            return {"shape": "bearish_reversal_day",
                    "high1": round(hi1, 2), "high2": round(hi2, 2),
                    "capitulation_t": sess[i2]["t"],
                    "capitulation_x": round(
                        min(max(vols[max(0, i2 - 1):i2 + 2]) / med_v, 99.0), 1)}
    return None


def read_tape(bars: list[dict], ticker: str = "Spot") -> dict:
    """The full house read on a bar series. Pure: no I/O, deterministic."""
    if not bars:
        raise ValueError("read_tape needs at least one bar")
    n = len(bars)
    spot = float(bars[-1]["c"])
    vw = session_vwap(bars)
    band = vw[-1]
    rsi_s = rsi([b["c"] for b in bars])
    ma_s = sma(rsi_s, RSI_MA_N)
    ha = heikin(bars)
    prof = volume_profile(bars)

    state = _rsi_state(rsi_s, ma_s, n - 1)
    ratios = [round(h["body_ratio"], 3) for h in ha[-3:]]
    direction = "up" if ha[-1]["c"] >= ha[-1]["o"] else "down"

    # ---- stage machine, latest bars only -------------------------------
    # ARM: a -2/+2 band tag with RSI on the wrong side of its MA, inside the
    # lookback. Warm-up bars and degenerate session-open bands can't arm.
    arm_long = arm_short = None
    for i in range(max(0, n - ARM_LOOKBACK), n):
        st = _rsi_state(rsi_s, ma_s, i)
        if st is None or vw[i]["sigma"] <= spot * 1e-4:
            continue
        if bars[i]["l"] <= vw[i]["d2"] and st in ("red", "red_curling"):
            arm_long = i
        if bars[i]["h"] >= vw[i]["u2"] and st in ("green", "green_fading"):
            arm_short = i
    if arm_long is not None and (arm_short is None or arm_long >= arm_short):
        bias, arm_i = "long", arm_long
    elif arm_short is not None:
        bias, arm_i = "short", arm_short
    else:
        bias, arm_i = None, None

    # CONFIRM: wick back into/through the -1/+1 band on volume, RSI curling
    # the right way, HA bodies thickening in the trade direction.
    stage, conf_i = ("armed", None) if bias else ("none", None)
    if bias:
        for k in range(max(arm_i, n - CONFIRM_WINDOW), n):
            st = _rsi_state(rsi_s, ma_s, k)
            if st is None or not _vol_spike(bars, k) or not _thickening(ha, k):
                continue
            ha_up = ha[k]["c"] >= ha[k]["o"]
            if (bias == "long" and bars[k]["h"] >= vw[k]["d1"]
                    and st in ("red_curling", "green") and ha_up):
                conf_i = k
            elif (bias == "short" and bars[k]["l"] <= vw[k]["u1"]
                    and st in ("green_fading", "red") and not ha_up):
                conf_i = k
        # TRIGGER: spot through the nearest wall past the confirm close.
        # Past a wall the low-volume gap rolls fast toward the next wall.
        crossed = None
        if conf_i is not None:
            stage = "confirming"
            ref = bars[conf_i]["c"]
            if bias == "long":
                gate = [w for w in prof["walls"] if w > ref]
                if gate and spot > min(gate):
                    stage, crossed = "triggered", min(gate)
            else:
                gate = [w for w in prof["walls"] if w < ref]
                if gate and spot < max(gate):
                    stage, crossed = "triggered", max(gate)

    # ---- profile context around spot ------------------------------------
    walls = prof["walls"]
    wall_above = min((w for w in walls if w > spot), default=None)
    wall_below = max((w for w in walls if w < spot), default=None)
    in_gap = any(g["lo"] <= spot <= g["hi"] for g in prof["gaps"])
    gap_ahead = None
    if bias == "long":
        ahead = [g for g in prof["gaps"] if g["hi"] > spot]
        gap_ahead = min(ahead, key=lambda g: g["lo"]) if ahead else None
    elif bias == "short":
        ahead = [g for g in prof["gaps"] if g["lo"] < spot]
        gap_ahead = max(ahead, key=lambda g: g["hi"]) if ahead else None

    # Target: next wall in the trade direction, else VWAP (the magnet).
    target = None
    if bias == "long":
        target = wall_above if wall_above is not None else band["vwap"]
    elif bias == "short":
        target = wall_below if wall_below is not None else band["vwap"]

    # ---- plain-language read (numbers stay numbers) ----------------------
    pos = _band_position(spot, band)
    if state is None:
        head = f"{ticker} {spot:.2f} vs VWAP {band['vwap']:.2f}, {pos} band; RSI still warming up."
    else:
        head = (f"{ticker} {spot:.2f} vs VWAP {band['vwap']:.2f}, {pos} band; "
                f"RSI {rsi_s[-1]:.1f} vs MA {ma_s[-1]:.1f} ({state}).")
    if stage == "none":
        tail = (f"No setup armed; walls {_fmt(wall_below)} below / {_fmt(wall_above)} above, "
                f"POC {_fmt(prof['poc'])}.")
    elif stage == "armed":
        edge = bars[arm_i]["l"] if bias == "long" else bars[arm_i]["h"]
        b2 = "-2" if bias == "long" else "+2"
        b1 = band["d1"] if bias == "long" else band["u1"]
        tail = (f"{bias.capitalize()} armed off the {b2} band tag at {edge:.2f}; needs a "
                f"high-volume wick back through {b1:.2f} with RSI curling.")
    elif stage == "confirming":
        tail = (f"{bias.capitalize()} confirming: volume back through the "
                f"{'-1' if bias == 'long' else '+1'} band with HA bodies thickening; trigger is "
                f"the {_fmt(wall_above if bias == 'long' else wall_below)} wall, target {_fmt(target)}.")
    else:
        tail = (f"{bias.capitalize()} triggered through the {_fmt(crossed)} wall; the low-volume "
                f"gap should travel fast toward {_fmt(target)}, VWAP {band['vwap']:.2f} stays the magnet.")

    ds = day_shape(bars)
    if ds:
        # THE BACKTESTED GATE (docs/BACKTEST.md): 4.4y out-of-sample says the
        # reversal-day trade pays after 12:45 ET while the capitulation is
        # under two hours old; earlier/staler shapes graded flat-to-negative.
        ds["age_h"] = round((bars[-1]["t"] - ds["capitulation_t"]) / 3600, 2)
        ds["takeable"] = (_true_et_secs(bars[-1]["t"]) >= 12.75 * 3600
                          and ds["age_h"] < 2.0)
        side = "bullish" if ds["shape"].startswith("bull") else "bearish"
        lvl = ds.get("low2", ds.get("high2"))
        tail += (f" DAY SHAPE: {side} reversal day - capitulation "
                 f"({ds['capitulation_x']}x volume) at the double "
                 f"{'bottom' if side == 'bullish' else 'top'} near {lvl}, "
                 f"VWAP {'reclaimed' if side == 'bullish' else 'lost'}.")
        if not ds["takeable"]:
            tail += (" FORMING - the desk takes reversal-day entries after "
                     "12:45 ET on a capitulation under two hours old; until "
                     "then it is context, not the trade.")

    # ---- the four-check reversal list (his indicators, numbered) ---------
    # Each check reports the LATEST bar it fired on, so the chart can circle
    # exactly one spot per indicator and agents can walk the list by number.
    cl_side = bias or ("long" if spot < band["vwap"] else "short")
    lb0 = max(0, n - ARM_LOOKBACK)

    def _latest(pred):
        for i in range(n - 1, lb0 - 1, -1):
            if pred(i):
                return i
        return None

    if cl_side == "long":
        rsi_i = _latest(lambda i: _rsi_state(rsi_s, ma_s, i) in ("red", "red_curling"))
        tag_i = _latest(lambda i: vw[i]["sigma"] > spot * 1e-4
                        and bars[i]["l"] <= vw[i]["d2"])
        held = tag_i is not None and bars[tag_i]["c"] > vw[tag_i]["d2"]
        tag_px = None if tag_i is None else bars[tag_i]["l"]
    else:
        rsi_i = _latest(lambda i: _rsi_state(rsi_s, ma_s, i) in ("green", "green_fading"))
        tag_i = _latest(lambda i: vw[i]["sigma"] > spot * 1e-4
                        and bars[i]["h"] >= vw[i]["u2"])
        held = tag_i is not None and bars[tag_i]["c"] < vw[tag_i]["u2"]
        tag_px = None if tag_i is None else bars[tag_i]["h"]
    vol_i = _latest(lambda i: _vol_spike(bars, i))
    cap_t = (ds or {}).get("capitulation_t")
    conf_ok = conf_i is not None and bias == cl_side
    b2, b1 = ("-2σ", "-1σ") if cl_side == "long" else ("+2σ", "+1σ")

    def _chk(num, key, label, i, px, t_override=None):
        t = t_override if t_override is not None else (None if i is None else bars[i]["t"])
        return {"n": num, "key": key, "label": label, "ok": t is not None,
                "t": t, "px": None if px is None else round(px, 2)}

    checks = [
        _chk(1, "rsi", "RSI red under its MA" if cl_side == "long"
             else "RSI green over its MA", rsi_i,
             None if rsi_i is None else bars[rsi_i]["l" if cl_side == "long" else "h"]),
        _chk(2, "band2", f"{b2} tagged, {'wick held' if held else 'tag'}",
             tag_i, tag_px),
        _chk(3, "climax", "climax volume (trend exhaustion)"
             + (f" {ds['capitulation_x']}x" if cap_t else ""),
             vol_i, None if vol_i is None else bars[vol_i]["l" if cl_side == "long" else "h"],
             t_override=cap_t),
        _chk(4, "thick1", f"thick candle back through {b1}",
             conf_i if conf_ok else None,
             None if not conf_ok else (vw[conf_i]["d1"] if cl_side == "long" else vw[conf_i]["u1"])),
    ]
    checklist = {"side": cl_side, "done": sum(1 for c in checks if c["ok"]),
                 "checks": checks, "stage": stage}

    # ---- MECHANICAL NOW-STATE -------------------------------------------
    # One deterministic "you are here": what to do at THIS price plus the
    # nearest actionable line on each side. Levels beyond realistic reach
    # are never emitted — the voice reads this block verbatim instead of
    # composing its own levels (a wall 4$ away is context, not a trigger).
    reach = max(1.25 * band["sigma"], spot * 0.0015)

    def _line(level, means):
        if level is None or abs(level - spot) > reach or abs(level - spot) < spot * 1e-5:
            return None
        return {"level": round(level, 2), "dist": round(level - spot, 2),
                "means": means}

    vw_px = band["vwap"]
    up = down = None
    if stage == "triggered":
        stance = "in_trade"
        do_now = (f"The {bias} reversal is live - manage it, don't re-pitch it: "
                  f"target {_fmt(target)}, thesis dies on a 15m close back "
                  f"{'below' if bias == 'long' else 'above'} VWAP {vw_px:.2f}.")
        if bias == "long":
            up = _line(target, "the gap target - scale the runner there")
            down = _line(vw_px, "a 15m close under VWAP kills the thesis - no adds")
        else:
            down = _line(target, "the gap target - scale the runner there")
            up = _line(vw_px, "a 15m close over VWAP kills the thesis - no adds")
    elif ds and ds.get("takeable"):
        d_side = "long" if ds["shape"].startswith("bull") else "short"
        stance = "enter"
        do_now = (f"Reversal day is on and fresh - the {d_side} works here at "
                  f"{spot:.2f}; the thesis line is VWAP {vw_px:.2f}.")
        if d_side == "long":
            up = _line(target, "first objective - the gap/wall ahead")
            down = _line(vw_px, "the thesis - a thick 15m close under it and the day-call is wrong")
        else:
            down = _line(target, "first objective - the gap/wall ahead")
            up = _line(vw_px, "the thesis - a thick 15m close over it and the day-call is wrong")
    elif ds:
        d_side = "long" if ds["shape"].startswith("bull") else "short"
        stance = "wait_pullback"
        do_now = (f"No counter-trend and no chase at {spot:.2f} - the "
                  f"{'bullish' if d_side == 'long' else 'bearish'} reversal day is in but the "
                  f"entry window passed; wait for a pullback toward VWAP {vw_px:.2f} that holds.")
        if d_side == "long":
            down = _line(vw_px, "the pullback zone - a hold there is the entry; a thick close through it kills the day")
            up = _line(wall_above, "if it gets there without a pullback, it ran without you - still no chase")
        else:
            up = _line(vw_px, "the pullback zone - a hold there is the entry; a thick close through it kills the day")
            down = _line(wall_below, "if it gets there without a pullback, it ran without you - still no chase")
    elif stage == "confirming":
        gate = ([w for w in prof["walls"] if w > bars[conf_i]["c"]] if bias == "long"
                else [w for w in prof["walls"] if w < bars[conf_i]["c"]])
        trig_lvl = (min(gate) if gate else None) if bias == "long" else (max(gate) if gate else None)
        stance = "conditional"
        do_now = (f"Nothing filled yet at {spot:.2f} - the {bias} is confirming; "
                  "the trigger is the wall, not here.")
        if bias == "long":
            up = _line(trig_lvl, f"a push through it TRIGGERS the long - gap runs toward {_fmt(target)}")
            down = _line(band["d1"], "a 15m close back under -1σ kills the confirm - stand down")
        else:
            down = _line(trig_lvl, f"a push through it TRIGGERS the short - gap runs toward {_fmt(target)}")
            up = _line(band["u1"], "a 15m close back over +1σ kills the confirm - stand down")
    elif stage == "armed":
        stance = "conditional"
        do_now = (f"Nothing to buy at {spot:.2f} - the {bias} reversal is armed, "
                  "not confirmed; the entry is the confirm close, and only that.")
        if bias == "long":
            up = _line(band["d1"], "a high-volume 15m body closing back above -1σ CONFIRMS the long - that is the entry")
            down = _line(band["d2"], "the tag zone - a wick re-arms it; a THICK close under it means the flush is real, no knife-catch")
        else:
            down = _line(band["u1"], "a high-volume 15m body closing back under +1σ CONFIRMS the short - that is the entry")
            up = _line(band["u2"], "the tag zone - holding above it kills the fade, the trend runs")
    else:
        stance = "wait"
        do_now = f"No setup armed at {spot:.2f} - nothing to do; watch the lines."
        ups = [x for x in (band["u1"], band["u2"], wall_above) if x is not None and x > spot]
        dns = [x for x in (band["d1"], band["d2"], wall_below) if x is not None and x < spot]
        up = _line(min(ups) if ups else None, "reclaim and hold it and buyers take control")
        down = _line(max(dns) if dns else None, "lose it and sellers take control")

    action = {"stance": stance, "do_now": do_now, "up": up, "down": down,
              "reach": round(reach, 2)}

    return {
        "action": action,
        "bands": {"u2": round(band["u2"], 2), "u1": round(band["u1"], 2),
                  "vwap": round(band["vwap"], 2), "d1": round(band["d1"], 2),
                  "d2": round(band["d2"], 2)},
        "spot": round(spot, 4),
        "vwap": round(band["vwap"], 4),
        "day_shape": ds,
        "band_position": pos,
        "rsi": {"value": None if rsi_s[-1] is None else round(rsi_s[-1], 2),
                "ma": None if ma_s[-1] is None else round(ma_s[-1], 2),
                "state": state},
        "ha": {"body_ratio_last3": ratios,
               "thickening": _thickening(ha, n - 1),
               "direction": direction},
        "profile": {"poc": prof["poc"], "wall_above": wall_above,
                    "wall_below": wall_below, "in_gap": in_gap,
                    "gap_ahead": gap_ahead},
        "stage": stage,
        "bias": bias,
        "checklist": checklist,
        "target": None if target is None else round(target, 2),
        "plain": f"{head} {tail} NOW: {do_now}",
    }


def get_tape_read(ticker: str, interval: str = "15m") -> dict | None:
    """Live seam: bars from the shared quote feed, read by the house method.
    None when the feed is off/empty (QUOTES_PROVIDER=off in tests)."""
    from common import quotes

    payload = quotes.get_bars(ticker, interval, limit=240)  # >=120 bars wanted
    if not payload or not payload.get("bars"):
        return None
    bars = payload["bars"]
    read = read_tape(bars, ticker=payload["ticker"])
    read.update({
        "ticker": payload["ticker"],
        "interval": payload["interval"],
        "as_of": datetime.fromtimestamp(bars[-1]["t"], tz=timezone.utc).isoformat(),
    })
    return read
