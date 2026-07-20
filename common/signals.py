"""Deterministic GEX trade engine.

The LLM never picks strikes. This module turns the latest dealer-positioning
snapshot into ONE exact, rule-based options structure; the voice/chat agents
only narrate it. Splitting "decide" (code, testable) from "explain" (model)
is the core design decision — the same split a real desk would demand.

ONE DIRECTION, ALWAYS. The desk buys a single option and never spreads: the
sizing is buying math (budget / premium), the management rule is "sell half at
+50%", and the runner is then stopped at entry. Spreads and condors
cannot be sized, trimmed or held that way, so a payload that named one was
describing a trade the desk would never place.

Rules (v1, intentionally simple and inspectable):
  negative gamma  → dealers amplify moves → trade momentum:
      spot below flip → LONG PUT      spot above flip → LONG CALL
  positive gamma  → dealers dampen moves → fade the range from its edge:
      score >= 0 → LONG CALL from the low end
      score <  0 → LONG PUT from the high end
  the tape (triggered reversal, gap run, reversal day) overrides the structure
  lean and picks the side; the contract is still one long option.
"""

import json

from common import db, market
from common.db import get_connection

# House convention: ANALYZE on SPY, EXECUTE in XSP (mini-SPX).
# XSP usually trades ~+2 over SPY; strikes map SPY level + offset.
XSP_OFFSET = 2.0
DEFAULT_BUDGET_USD = 2000


def _latest_walls(ticker: str, as_of: str | None = None) -> dict:
    if db.using_live_db():
        q = ("SELECT call_walls, put_walls FROM gex_dex_snapshots WHERE ticker = %s"
             + (" AND timestamp <= %s" if as_of else ""))
        args = (ticker.upper(), as_of) if as_of else (ticker.upper(),)
        rows = db.run_readonly(q + " ORDER BY timestamp DESC LIMIT 1", args)
        if not rows:
            return {}
        walls = {}
        for kind, raw in zip(("call", "put"), rows[0]):
            arr = raw if isinstance(raw, list) else json.loads(raw or "[]")
            if arr:  # collector orders strongest first
                walls[kind] = {"strike": arr[0]["strike"], "strength": abs(arr[0].get("gex", 0))}
        return walls
    conn = get_connection()
    cap = " AND captured_at <= ?" if as_of else ""
    args = ((ticker.upper(), ticker.upper(), as_of) if as_of
            else (ticker.upper(), ticker.upper()))
    rows = conn.execute(
        f"""
        SELECT w.kind, w.strike, w.strength FROM walls w
        JOIN snapshots s ON s.id = w.snapshot_id
        WHERE s.ticker = ?
          AND s.captured_at = (SELECT MAX(captured_at) FROM snapshots
                               WHERE ticker = ?{cap})
        """,
        args,
    ).fetchall()
    conn.close()
    return {kind: {"strike": strike, "strength": strength} for kind, strike, strength in rows}


def _leg(snap: dict, strike: float, kind: str, side: str, dte: float) -> dict:
    px = market.black_scholes(snap["spot"], strike, dte, snap["atm_iv"], kind)
    return {"side": side, "kind": kind, "strike": strike, "expiry": snap["expiry"],
            "indicative_price": px["price"], "delta": px["delta"]}


