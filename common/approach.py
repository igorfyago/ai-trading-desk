"""How price ARRIVED at a level, and therefore what happens next.

The same level, the same RSI, the same band - and two opposite trades. What
separates them is the CHARACTER OF THE APPROACH:

  FLUSH      price sat still, then dropped fast. The 15m bars print WICKS:
             price went there and got rejected. RSI red, deep in the bands,
             into a level that already holds volume. Sellers spent themselves
             in a hurry. -> REVERSAL, buy it.

  GRIND      price walks down bar after bar, printing BODIES: every level is
             accepted, not rejected. RSI never goes red because nothing ever
             snaps. The profile below is thin. -> FOLLOW-THROUGH, the level
             breaks and it runs, so buy puts instead.

Speed is the primary axis, which is why it is measured first and reported on
every read even when no setup fires. Wick-vs-body, RSI and the profile confirm
or deny what the speed suggests; they never override it.

This module decides SETUP and SIDE from the tape alone. It says nothing about
whether to trade - dealer positioning gets that vote, in `gex_verdict` below,
and only after the tape has spoken.

Deliberately separate from `day_shape` in tape.py. That path carries the only
out-of-sample validated edge in the desk (+27.7% net, 46 clusters, 4.4y) and
must not be disturbed by anything written here.

Pure functions: bars in, dicts out. Nothing reads a clock or a database, so
the backtest and the live desk run identical code, and a replay can never see
a bar that had not printed yet.
"""

from __future__ import annotations

import math

from common.tape import (MEANINGFUL_BODY, RSI_MA_N, _band_position, _rsi_state,
                         _TINY, rsi, session_vwap, sma, volume_profile)

# --- the leg -----------------------------------------------------------------
LEG_LOOKBACK = 20     # bars searched for where the current move began
MIN_LEG_BARS = 3      # shorter than this is noise, not a leg
RECENT = 3            # bars at the destination that judge character

# --- speed, in sigma per bar. The whole distinction lives here. --------------
# Sigma-normalised so the same numbers work on SPY, QQQ and a quiet tape.
# CALIBRATED, not guessed: scripts/calibrate_approach.py --days 90 measured the
# down-leg distribution as p10 0.24 / p50 0.58 / p75 1.09 / p90 2.65. The first
# guesses (0.30 / 0.15) sat near the 10th percentile and called 82% of all
# approaches "fast", which is not a discriminator, it is a rubber stamp.
FAST_SIGMA_PER_BAR = 1.10    # ~p75: genuinely quicker than most approaches
GRIND_SIGMA_PER_BAR = 0.35   # ~p25: genuinely slower than most
MIN_SIGMA_FRAC = 5e-4        # sigma must be >= 5bp of spot to divide by

# --- character ---------------------------------------------------------------
WICK_RATIO = 0.6        # wick >= 0.6 x body, same test tape.py's gap-run uses
WICK_FRAC = 0.5         # this share of RECENT bars wick-dominant = rejection
BODY_FRAC = 0.6         # this share body-dominant = acceptance

# --- the exhaustion pair -----------------------------------------------------
# RSI here is a LEVEL, not a position against its own average. Below the MA is
# true all the way down a grind, so it separates nothing; being genuinely
# oversold is the state that makes a bounce likely. Paired with a sudden
# high-volume bar, that is what marks the turn.
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
SPIKE_MULT = 1.8        # a RECENT bar this many x the prior average = the flush
SPIKE_BASE = 20         # bars of average volume it must beat

# --- the higher timeframes ---------------------------------------------------
# On 15m base bars. The flush is defined by what the SLOWER charts have NOT
# done yet: they are still a tail, not a close. Everything about this setup is
# "the drop has not been accepted."
HTF_MID = 3             # 3 x 15m = 45m
HTF_SLOW = 16           # 16 x 15m = 4h
HTF_WICK_FRAC = 0.45    # tail must be at least this share of the candle range
HTF_BODY_MAX = 0.40     # and the body no more than this: not yet accepted

# --- where we are ------------------------------------------------------------
LEVEL_PROX_SIGMA = 0.35   # within this of a volume wall counts as "at" it
AIR_PROX_SIGMA = 0.50     # a gap starting this close ahead counts as air
DEEP_BAND = 2.0           # |bands| beyond this is stretched enough to matter


def _aggregate(bars: list[dict], factor: int) -> list[dict]:
    """Roll base bars up into a higher timeframe. The LAST candle is the one
    still forming, which is the one that matters: we want to know what the 4h
    looks like right now, mid-print, not what it looked like when it closed."""
    out = []
    for i in range(0, len(bars), factor):
        chunk = bars[i:i + factor]
        if not chunk:
            continue
        out.append({"t": chunk[0]["t"], "o": chunk[0]["o"], "c": chunk[-1]["c"],
                    "h": max(b["h"] for b in chunk), "l": min(b["l"] for b in chunk),
                    "v": sum(float(b.get("v") or 0) for b in chunk)})
    return out


