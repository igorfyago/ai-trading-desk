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
        return {"error": f"No data for {ticker}. Covered: SPY, QQQ, IWM."}
    walls = _latest_walls(ticker)
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
    return {
        "ticker": snap["ticker"], "as_of": snap["captured_at"], "spot": spot,
        "regime": snap["regime"], "gamma_flip": flip, "signal_score": score,
        "structure": name, "bias": bias, "legs": legs,
        "one_sigma_move": em, "expected_move_band": [round(spot - em, 2), round(spot + em, 2)],
        "rationale": rationale,
        "execution": execution,
        "plain_english": _plain_english_exec(snap["ticker"], execution),
        "invalidation": f"Exit if {invalidation}.",
        "sizing": "Risk no more than 1% of account on the structure's max loss.",
        "disclaimer": DISCLAIMER,
    }


def _execution_plan(snap, spot, flip, put_wall, call_wall, score, dte, atm, step, em) -> dict:
    """The desk format: ONE option to buy (0-5 DTE), the underlying
    entry level + condition, the underlying level to sell HALF at (with the
    option's rough value there), and a hard stop. All rule-derived.
    """
    regime = snap["regime"]
    if regime == "negative_gamma":
        bullish = spot >= flip
        kind = "call" if bullish else "put"
        entry_u, condition = spot, (f"while {snap['ticker']} holds "
                                    f"{'above' if bullish else 'below'} {flip:g}")
        tp50_u = call_wall if bullish else put_wall
        stop_u, conviction = flip, "standard"
    else:
        bullish = score >= 0
        kind = "call" if bullish else "put"
        entry_u = put_wall if bullish else call_wall     # buy the bounce off the wall
        condition = (f"on a touch of the {'put' if bullish else 'call'} wall at {entry_u:g} "
                     "(long-gamma tape mean-reverts — don't chase mid-range)")
        tp50_u = call_wall if bullish else put_wall
        stop_u = round(entry_u - step if bullish else entry_u + step, 2)
        conviction = "reduced - long-gamma tape dampens moves; prefer the credit structure"

    # target must be worth taking: at least 0.6 sigma from entry, else extend
    min_dist = max(em * 0.6, step)
    if abs(tp50_u - entry_u) < min_dist:
        tp50_u = round(entry_u + min_dist if bullish else entry_u - min_dist, 2)

    strike = atm if regime == "negative_gamma" else (
        round(entry_u / step) * step if step else entry_u)
    entry_px = market.black_scholes(entry_u, strike, dte, snap["atm_iv"], kind)["price"]
    tp_dte = max(dte * 0.5, 0.5)                          # rough: target hit mid-horizon
    tp50_px = market.black_scholes(tp50_u, strike, tp_dte, snap["atm_iv"], kind)["price"]
    return {
        "action": "buy", "kind": kind, "strike": strike, "expiry": snap["expiry"],
        "dte_days": round(dte, 1),
        "entry_underlying": round(entry_u, 2), "entry_condition": condition,
        "entry_option_price_est": entry_px,
        "tp50_underlying": round(tp50_u, 2), "tp50_option_price_est": tp50_px,
        "tp50_rule": "sell HALF at the target, let the rest run, never past the stop",
        "stop_underlying": round(stop_u, 2),
        "conviction": conviction,
        "estimates_note": "option prices are Black-Scholes estimates at ATM vol; "
                          "theta/vol shift will move them",
    }


def _plain_english_exec(ticker: str, x: dict) -> str:
    """His format, four sentences, ~one number each — engine-authored."""
    return (
        f"Buy the {ticker} {x['strike']:g} {x['kind']}s expiring {x['expiry']} "
        f"({x['dte_days']:g} days), {x['entry_condition']} — about "
        f"{x['entry_option_price_est']:.2f} per contract with {ticker} at "
        f"{x['entry_underlying']:g}. "
        f"Sell HALF when {ticker} touches {x['tp50_underlying']:g} — those "
        f"{x['kind']}s should be worth roughly {x['tp50_option_price_est']:.2f}. "
        f"Let the rest run, but if {ticker} crosses {x['stop_underlying']:g} the trade "
        f"is over — out completely. "
        f"Risk about one percent of your account, no more."
    )