def recommend_trade(ticker: str, as_of: str | None = None,
                    tape_bars: list | None = None) -> dict:
    """Exact trade for the current regime, or an error dict.
    With as_of: the REPLAY blindfold — the engine decides from the snapshot at
    or before that moment only; no live feed, no future, no exceptions.
    tape_bars: the intraday bars the tape read runs on (replay passes its own
    past-only slice; live mode fetches from the shared feed)."""
    snap = market.latest_snapshot(ticker, as_of)
    if snap is None:
        return {"error": f"No data for {ticker}. Covered: SPY, QQQ, IWM, XSP."}
    walls = _latest_walls(market.resolve_feed(ticker)[0], as_of)
    if "call" not in walls or "put" not in walls:
        return {"error": f"No wall data for {ticker}."}

    # The house split: structure (walls/flip/IV) moves slowly and comes from
    # the chain snapshot; SPOT is fast and comes from the live feed when up —
    # but only when the two agree (blendable_spot's 2% coherence band).
    # Under the replay blindfold the live feed does not exist.
    live = None if as_of else market.blendable_spot(ticker, snap)
    spot_source = "snapshot"
    if live:
        snap = {**snap, "spot": live["price"]}
        spot_source = live["source"]

    spot = snap["spot"]
    # under the blindfold, time value is measured from the decision moment
    dte = market.days_to(snap["expiry"], as_of)
    # index ETFs/XSP trade $1 strikes; 0.5%-spaced grid only for anything else
    step = 1 if ticker in ("SPY", "XSP", "QQQ", "IWM") else max(round(spot * 0.005), 1)
    atm = round(spot / step) * step
    call_wall, put_wall = walls["call"]["strike"], walls["put"]["strike"]
    score = snap["signal_score"] or 0
    em = market.expected_move(spot, snap["atm_iv"], dte)

    # A flip only counts when it is a REAL, distinct level. Missing (cumulative
    # GEX never crosses zero on a one-sided tape) or sitting on top of price,
    # it carries ZERO direction — "above the flip" would just mean "the price
    # is the price", a recursive non-signal. Then the desk SIGNAL picks the
    # side and the opposing WALL becomes the thesis level.
    flip_raw = snap["gamma_flip"]
    flip = flip_raw if (flip_raw is not None
                        and abs(spot - flip_raw) >= 0.25 * em) else None

    # ---- THE TAPE: the 2-3 hour horizon ---------------------------------
    # The desk trades the next couple of hours. The structure lean stands
    # UNLESS the house tape read (RSI red at the band, wick-held -2σ, climax
    # volume, thick back through -1σ) has TRIGGERED a reversal — then the
    # tape TAKES the trade. Armed/confirming states ride along as the spoken
    # conditional. Replay passes its own past-only bars.
    tape = None
    try:
        from common import tape as tape_mod
        if tape_bars is not None and len(tape_bars) >= 30:
            tape = tape_mod.read_tape(tape_bars, ticker=snap["ticker"])
        elif tape_bars is None and as_of is None:
            tape = tape_mod.get_tape_read(market.resolve_feed(ticker)[0])
    except Exception:
        tape = None

    # `tag` is the context the structure/tape lean carries into the name. The
    # SIDE and the CONTRACT are never decided here: execution owns both, and
    # legs are derived from it below so the two can never drift apart again.
    tag = None
    if snap["regime"] == "negative_gamma":
        flip_ok = flip is not None
        bullish = (spot >= flip) if flip_ok else (score >= 0)
        if not bullish:
            bias = "bearish momentum"
            invalidation = (f"spot reclaims the gamma flip at {flip}" if flip_ok
                            else f"a reclaim of the call wall at {call_wall}")
        else:
            bias = "bullish momentum"
            invalidation = (f"spot loses the gamma flip at {flip}" if flip_ok
                            else f"a loss of the put wall at {put_wall}")
        if flip_ok:
            rationale = (f"Dealers are SHORT gamma (net GEX {snap['net_gex_total']:+,.0f}); their "
                         f"hedging amplifies moves. Spot {spot} is "
                         f"{'above' if bullish else 'below'} the flip ({flip}), so momentum "
                         f"continues until the flip is recrossed. Target: the "
                         f"{'call' if bullish else 'put'} wall.")
        else:
            rationale = (f"Dealers are SHORT gamma (net GEX {snap['net_gex_total']:+,.0f}); their "
                         f"hedging amplifies moves. No usable flip on this snapshot "
                         f"({'absent' if flip_raw is None else 'sitting on price'}: the level "
                         f"carries no direction), so the side comes from the desk signal "
                         f"({score:+.0f}). Target: the {'call' if bullish else 'put'} wall.")
    else:
        # execution's rule mirrored exactly (score picks the side): the desk
        # FADES the range from its edge with one long contract. The old
        # branches sold premium at the walls, which the buying sizing, the
        # +50% trim and hold-to-zero could never have executed.
        bullish = score >= 0
        bias = ("bullish range - long from the low end" if bullish
                else "bearish range - short from the high end")
        invalidation = (f"a close below the put wall at {put_wall}" if bullish
                        else f"a close above the call wall at {call_wall}")
        rationale = (f"Dealers are LONG gamma (net GEX {snap['net_gex_total']:+,.0f}); their "
                     f"hedging pins price between the walls ({put_wall} / {call_wall}), "
                     f"so moves fade. Signal score {score:+.0f} leans "
                     + ("long from the low end." if bullish
                        else "short from the high end."))

    # Precedence: a takeable REVERSAL DAY takes the trade - it is the only
    # branch with an out-of-sample record. Failing that, a TRIGGERED tape
    # reversal, then a gap run, then the structure lean. The desk trades the
    # next 2-3 hours.
    ds = (tape or {}).get("day_shape")
    # same precedence fix as _execution_plan: the branch with an out-of-sample
    # record is not allowed to be preempted by the two branches without one
    # capitulation counts as validated HERE TOO. _execution_plan already
    # treats it that way, and when only one of the two ladders knows about it
    # the payload describes a short in `bias` and `rationale` while `execution`
    # is buying calls - one object, two opposite trades.
    cap_r = (tape or {}).get("capitulation") if tape else None
    _validated = bool((ds and ds.get("takeable", True)) or cap_r)
    if cap_r:
        tag = "capitulation flush"
        bias = "bullish - capitulation flush, sellers spent"
        invalidation = ("a 15m close back below the session VWAP at "
                        f"{tape['vwap']:.2f}")
        rationale = ("Capitulation flush: " + cap_r["why"] +
                     " - the desk buys the bounce; the session VWAP is the thesis.")
    elif (tape and not _validated
            and tape.get("stage") == "triggered" and tape.get("bias")):
        t_kind = "call" if tape["bias"] == "long" else "put"
        tag = "tape reversal triggered"
        bias = f"{'bullish' if t_kind == 'call' else 'bearish'} reversal - tape triggered"
        invalidation = (f"a 15m close back {'below' if t_kind == 'call' else 'above'} "
                        f"the session VWAP at {tape['vwap']:.2f}")
        rationale = ("The house tape read fired: " + tape["plain"] +
                     " A triggered reversal takes the trade over the structure lean.")
    elif (not _validated and (grun := (tape or {}).get("gap_run"))
          and grun.get("fired")):
        # thicks rule, then volume profiles: wicks based at the band, a thick
        # candle crossed the trigger into a THIN book - it travels fast
        g_kind = "call" if grun["side"] == "long" else "put"
        tag = "gap run"
        bias = f"{'bullish' if g_kind == 'call' else 'bearish'} momentum - gap run"
        invalidation = (f"a 15m close back {'below' if g_kind == 'call' else 'above'} "
                        f"the trigger at {grun['trigger']}")
        rationale = (f"Gap run: wicks based {'between -1σ and VWAP' if g_kind == 'call' else 'between VWAP and +1σ'}, "
                     f"a thick 15m candle crossed {grun['trigger']} and the profile is "
                     f"thin to {grun['target']} - price travels an empty book fast.")
    elif ds and ds.get("takeable", True):
        # the gate rode in on the tape read: 4.4y out-of-sample (docs/BACKTEST.md)
        # graded pre-12:45/stale-capitulation entries flat-to-negative, so those
        # stay context and the structure trade stands until the window opens
        d_kind = "call" if ds["shape"].startswith("bull") else "put"
        tag = "reversal day"
        bias = (f"{'bullish' if d_kind == 'call' else 'bearish'} - reversal day "
                f"(capitulation + double {'bottom' if d_kind == 'call' else 'top'})")
        invalidation = (f"a 15m close back {'below' if d_kind == 'call' else 'above'} "
                        f"the session VWAP at {tape['vwap']:.2f}")
        rationale = (f"Reversal day: capitulation ({ds['capitulation_x']}x volume) at the "
                     f"double {'bottom' if d_kind == 'call' else 'top'} and VWAP "
                     f"{'reclaimed' if d_kind == 'call' else 'lost'} - after capitulation "
                     "the desk stops leaning with the old trend.")
    elif ds and ((ds["shape"].startswith("bull") and score <= 0)
                 or (ds["shape"].startswith("bear") and score >= 0)):
        # established reversal day vs an opposing structure lean: the desk
        # NEVER fades a capitulation day. Late chases graded flat-to-negative
        # too (docs/BACKTEST.md) - so the call is the day's side, pullback-only.
        d_kind = "call" if ds["shape"].startswith("bull") else "put"
        tag = "reversal day - pullback only"
        bias = (f"{'bullish' if d_kind == 'call' else 'bearish'} - reversal day "
                "(pullback only, no chase)")
        invalidation = (f"a 15m close back {'below' if d_kind == 'call' else 'above'} "
                        f"the session VWAP at {tape['vwap']:.2f}")
        rationale = ("Reversal day on the tape - the desk never fades it, and the "
                     "clean entry window has passed: no chase at market, work a "
                     f"pullback that holds the session VWAP at {tape['vwap']:.2f}.")

    execution = _execution_plan(snap, spot, flip, put_wall, call_wall, score,
                                dte, atm, step, em, tape=tape)
    # ONE OPTION, ALWAYS, for every ticker. execution is the single authority
    # on side and strike, so deriving the legs from it is the only way they
    # cannot drift: the payload used to announce "put debit spread" beside a
    # single 744p, and the sizing underneath was buying math either way.
    legs = [_leg(snap, execution["strike"], execution["kind"], "buy", dte)]
    name = f"long {execution['kind']}" + (f" ({tag})" if tag else "")
    confluence = _confluence(execution["kind"], snap, flip, score, tape)
    execution["contract_plan"] = _contract_and_sizing(
        execution, snap["ticker"], snap["regime"], score,
        verdict=confluence["verdict"], tape=tape)
    plain = _plain_english_exec("SPY" if snap["ticker"] in ("SPY", "XSP") else snap["ticker"],
                                execution)
    return {
        "ticker": snap["ticker"], "as_of": snap["captured_at"], "spot": spot,
        "spot_source": spot_source, "snapshot_iv": snap["atm_iv"],
        "regime": snap["regime"], "gamma_flip": flip_raw, "signal_score": score,
        "structure": name, "bias": bias, "legs": legs,
        "one_sigma_move": em, "expected_move_band": [round(spot - em, 2), round(spot + em, 2)],
        "rationale": rationale,
        "execution": execution,
        "confluence": confluence,
        "plain_english": plain,
        # the order alone, for the caller who is copying it into a broker
        "copy_trade": _copy_trade_line(execution),
        "levels_note": snap.get("levels_note"),
        "horizon": "the next 2-3 hours",
        "tape": None if tape is None else {
            "stage": tape.get("stage"), "bias": tape.get("bias"),
            "target": tape.get("target"), "vwap": tape.get("vwap"),
            "rsi": (tape.get("rsi") or {}).get("state"),
            "day_shape": (tape.get("day_shape") or {}).get("shape"),
            "day_shape_takeable": (tape.get("day_shape") or {}).get("takeable"),
            "cap_age_h": (tape.get("day_shape") or {}).get("age_h"),
            "capitulation_x": (tape.get("day_shape") or {}).get("capitulation_x"),
            "checklist": tape.get("checklist"),
            "action": tape.get("action"),
            "gap_run": tape.get("gap_run"),
            "bands": tape.get("bands"),
            "band_position": tape.get("band_position"),
            "plain": tape.get("plain"),
        },
        "invalidation": f"Exit if {invalidation}.",
        "sizing": "Risk no more than 1% of account on the structure's max loss.",
    }