def _htf_wick(bars: list[dict], factor: int, side: int,
              cfg: dict | None = None) -> dict | None:
    """Is the higher timeframe still only a WICK here, or has it accepted?

    This is the distinction that a single timeframe cannot see. A flush prints
    down BODIES on the 15m - of course it does, it is falling fast - so 15m
    character alone cannot separate a flush from a grind. What separates them
    is that the flush leaves the 4h and the 45m sitting in a long lower tail,
    price already recovering inside a candle that was fine a moment ago, while
    a grind closes the higher timeframe down body after body: accepted.

    Anchoring on the last (incomplete) candle is the point, not a bug.
    """
    c = cfg or {}
    wick_min = c.get("htf_wick_frac", HTF_WICK_FRAC)
    body_max = c.get("htf_body_max", HTF_BODY_MAX)
    agg = _aggregate(bars, factor)
    if not agg:
        return None
    c = agg[-1]
    rng = max(c["h"] - c["l"], _TINY)
    body = abs(c["c"] - c["o"])
    wick = (min(c["o"], c["c"]) - c["l"]) if side > 0 else (c["h"] - max(c["o"], c["c"]))
    return {
        "wick_frac": round(wick / rng, 2),          # tail as a share of the candle
        "body_frac": round(body / rng, 2),
        "is_wick": wick >= wick_min * rng and body < body_max * rng,
        "recovered": round((c["c"] - c["l"]) / rng, 2) if side > 0
        else round((c["h"] - c["c"]) / rng, 2),
    }


def _body(b: dict) -> float:
    return abs(b["c"] - b["o"])


def _rng(b: dict) -> float:
    return max(b["h"] - b["l"], _TINY)


def _wick_dominant(b: dict, side: int) -> bool:
    """Rejection: the tail into the move dwarfs the body. side +1 = looking at
    a DOWN move (lower tail), -1 = an UP move (upper tail)."""
    body = max(_body(b), _TINY)
    wick = (min(b["o"], b["c"]) - b["l"]) if side > 0 else (b["h"] - max(b["o"], b["c"]))
    return wick >= WICK_RATIO * body


def _body_dominant(b: dict) -> bool:
    """Acceptance: the bar closed where it travelled, little tail either end."""
    return _body(b) / _rng(b) >= MEANINGFUL_BODY


def _leg(bars: list[dict], vw: list[dict]) -> dict | None:
    """The fastest run into the current price, and how fast it was.

    Anchoring to the extreme of a fixed lookback is wrong: "quiet for 45
    minutes, then it fell out of bed" would average the quiet part into the
    fall and report a gentle slide. The flush IS the fast stretch, so we scan
    every window from MIN_LEG_BARS up and keep whichever maximises sigma per
    bar. A flush wins on a short window; a grind reads the same at every
    width, which is exactly what makes it a grind.

    Distance is in sigma, so a 2-point drop on a calm day and a 6-point drop
    on a wild one compare honestly.
    """
    n = len(bars)
    spot = bars[-1]["c"]
    sigma = vw[-1]["sigma"]
    # A session that is a few bars old has near-zero sigma, and dividing by it
    # reports speeds in the hundreds of thousands. Sigma has to be a real
    # fraction of price before "sigma per bar" means anything at all.
    if n < MIN_LEG_BARS + 1 or sigma < MIN_SIGMA_FRAC * spot:
        return None

    best = None
    widest = min(LEG_LOOKBACK, n - 1)
    for w in range(MIN_LEG_BARS, widest + 1):
        win = bars[-w:]
        for side, anchor in ((1, max(b["h"] for b in win)),
                             (-1, min(b["l"] for b in win))):
            move = ((anchor - spot) if side > 0 else (spot - anchor)) / sigma
            if move <= 0:
                continue
            speed = move / w
            if best is None or speed > best["speed"]:
                best = {"side": side, "bars": w,
                        "sigma_moved": round(move, 2),
                        "speed": round(speed, 3),
                        "from_price": round(anchor, 2)}
    return best


def _nearest(levels: list[float], spot: float, sigma: float,
             below: bool) -> tuple[float | None, float | None]:
    """Closest level on one side, and its distance in sigma."""
    side = [x for x in levels if (x <= spot if below else x >= spot)]
    if not side or sigma <= _TINY:
        return None, None
    lvl = max(side) if below else min(side)
    return round(lvl, 2), round(abs(spot - lvl) / sigma, 2)


