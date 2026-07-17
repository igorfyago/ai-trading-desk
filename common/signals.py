"""Deterministic GEX trade engine.

The LLM never picks strikes. This module turns the latest dealer-positioning
snapshot into ONE exact, rule-based options structure; the voice/chat agents
only narrate it. Splitting "decide" (code, testable) from "explain" (model)
is the core design decision — the same split a real desk would demand.

Rules (v1, intentionally simple and inspectable):
  negative gamma  → dealers amplify moves → trade momentum, defined risk:
      spot below flip → PUT DEBIT SPREAD  (long ATM, short at the put wall)
      spot above flip → CALL DEBIT SPREAD (long ATM, short at the call wall)
  positive gamma  → dealers dampen moves → trade the range:
      strong bullish score → PUT CREDIT SPREAD below the put wall
      strong bearish score → CALL CREDIT SPREAD above the call wall
      neutral score        → IRON CONDOR with shorts at both walls
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


def recommend_trade(ticker: str, as_of: str | None = None) -> dict:
    """Exact trade for the current regime, or an error dict.
    With as_of: the REPLAY blindfold — the engine decides from the snapshot at
    or before that moment only; no live feed, no future, no exceptions."""
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
    dte = market.days_to(snap["expiry"])
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

    if snap["regime"] == "negative_gamma":
        flip_ok = flip is not None
        bullish = (spot >= flip) if flip_ok else (score >= 0)
        if not bullish:
            name, bias = "put debit spread", "bearish momentum"
            legs = [_leg(snap, atm, "put", "buy", dte),
                    _leg(snap, min(put_wall, atm - step), "put", "sell", dte)]
            invalidation = (f"spot reclaims the gamma flip at {flip}" if flip_ok
                            else f"a reclaim of the call wall at {call_wall}")
        else:
            name, bias = "call debit spread", "bullish momentum"
            legs = [_leg(snap, atm, "call", "buy", dte),
                    _leg(snap, max(call_wall, atm + step), "call", "sell", dte)]
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
                         f"({'absent' if flip_raw is None else 'sitting on price'} — the level "
                         f"carries no direction), so the side comes from the desk signal "
                         f"({score:+.0f}). Target: the {'call' if bullish else 'put'} wall.")
    else:
        if score > 25:
            name, bias = "put credit spread", "bullish range"
            legs = [_leg(snap, put_wall, "put", "sell", dte),
                    _leg(snap, put_wall - step, "put", "buy", dte)]
            invalidation = f"a close below the put wall at {put_wall}"
        elif score < -25:
            name, bias = "call credit spread", "bearish range"
            legs = [_leg(snap, call_wall, "call", "sell", dte),
                    _leg(snap, call_wall + step, "call", "buy", dte)]
            invalidation = f"a close above the call wall at {call_wall}"
        else:
            name, bias = "iron condor", "neutral range"
            legs = [_leg(snap, put_wall, "put", "sell", dte),
                    _leg(snap, put_wall - step, "put", "buy", dte),
                    _leg(snap, call_wall, "call", "sell", dte),
                    _leg(snap, call_wall + step, "call", "buy", dte)]
            invalidation = f"a close outside {put_wall}–{call_wall}"
        rationale = (f"Dealers are LONG gamma (net GEX {snap['net_gex_total']:+,.0f}); their "
                     f"hedging pins price between the walls ({put_wall} / {call_wall}). "
                     f"Sell premium at the walls dealers defend. Signal score {score:+.0f}.")

    execution = _execution_plan(snap, spot, flip, put_wall, call_wall, score, dte, atm, step, em)
    if snap["ticker"] == "XSP":
        # house rule: XSP fills are ugly on spreads - single direction only
        name = f"long {execution['kind']} (XSP: single-leg only, no spreads)"
        legs = [_leg(snap, execution["strike"], execution["kind"], "buy", dte)]
    execution["contract_plan"] = _contract_and_sizing(
        execution, snap["ticker"], snap["regime"], score)
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
        "plain_english": plain,
        "levels_note": snap.get("levels_note"),
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


def _execution_plan(snap, spot, flip, put_wall, call_wall, score, dte, atm, step, em) -> dict:
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
    if regime == "negative_gamma":
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
    if flip_ok:
        thesis_level, thesis_label = flip, "the tipping point"
    elif bullish:
        thesis_level, thesis_label = put_wall, "the put wall"
    else:
        thesis_level, thesis_label = call_wall, "the call wall"

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
        "runner_rule": "after the trim, let the rest ride to the target or expiry - "
                       "no management, worst case it expires worthless",
        "stop": None,
        "risk_plan": "no stop-loss by design: size small (half a percent of the "
                     "account max) and accept the contract can go to zero",
        "thesis_reference": round(thesis_level, 2),
        "thesis_kind": "flip" if flip_ok else "wall",
        "thesis_label": thesis_label,
        "thesis_note": (f"{thesis_label} at {thesis_level:g} is the line for the THESIS - "
                        "if it breaks, don't add and don't re-enter, but the position "
                        "itself is managed by the +50% trim and small sizing"
                        + ("" if flip_ok else
                           " (no usable gamma flip on this snapshot - the level would "
                           "just mirror the price, so the wall carries the thesis)")),
        "estimates_note": "option prices are Black-Scholes estimates at ATM vol",
    }


def _contract_and_sizing(execution: dict, ticker: str, regime: str, score: float,
                         budget: float = DEFAULT_BUDGET_USD) -> dict:
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
    strong = (regime == "negative_gamma" and abs(score) >= 40)
    if strong:
        plan, now_usd, later_usd = "full clip", budget, 0
        trigger = None
        why = "momentum tape and strong signal agree - deploy the full clip"
    elif regime == "negative_gamma":
        plan, now_usd, later_usd = "split", budget / 2, budget / 2
        trigger = ("add the second half only if the contract is up 25% or SPY "
                   "pushes through the entry level with momentum")
        why = "momentum tape but the signal is lukewarm - half now, prove it, then add"
    else:
        plan, now_usd, later_usd = "split", budget / 2, budget / 2
        trigger = "add the second half only on a clean tag of the far wall"
        why = "pinned tape - mean-reversion entries earn the add, they don't get it upfront"

    return {
        "contract_ticker": contract_ticker,
        "contract": f"{contract_strike:g}{kind_letter}",
        "contract_strike": contract_strike,
        "moneyness": "ATM",
        "conversion_note": conversion_note,
        "budget_usd": budget,
        "plan": plan,
        "now_usd": round(now_usd),
        "later_usd": round(later_usd),
        "contracts_now": max(int(now_usd // per_contract), 1),
        "add_trigger": trigger,
        "sizing_why": why,
        "doctrine": "size for zero: the whole premium is the risk, no stop",
    }


def _plain_english_exec(ticker: str, x: dict) -> str:
    """The desk script in house notation (TICKER STRIKEc/p @ PRICE): headline,
    the exact contract, trim, sizing, risk. Digits, never spelled-out numbers."""
    cp = x.get("contract_plan", {})
    kl = "c" if x["kind"] == "call" else "p"
    contract = (f"{cp.get('contract_ticker', ticker)} {cp.get('contract', '')}"
                if cp else f"{ticker} {round(x['strike']):g}{kl}")
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
        f"No stop-loss: size for zero; {x.get('thesis_label', 'the tipping point')} at "
        f"{x['thesis_reference']:g} only tells you whether the thesis still stands."
    )