def _spot_for_option_price(target_px, strike, dte, iv, kind, spot_hint):
    """Invert Black-Scholes: the underlying level where the option trades at
    target_px. Bisection — price is monotonic in spot for a single leg."""
    lo, hi = spot_hint * 0.55, spot_hint * 1.6
    for _ in range(70):
        mid = (lo + hi) / 2
        px = market.black_scholes(mid, strike, dte, iv, kind)["price"]
        if kind == "call":            # call price rises with spot
            if px < target_px:
                lo = mid
            else:
                hi = mid
        else:                         # put price falls as spot rises
            if px < target_px:
                hi = mid
            else:
                lo = mid
    return round((lo + hi) / 2, 2)


def _execution_plan(snap, spot, flip, put_wall, call_wall, score, dte, atm, step, em,
                    tape=None) -> dict:
    """The desk's management rules, computed deterministically:
      - ONE option to buy (0-5 DTE), direction from the GEX regime
      - take profit on OPTION P&L: sell HALF at +50%% on the contract,
        with the underlying level where that happens (BS inversion)
      - NO stop-loss: size small, hold to zero if wrong, let the runner ride;
        the tipping point is a thesis reference, never a tripwire
    """
    regime = snap["regime"]
    ticker = snap["ticker"]
    flip_ok = flip is not None
    dshape = (tape or {}).get("day_shape") if tape else None
    # THE VALIDATED SETUP OUTRANKS THE UNVALIDATED ONES. A takeable day shape
    # is the only branch here with a positive out-of-sample record. The
    # standalone trigger graded 38.5% hit / negative EV, and the gap run has
    # never been tested at all - yet both used to take the trade ahead of it,
    # purely by sitting earlier in the chain. They are demoted, not deleted:
    # they still run when there is no day shape to preempt.
    cap = (tape or {}).get("capitulation") if tape else None
    _validated = bool((dshape and dshape.get("takeable", True)) or cap)
    trig = tape if (tape and not _validated
                    and tape.get("stage") == "triggered"
                    and tape.get("bias")) else None
    est = False
    if dshape and not dshape.get("takeable", True):
        # outside the entry window the shape still VETOES counter-trend: the
        # desk never fades a capitulation day. If structure already points the
        # day's way it runs the show; if it opposes, plan the pullback instead.
        opposes = ((dshape["shape"].startswith("bull") and score <= 0)
                   or (dshape["shape"].startswith("bear") and score >= 0))
        est = opposes
        if not opposes:
            dshape = None
    grun = (tape or {}).get("gap_run") if tape else None
    if grun and (not grun.get("fired") or _validated):
        grun = None                       # loaded rides as the conditional, not the plan
    if trig:
        bullish = trig["bias"] == "long"
        headline = f"TAPE says {'long' if bullish else 'short'} reversal - triggered"
        why = ("the checklist fired: the 2-sigma band held on wicks, climax "
               "volume marked the turn, and a thick candle crossed the "
               "1-sigma band - price runs the low-volume gap")
    elif grun:
        bullish = grun["side"] == "long"
        headline = f"TAPE says {'bullish' if bullish else 'bearish'} gap run - thin book ahead"
        why = (f"wicks based at the band, a thick close crossed {grun['trigger']} "
               f"and the profile is empty to {grun['target']} - it travels fast")
    elif cap:
        # The capitulation flush. Out-of-sample on DIA 2022-2026 (PF 3.05) and
        # IWM 2018-2021 (PF 3.41) at 0DTE under house management after 5%
        # per-side friction - the only entry here confirmed on instruments it
        # was not built from. Long only: the short mirror lost in every test.
        bullish = True
        headline = "TAPE says capitulation flush - sellers spent"
        why = cap["why"] + " - long the bounce, VWAP is the thesis"
    elif dshape:
        bullish = dshape["shape"].startswith("bull")
        headline = (f"TAPE says {'bullish' if bullish else 'bearish'} reversal day"
                    + (" - pullback only, no chase" if est else ""))
        why = (f"capitulation ({dshape['capitulation_x']}x volume) at the double "
               f"{'bottom' if bullish else 'top'} and VWAP "
               f"{'reclaimed' if bullish else 'lost'} - the old trend is done for today")
        if est:
            why += ("; the clean entry window has passed, so no chase at market - "
                    "work a pullback that holds the session VWAP")
    elif regime == "negative_gamma":
        bullish = (spot >= flip) if flip_ok else (score >= 0)
        headline = f"GEX says {'bullish' if bullish else 'bearish'} momentum holds today"
        if flip_ok:
            why = ("dealers are forced to chase the move until the tipping point "
                   f"at {flip:g} breaks")
        else:
            why = ("dealers amplify the move and the desk signal "
                   f"({score:+.0f}) sits with the {'buyers' if bullish else 'sellers'} "
                   "- the flip is on price, no edge from the level itself")
    else:
        bullish = score >= 0
        headline = "GEX says pinned, mean-reversion tape today"
        why = (f"dealers defend the {put_wall:g}-{call_wall:g} range, so moves fade; "
               "lean " + ("long from the low end" if bullish else "short from the high end"))
    # the thesis line: the flip when it is a real level, else the wall the
    # trade leans on — always a DISTINCT price, never a mirror of spot
    if trig or dshape or grun:
        thesis_level, thesis_label = tape["vwap"], "the session VWAP"
    elif flip_ok:
        thesis_level, thesis_label = flip, "the tipping point"
    elif bullish:
        thesis_level, thesis_label = put_wall, "the put wall"
    else:
        thesis_level, thesis_label = call_wall, "the call wall"

    # HORIZON DISCIPLINE: the desk trades the next 2-3 hours. A thesis line
    # beyond today's realistic reach (~1.2x the one-day expected move) is
    # CONTEXT, not the working line — the local level takes over.
    context_level = context_label = None
    reach = market.expected_move(spot, snap["atm_iv"], min(dte, 1.0)) * 1.2
    if not trig and not dshape and not grun and abs(thesis_level - spot) > reach:
        context_level, context_label = thesis_level, thesis_label
        if tape and tape.get("vwap"):
            thesis_level, thesis_label = tape["vwap"], "the session VWAP"
        else:
            near = min((atm, put_wall, call_wall), key=lambda v: abs(v - spot))
            thesis_level, thesis_label = near, "the nearest level"

    # the achievable TARGET: the tape's gap/wall on a triggered reversal; the
    # next profile wall in the trade direction on a reversal day
    if trig:
        target = trig.get("target")
    elif dshape:
        prof = tape.get("profile") or {}
        target = prof.get("wall_above") if bullish else prof.get("wall_below")
        if target is None and tape:
            # A trigger demoted behind the day shape lost the SIDE vote, not
            # its read of where the tape was headed. When the profile offers
            # no wall this way, keep that target instead of quoting none:
            # demoting a signal should cost it precedence, not information.
            target = tape.get("target")
    else:
        target = None

    kind = "call" if bullish else "put"
    entry_px = market.black_scholes(spot, atm, dte, snap["atm_iv"], kind)["price"]
    tp_dte = max(dte * 0.6, 0.4)
    tp50_px = round(entry_px * 1.5, 2)
    tp50_u = _spot_for_option_price(tp50_px, atm, tp_dte, snap["atm_iv"], kind, spot)

    return {
        "gex_headline": headline, "gex_why": why,
        "action": "buy", "kind": kind, "strike": atm, "expiry": snap["expiry"],
        "dte_days": round(dte, 1),
        "entry_underlying": round(spot, 2),
        "entry_option_price_est": entry_px,
        "tp_rule": "sell HALF when the option is up 50%",
        "tp50_option_price": tp50_px,
        "tp50_underlying_est": tp50_u,
        # THE TESTED MANAGEMENT. "let it ride to expiry, no management" is not
        # what was measured: the runner is stopped at ENTRY if it round-trips,
        # and taken at +400%. The breakeven stop is what turns the average
        # loser from -100% into something survivable, and it is the single
        # biggest contributor to the profit factor.
        "runner_rule": (f"after the trim, stop the runner at {entry_px:.2f} "
                        f"(entry - the trade is free from there) or let it ride "
                        f"to {entry_px * 5:.2f}"),
        "runner_stop_price": entry_px,
        "runner_target_price": round(entry_px * 5, 2),
        "stop": None,
        "risk_plan": "no stop until the trim: size small (half a percent of the "
                     "account max) and accept the contract can go to zero",
        "thesis_reference": round(thesis_level, 2),
        # derived from the label that actually shipped: the dshape, gap-run,
        # capitulation and horizon-demotion branches all reassign thesis_label
        # without touching this, so a hardcoded value drifts out of agreement
        "thesis_kind": ("tape" if thesis_label == "the session VWAP"
                        else "flip" if thesis_label == "the tipping point"
                        else "wall" if thesis_label.endswith(" wall") else "level"),
        "thesis_label": thesis_label,
        "target": None if target is None else round(target, 2),
        "context_level": None if context_level is None else round(context_level, 2),
        "context_label": context_label,
        "horizon": "the next 2-3 hours",
        "thesis_note": (f"{thesis_label} at {thesis_level:g} is the line for the THESIS - "
                        "if it breaks, don't add and don't re-enter, but the position "
                        "itself is managed by the +50% trim and small sizing"
                        + (f" ({context_label} at {context_level:g} is context - beyond "
                           f"today's realistic reach)" if context_level is not None else "")
                        + ("" if (flip_ok or trig) else
                           " (no usable gamma flip on this snapshot - the level would "
                           "just mirror the price, so the wall carries the thesis)")),
        "estimates_note": "option prices are Black-Scholes estimates at ATM vol",
    }