def _prior_pivot(bars: list[dict], price: float, sigma: float, side: int) -> bool:
    """Polarity flip: was this price a prior swing HIGH that now has to act as
    support (or a prior LOW now acting as resistance)? That is what makes a
    level confluent rather than merely a round number."""
    if sigma <= _TINY or len(bars) < 12:
        return False
    key = "h" if side > 0 else "l"
    tol = LEVEL_PROX_SIGMA * sigma
    body = bars[:-RECENT] if len(bars) > RECENT else bars
    for i in range(3, len(body) - 3):
        win = body[i - 3:i + 4]
        extreme = max(b[key] for b in win) if side > 0 else min(b[key] for b in win)
        if body[i][key] == extreme and abs(body[i][key] - price) <= tol:
            return True
    return False


def read_approach(bars: list[dict], ticker: str = "Spot",
                  cfg: dict | None = None) -> dict | None:
    """Classify the approach. None when there are not enough bars to judge.

    verdict is one of:
      "flush_reversal"  - fast, wick-rejected, oversold, into a held level
      "grind_followthru"- slow, body-accepted, RSI never snapped, air ahead
      "none"            - neither shape is clean; speed is still reported
    """
    c = cfg or {}
    def _p(k, d):                    # sweep override, else the default
        return c.get(k, d)
    if len(bars) < max(LEG_LOOKBACK, RSI_MA_N + 16):
        return None
    vw = session_vwap(bars)
    prof = volume_profile(bars)
    closes = [b["c"] for b in bars]
    rsi_s = rsi(closes)
    ma_s = sma(rsi_s, RSI_MA_N)
    spot = bars[-1]["c"]
    sigma = vw[-1]["sigma"]
    band = vw[-1]
    leg = _leg(bars, vw)
    if leg is None:
        return None

    side = leg["side"]                       # +1 price fell to here, -1 rose
    recent = bars[-RECENT:]
    wick_n = sum(1 for b in recent if _wick_dominant(b, side))
    body_n = sum(1 for b in recent if _body_dominant(b))
    wick_frac = wick_n / len(recent)
    body_frac = body_n / len(recent)

    rstate = _rsi_state(rsi_s, ma_s, len(bars) - 1) or "flat"
    rsi_now = rsi_s[-1]
    # THE LEVEL, not the slope. Below its own average stays true the whole way
    # down a grind; genuinely oversold does not.
    exhausted = rsi_now is not None and (
        rsi_now <= _p("rsi_oversold", RSI_OVERSOLD) if side > 0 else rsi_now >= (100 - _p("rsi_oversold", RSI_OVERSOLD)))

    # the sudden bar: sellers dumping all at once, not bleeding out slowly
    base = [float(b.get("v") or 0) for b in bars[-(SPIKE_BASE + RECENT):-RECENT]]
    avg_v = (sum(base) / len(base)) if base else 0.0
    peak_v = max((float(b.get("v") or 0) for b in recent), default=0.0)
    spiked = avg_v > 0 and peak_v >= _p("spike_mult", SPIKE_MULT) * avg_v

    wall, wall_d = _nearest(prof["walls"], spot, sigma, below=(side > 0))
    at_level = wall is not None and wall_d is not None and wall_d <= LEVEL_PROX_SIGMA
    confluent = at_level and _prior_pivot(bars, wall, sigma, side)

    # air = a thin pocket immediately AHEAD of the move, i.e. below a down leg
    air = None
    for g in prof["gaps"]:
        edge = g["hi"] if side > 0 else g["lo"]
        ahead = (edge <= spot) if side > 0 else (edge >= spot)
        if ahead and sigma > _TINY and abs(spot - edge) / sigma <= AIR_PROX_SIGMA:
            air = g
            break

    bpos = _band_position(spot, band)
    stretched = abs(_band_depth(spot, band)) >= DEEP_BAND

    fast = leg["speed"] >= FAST_SIGMA_PER_BAR
    slow = leg["speed"] <= GRIND_SIGMA_PER_BAR

    mid = _htf_wick(bars, _p("htf_mid", HTF_MID), side, c)     # 45m
    slowtf = _htf_wick(bars, _p("htf_slow", HTF_SLOW), side, c)  # 4h
    # "still just a wick up there": the slower charts have not accepted the move
    htf_unaccepted = bool((mid and mid["is_wick"]) or (slowtf and slowtf["is_wick"]))
    htf_accepted = bool(mid and not mid["is_wick"] and mid["body_frac"] >= HTF_BODY_MAX)

    verdict, why = "none", []
    want_fast = _p("require_fast", True)
    want_spike = _p("require_spike", True)
    want_level = _p("require_level", False)
    if ((fast or not want_fast) and htf_unaccepted and exhausted
            and (spiked or not want_spike)
            and (at_level or stretched or not want_level)):
        verdict = "flush_reversal"
        why = [f"{leg['sigma_moved']}σ in {leg['bars']} bars ({leg['speed']}σ/bar)",
               ("still a wick on the 45m" if mid and mid["is_wick"] else "")
               + (" and the 4h" if slowtf and slowtf["is_wick"] else "")
               or "higher timeframe has not accepted it",
               f"RSI {rsi_now:.0f} - oversold",
               f"volume {peak_v / avg_v:.1f}x the prior average",
               (f"at the {wall} shelf" + (" (prior pivot)" if confluent else ""))
               if at_level else f"stretched to {bpos}"]
    elif slow and htf_accepted and body_frac >= BODY_FRAC and not exhausted and air:
        verdict = "grind_followthru"
        why = [f"{leg['sigma_moved']}σ ground out over {leg['bars']} bars "
               f"({leg['speed']}σ/bar)",
               f"{body_n}/{len(recent)} bars closed on their body",
               f"RSI {rsi_now:.0f} - never got oversold",
               f"thin book {air['lo']}-{air['hi']} straight ahead"]
    else:
        why = [f"{leg['speed']}σ/bar", f"wick {wick_frac:.0%}", f"body {body_frac:.0%}",
               f"RSI {rsi_now:.0f}" if rsi_now is not None else "RSI n/a",
               "spiked" if spiked else "no volume spike",
               "air ahead" if air else "no air ahead"]

    # trade side: a flush DOWN is bought, a grind DOWN is sold
    trade = None
    if verdict == "flush_reversal":
        trade = "long" if side > 0 else "short"
    elif verdict == "grind_followthru":
        trade = "short" if side > 0 else "long"

    return {
        "ticker": ticker, "verdict": verdict, "trade": trade,
        "leg": leg, "speed": leg["speed"], "fast": fast, "slow": slow,
        "wick_frac": round(wick_frac, 2), "body_frac": round(body_frac, 2),
        "htf_45m": mid, "htf_4h": slowtf,
        "htf_unaccepted": htf_unaccepted, "htf_accepted": htf_accepted,
        "rsi": round(rsi_now, 1) if rsi_now is not None else None,
        "rsi_state": rstate, "exhausted": exhausted,
        "vol_x": round(peak_v / avg_v, 2) if avg_v > 0 else None, "spiked": spiked,
        "level": wall, "level_dist_sigma": wall_d,
        "at_level": at_level, "confluent": confluent,
        "air_ahead": air, "band_position": bpos, "spot": round(spot, 2),
        "sigma": round(sigma, 3), "why": "; ".join(why),
    }


