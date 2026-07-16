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

DISCLAIMER = ("Demo data + rule-based engine. Educational example only — "
              "not financial advice, not an offer to trade.")


def _latest_walls(ticker: str) -> dict:
    if db.using_live_db():
        rows = db.run_readonly(
            "SELECT call_walls, put_walls FROM gex_dex_snapshots WHERE ticker = %s"
            " ORDER BY timestamp DESC LIMIT 1", (ticker.upper(),))
        if not rows:
            return {}
        walls = {}
        for kind, raw in zip(("call", "put"), rows[0]):
            arr = raw if isinstance(raw, list) else json.loads(raw or "[]")
            if arr:  # collector orders strongest first
                walls[kind] = {"strike": arr[0]["strike"], "strength": abs(arr[0].get("gex", 0))}
        return walls
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT w.kind, w.strike, w.strength FROM walls w
        JOIN snapshots s ON s.id = w.snapshot_id
        WHERE s.ticker = ?
          AND s.captured_at = (SELECT MAX(captured_at) FROM snapshots WHERE ticker = ?)
        """,
        (ticker.upper(), ticker.upper()),
    ).fetchall()
    conn.close()
    return {kind: {"strike": strike, "strength": strength} for kind, strike, strength in rows}


def _leg(snap: dict, strike: float, kind: str, side: str, dte: float) -> dict:
    px = market.black_scholes(snap["spot"], strike, dte, snap["atm_iv"], kind)
    return {"side": side, "kind": kind, "strike": strike, "expiry": snap["expiry"],
            "indicative_price": px["price"], "delta": px["delta"]}


def recommend_trade(ticker: str) -> dict:
    """Exact trade for the current regime, or an error dict."""
    snap = market.latest_snapshot(ticker)
    if snap is None:
        return {"error": f"No data for {ticker}. Covered: SPY, QQQ, IWM, XSP."}
    walls = _latest_walls(market.resolve_feed(ticker)[0])
    if "call" not in walls or "put" not in walls:
        return {"error": f"No wall data for {ticker}."}

    spot, flip = snap["spot"], snap["gamma_flip"] or snap["spot"]
    dte = market.days_to(snap["expiry"])
    step = max(round(spot * 0.005), 1)
    atm = round(spot / step) * step
    call_wall, put_wall = walls["call"]["strike"], walls["put"]["strike"]
    score = snap["signal_score"] or 0
    em = market.expected_move(spot, snap["atm_iv"], dte)

    if snap["regime"] == "negative_gamma":
        if spot < flip:
            name, bias = "put debit spread", "bearish momentum"
            legs = [_leg(snap, atm, "put", "buy", dte),
                    _leg(snap, min(put_wall, atm - step), "put", "sell", dte)]
            invalidation = f"spot reclaims the gamma flip at {flip}"
        else:
            name, bias = "call debit spread", "bullish momentum"
            legs = [_leg(snap, atm, "call", "buy", dte),
                    _leg(snap, max(call_wall, atm + step), "call", "sell", dte)]
            invalidation = f"spot loses the gamma flip at {flip}"
        rationale = (f"Dealers are SHORT gamma (net GEX {snap['net_gex_total']:+,.0f}); their "
                     f"hedging amplifies moves. Spot {spot} is "
                     f"{'below' if spot < flip else 'above'} the flip ({flip}), so momentum "
                     f"continues until the flip is recrossed. Target: the "
                     f"{'put' if spot < flip else 'call'} wall.")
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
    plain = _plain_english_exec(snap["ticker"], execution)
    if snap.get("levels_note"):
        plain += f" ({snap['levels_note']}.)"
    return {
        "ticker": snap["ticker"], "as_of": snap["captured_at"], "spot": spot,
        "regime": snap["regime"], "gamma_flip": flip, "signal_score": score,
        "structure": name, "bias": bias, "legs": legs,
        "one_sigma_move": em, "expected_move_band": [round(spot - em, 2), round(spot + em, 2)],
        "rationale": rationale,
        "execution": execution,
        "plain_english": plain,
        "levels_note": snap.get("levels_note"),
        "invalidation": f"Exit if {invalidation}.",
        "sizing": "Risk no more than 1% of account on the structure's max loss.",
        "disclaimer": DISCLAIMER,
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
    if regime == "negative_gamma":
        bullish = spot >= flip
        headline = f"GEX says {'bullish' if bullish else 'bearish'} momentum holds today"
        why = ("dealers are forced to chase the move until the tipping point "
               f"at {flip:g} breaks")
    else:
        bullish = score >= 0
        headline = "GEX says pinned, mean-reversion tape today"
        why = (f"dealers defend the {put_wall:g}-{call_wall:g} range, so moves fade; "
               "lean " + ("long from the low end" if bullish else "short from the high end"))

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
        "thesis_reference": round(flip, 2),
        "thesis_note": f"the tipping point at {flip:g} is the line for the THESIS - "
                       "if it breaks, don't add and don't re-enter, but the position "
                       "itself is managed by the +50% trim and small sizing",
        "estimates_note": "option prices are Black-Scholes estimates at ATM vol",
    }


def _plain_english_exec(ticker: str, x: dict) -> str:
    """The desk script: headline, the buy, the trim, the risk frame."""
    return (
        f"{x['gex_headline']} - {x['gex_why']}. "
        f"Buy the {ticker} {x['strike']:g} {x['kind']}s expiring {x['expiry']} "
        f"({x['dte_days']:g} days), about {x['entry_option_price_est']:.2f} per "
        f"contract with {ticker} at {x['entry_underlying']:g}. "
        f"Sell HALF when the contract is up fifty percent - around "
        f"{x['tp50_option_price']:.2f}, which is {ticker} near "
        f"{x['tp50_underlying_est']:g} - then let the rest ride. "
        f"No stop-loss: size it small, half a percent of the account, and accept "
        f"it can go to zero; the tipping point at {x['thesis_reference']:g} only "
        f"tells you whether the thesis still stands."
    )