def _confluence(kind: str, snap: dict, flip, score: float, tape: dict | None) -> dict:
    """THE DESK MODEL, as one transparent scorecard: the original intent is
    GEX context PLUS the house reversal checklist, and full conviction only
    when the boxes are green TOGETHER. Every box carries its evidence; the
    verdict drives sizing. No box, no override, no precedence magic:
      full_confluence  every supporting box green, none red  -> full clip
      partial          real support with a dissenter          -> half, prove it
      wait             the tape hasn't earned a fill          -> plan only
    Validation status is honest: the day-shape window is the OOS-validated
    edge (docs/BACKTEST.md); a triggered tape ALONE graded ~coin-flip, which
    is exactly why it now needs the structure box green to reach full size."""
    side = 1 if kind == "call" else -1
    tape = tape or {}
    ds = tape.get("day_shape") or {}
    cl = tape.get("checklist") or {}
    stage = tape.get("stage") or "none"
    boxes = []

    def box(name, state, why):
        boxes.append({"name": name, "state": state, "why": why})
        return state

    # 1. GEX structure: does dealer positioning back this side?
    struct_dir = 1 if ((spotv := snap["spot"]) >= flip if flip is not None
                       else score >= 0) else -1
    if abs(score) < 15 and flip is None:
        s1 = box("gex structure", "gray", f"signal {score:+.0f} is noise, no usable flip")
    elif struct_dir == side:
        s1 = box("gex structure", "green",
                 f"{snap['regime'].replace('_', ' ')}, signal {score:+.0f}"
                 + (f", spot {'above' if side > 0 else 'below'} the flip {flip:g}"
                    if flip is not None else ""))
    else:
        s1 = box("gex structure", "red", f"dealer lean is the OTHER way (signal {score:+.0f})")

    # 2. Day shape: the validated reversal-day window
    ds_dir = 0 if not ds else (1 if ds.get("shape", "").startswith("bull") else -1)
    if ds_dir == side and ds.get("takeable"):
        s2 = box("day shape", "green",
                 f"reversal day, capitulation {ds.get('capitulation_x')}x, window OPEN")
    elif ds_dir == side:
        s2 = box("day shape", "gray", "reversal day on the tape, entry window closed - pullback only")
    elif ds_dir and ds_dir != side:
        s2 = box("day shape", "red", "the DAY reversed the other way - never fade it")
    else:
        s2 = box("day shape", "gray", "no reversal day on the tape")

    # 3. His four checks, on this side
    cl_dir = 0 if not cl else (1 if cl.get("side") == "long" else -1)
    done = cl.get("done", 0)
    if cl_dir == side and done >= 3:
        s3 = box("checklist", "green", f"{done}/4 checks in on this side")
    elif cl_dir == side:
        s3 = box("checklist", "gray", f"only {done}/4 checks in")
    elif cl_dir and done >= 3:
        s3 = box("checklist", "red", f"the tape is building the OTHER side ({done}/4)")
    else:
        s3 = box("checklist", "gray", "no active setup on this side")

    # 4. Stage: how far the machine has walked (a FIRED gap run counts - it
    # is his continuation trigger, same thicks-rule mechanics)
    gap = tape.get("gap_run") or {}
    gap_dir = 0 if not gap else (1 if gap.get("side") == "long" else -1)
    tape_dir = 0 if not tape.get("bias") else (1 if tape["bias"] == "long" else -1)
    if stage == "triggered" and tape_dir == side:
        s4 = box("stage", "green", "TRIGGERED - the confirm printed")
    elif gap.get("fired") and gap_dir == side:
        s4 = box("stage", "green", f"GAP RUN fired - thick close through {gap.get('trigger')}")
    elif stage == "confirming" and tape_dir == side:
        s4 = box("stage", "gray", "confirming - the thick close hasn't printed")
    elif gap.get("ready") and gap_dir == side:
        s4 = box("stage", "gray", f"gap run loaded - thick close through {gap.get('trigger')} starts it")
    elif stage in ("armed", "confirming") and tape_dir == -side:
        s4 = box("stage", "red", f"the machine is {stage} the OTHER way")
    else:
        s4 = box("stage", "gray", f"stage: {stage}")

    # 5. Location: an actionable line within reach on this side
    act = tape.get("action") or {}
    line = act.get("down") if side < 0 else act.get("up")
    entry_line = line or (act.get("up") if side < 0 else act.get("down"))
    if entry_line:
        s5 = box("location", "green",
                 f"entry line {entry_line['level']} within reach ({entry_line['dist']:+})")
    else:
        s5 = box("location", "gray", "no actionable line within reach on this side")

    states = [s1, s2, s3, s4, s5]
    reds = states.count("red")
    support = ((s2 == "green") or (s3 == "green" and s4 == "green")
               or (gap.get("fired") and gap_dir == side))
    if support and s1 == "green" and reds == 0:
        verdict = "full_confluence"
    elif reds >= 2 or (reds and not support and s1 != "green"):
        verdict = "wait"
    elif support or s1 == "green" or s3 == "green":
        verdict = "partial"
    else:
        verdict = "wait"
    return {"side": "long" if side > 0 else "short", "verdict": verdict,
            "greens": states.count("green"), "reds": reds, "boxes": boxes}