def _band_depth(spot: float, band: dict) -> float:
    """Signed band position as a number: -2.4 means 2.4 sigma below VWAP."""
    sig = band.get("sigma") or 0.0
    if sig <= _TINY:
        return 0.0
    return (spot - band["vwap"]) / sig


# ---------------------------------------------------------------- GEX vote ---

def gex_verdict(approach: dict, regime: str | None, flip: float | None,
                put_wall: float | None, call_wall: float | None,
                score: float = 0.0) -> dict:
    """Dealer positioning votes on the tape's read, and names the target.

    The tape has already decided the setup and the side. This only answers
    two things: is dealer flow with it or against it, and where does it go.

    Short gamma means dealers chase - they push whatever direction price is
    already going. That is fuel for a follow-through and a headwind for a
    reversal. The flip is where that behaviour changes, so a reversal bought
    below the flip is bought into dealers who are still selling.
    """
    v = approach.get("verdict")
    if v not in ("flush_reversal", "grind_followthru"):
        return {"take": False, "target": None, "why": "no tape setup to confirm"}

    spot = approach["spot"]
    short_gamma = (regime == "negative_gamma")
    # a flip only carries information when it is a real, distinct level
    usable_flip = flip is not None and abs(spot - flip) > _TINY
    below_flip = usable_flip and spot < flip

    if v == "flush_reversal" and approach["trade"] == "long":
        if short_gamma and below_flip:
            return {"take": False, "target": None,
                    "why": f"short gamma under the flip at {flip:g} - dealers are "
                           "still selling into it; the bounce is fighting the flow"}
        target = call_wall if call_wall and call_wall > spot else None
        return {"take": True, "target": target,
                "first_stop": None,
                "why": ("dealers pin or are done pressing; "
                        + (f"room to the call wall at {call_wall:g}" if target
                           else "no call wall above - VWAP is the objective"))}

    if v == "grind_followthru" and approach["trade"] == "short":
        if short_gamma and below_flip:
            target = put_wall if put_wall and put_wall < spot else None
            return {"take": True, "target": target,
                    "why": ("short gamma under the flip - dealers amplify the "
                            "drop; " + (f"target the put wall at {put_wall:g}"
                                        if target else "no put wall below, run it"))}
        return {"take": False, "target": None,
                "why": "dealers are not pressing this direction - a grind "
                       "without dealer fuel stalls at the first shelf"}

    return {"take": False, "target": None,
            "why": f"no rule for {v} / {approach['trade']}"}