def _contract_and_sizing(execution: dict, ticker: str, regime: str, score: float,
                         budget: float = DEFAULT_BUDGET_USD,
                         verdict: str | None = None,
                         tape: dict | None = None) -> dict:
    """House conventions on top of the plan:
      - S&P trades: analysis stays in SPY levels, the CONTRACT is XSP
        (strike = SPY strike + ~2), notation like '753p'
      - sizing algo for the clip (default $2k): full clip when the regime and
        signal both back the move; split (half now, half on confirmation)
        when conviction is partial. Premium buyer, hold-to-zero sizing.
    """
    kind_letter = "p" if execution["kind"] == "put" else "c"
    if ticker in ("SPY", "XSP"):
        contract_ticker = "XSP"
        contract_strike = round(execution["strike"] + XSP_OFFSET)
        conversion_note = (f"analysis on SPY; execute in XSP at about +{XSP_OFFSET:g} "
                           "- confirm the live offset at the broker")
    else:
        contract_ticker = ticker
        contract_strike = round(execution["strike"])
        conversion_note = None

    est_px = execution["entry_option_price_est"]
    per_contract = max(est_px * 100, 1)

    # THE TRIGGER HAS TO CARRY A NUMBER. Every branch below used to describe
    # its condition in prose and REFER to a level ("the entry level", "the far
    # wall", "the missing board boxes") while the actual price sat unused in
    # scope. The desk can only ever be as exact as this string: handed prose,
    # the best it can do is paraphrase, which is how "when would it flip" got
    # answered with "it flips when the rules are met, and if they aren't it
    # doesn't". The second lot is earned by CONTINUATION: price carrying
    # through the next band in the direction of the trade.
    short = execution["kind"] == "put"
    bands = (tape or {}).get("bands") or {}
    cont = bands.get("d1") if short else bands.get("u1")
    if cont is None:                       # no bands on this snapshot
        cont = execution.get("entry_underlying")
    thru = "under" if short else "through"
    second = round(budget / 2)
    # levels are always spoken in the ANALYSIS ticker, never the contract one:
    # the same rule _plain_english_exec follows, so 744.44 never reads as XSP
    spoken = "SPY" if ticker in ("SPY", "XSP") else ticker

    def _at(level, fallback: str) -> str:
        """A level, or the honest prose when there genuinely isn't one."""
        return f"{level:.2f}" if isinstance(level, (int, float)) else fallback
    # CONFLUENCE DRIVES SIZE: the board's verdict outranks the old
    # regime/score heuristic whenever it is available
    if verdict == "full_confluence":
        plan, now_usd, later_usd = "full clip", budget, 0
        trigger = None
        why = "every box on the board is green - structure and the checklist agree, full clip"
    elif verdict == "partial":
        plan, now_usd, later_usd = "split", budget / 2, budget / 2
        trigger = (f"add the second ${second:g} when {spoken} crosses {thru} "
                   f"{_at(cont, 'the next band')}")
        why = "the board is part-green - half now, the tape earns the add"
    elif verdict == "wait" and not (tape or {}).get("capitulation"):
        plan, now_usd, later_usd = "plan", 0, budget
        trigger = (f"no fill until {spoken} trades {thru} {_at(cont, 'the entry level')} "
                   "- this is the plan, not an order")
        why = "the board says wait - quoting the structure so you're ready, not filled"
    elif regime == "negative_gamma" and abs(score) >= 40:
        plan, now_usd, later_usd = "full clip", budget, 0
        trigger = None
        why = "momentum tape and strong signal agree - deploy the full clip"
    elif regime == "negative_gamma":
        plan, now_usd, later_usd = "split", budget / 2, budget / 2
        trigger = (f"add the second ${second:g} if the contract is up 25% or {spoken} "
                   f"pushes {thru} {_at(execution.get('entry_underlying'), 'the entry level')}")
        why = "momentum tape but the signal is lukewarm - half now, prove it, then add"
    else:
        plan, now_usd, later_usd = "split", budget / 2, budget / 2
        trigger = (f"add the second ${second:g} on a clean tag of "
                   f"{_at(execution.get('context_level'), 'the far wall')}")
        why = "pinned tape - mean-reversion entries earn the add, they don't get it upfront"

    # ALWAYS SPOKEN AS A PAIR. The contract strike is NOT the level the caller
    # thinks in: XSP 744p is the SPY 742 level, and quoting one without the
    # other derails whole calls ("2.42 for the XSP 744 put doesn't tell me the
    # SPY equivalent"). The desk analyses in one ticker and fills in another,
    # so every strike it says has to carry both or it is only half a fact.
    contract_spoken = f"{contract_ticker} {contract_strike:g}{kind_letter}"
    if contract_ticker != spoken:
        contract_spoken += f" (= {spoken} {execution['strike']:g})"

    return {
        "contract_ticker": contract_ticker,
        "contract": f"{contract_strike:g}{kind_letter}",
        "contract_strike": contract_strike,
        "contract_spoken": contract_spoken,
        "analysis_strike": execution["strike"],
        "analysis_ticker": spoken,
        "moneyness": "ATM",
        "conversion_note": conversion_note,
        "budget_usd": budget,
        "plan": plan,
        "now_usd": round(now_usd),
        "later_usd": round(later_usd),
        "contracts_now": (max(int(now_usd // per_contract), 1) if now_usd > 0 else 0),
        "add_trigger": trigger,
        "sizing_why": why,
        "doctrine": "size for zero before the trim; after it the runner stops at entry",
    }


def _copy_trade_line(x: dict) -> str:
    """The order, and NOTHING else, composed in code so no model can pad it.

    This is what a caller copy-trades: what to buy, at what price, how much,
    where the trim is, and the exact level that earns the second lot. No
    headline, no rationale, no jargon, no hedging - those are available if
    asked, and asking is cheap. A model handed the raw payload re-composes
    this from scratch every call and pads it out; a model handed this line
    can only read it.
    """
    cp = x.get("contract_plan") or {}
    kl = "p" if x["kind"] == "put" else "c"
    what = f"{cp.get('contract_ticker')} {cp.get('contract_strike'):g}{kl}" if cp \
        else f"{x['strike']:g}{kl}"
    # the price belongs to the CONTRACT, so the equivalence comes after it:
    # "XSP 744p, that's SPY 742, at 1.34" invites hearing 1.34 as the SPY level
    also = (f", that's {cp.get('analysis_ticker')} {cp.get('analysis_strike'):g}"
            if cp.get("analysis_strike") is not None else "")

    if cp.get("plan") == "plan" or not cp.get("contracts_now"):
        # nothing to fill yet: the condition IS the whole instruction
        return f"Nothing on yet. {_sentence(cp.get('add_trigger') or 'wait for the setup')}"

    bits = [f"Buy {what} at {x['entry_option_price_est']:.2f}{also}",
            f"${cp.get('now_usd'):g}, {cp.get('contracts_now')} contracts"]
    if x.get("tp50_option_price"):
        bits.append(f"sell half at {x['tp50_option_price']:.2f}")
    if x.get("runner_stop_price"):
        # the caller is copying this into a broker: the runner needs its two
        # exits stated as prices, not as a doctrine they have to translate
        bits.append(f"then stop the rest at {x['runner_stop_price']:.2f} "
                    f"or ride to {x['runner_target_price']:.2f}")
    if cp.get("later_usd"):
        bits.append(str(cp.get("add_trigger")))
    return " ".join(_sentence(b) for b in bits)


def _sentence(s: str) -> str:
    """One clause, spoken: leading capital, closing full stop."""
    s = s.strip().rstrip(".")
    return (s[:1].upper() + s[1:] + ".") if s else ""


def _plain_english_exec(ticker: str, x: dict) -> str:
    """The desk script in house notation (TICKER STRIKEc/p @ PRICE): headline,
    the exact contract, trim, sizing, risk. Digits, never spelled-out numbers."""
    cp = x.get("contract_plan", {})
    kl = "c" if x["kind"] == "call" else "p"
    # the paired form, so the script itself never says a strike in one ticker
    # while the caller is thinking in the other
    contract = (cp.get("contract_spoken")
                or f"{cp.get('contract_ticker', ticker)} {cp.get('contract', '')}"
                ) if cp else f"{ticker} {round(x['strike']):g}{kl}"
    sizing_bit = (
        f"Clip ${cp.get('budget_usd', 2000):g}: "
        + (f"all of it now ({cp.get('contracts_now')}x) - {cp.get('sizing_why')}. "
           if cp.get("plan") == "full clip" else
           f"half now ({cp.get('contracts_now')}x, ${cp.get('now_usd')}), "
           f"the other half waits - {cp.get('add_trigger')}. ")
    ) if cp else ""
    return (
        f"{x['gex_headline']} - {x['gex_why']}. "
        f"With {ticker} at {x['entry_underlying']:g}: buy {contract} @ "
        f"~{x['entry_option_price_est']:.2f}, expiring {x['expiry']} "
        f"({x['dte_days']:g} DTE). "
        f"Sell HALF when the contract is up fifty percent - {contract} @ "
        f"{x['tp50_option_price']:.2f}, {ticker} near {x['tp50_underlying_est']:g} - "
        f"then let the rest ride. "
        f"{sizing_bit}"
        + (f"Target {x['target']:g} - the low-volume gap, achievable today - "
           "then reassess. " if x.get("target") else "")
        + f"No stop before the trim - size for zero, then the runner stops at entry; {x.get('thesis_label', 'the tipping point')} at "
        f"{x['thesis_reference']:g} only tells you whether the thesis still stands."
    )


