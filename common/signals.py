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

from common import market
from common.db import get_connection

DISCLAIMER = ("Demo data + rule-based engine. Educational example only — "
              "not financial advice, not an offer to trade.")


def _latest_walls(ticker: str) -> dict:
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

    return {
        "ticker": snap["ticker"], "as_of": snap["captured_at"], "spot": spot,
        "regime": snap["regime"], "gamma_flip": flip, "signal_score": score,
        "structure": name, "bias": bias, "legs": legs,
        "one_sigma_move": em, "expected_move_band": [round(spot - em, 2), round(spot + em, 2)],
        "rationale": rationale,
        "plain_english": _plain_english(snap["ticker"], name, legs, spot, flip,
                                        put_wall, call_wall, snap["regime"]),
        "invalidation": f"Exit if {invalidation}.",
        "sizing": "Risk no more than 1% of account on the structure's max loss.",
        "disclaimer": DISCLAIMER,
    }


def _plain_english(ticker: str, structure: str, legs: list, spot: float, flip: float,
                   put_wall: float, call_wall: float, regime: str) -> str:
    """The no-jargon version, authored by the engine so it's always faithful.

    Rules: cause-and-effect words, at most one number per sentence, four
    sentences total. This is the default narration; jargon is opt-in.
    """
    def leg_words(leg):
        return f"{'buy' if leg['side'] == 'buy' else 'sell'} the {leg['strike']:g} {leg['kind']}s"

    trade = ", ".join(leg_words(l) for l in legs) + f" expiring {legs[0]['expiry']}"

    if regime == "negative_gamma":
        if spot < flip:
            why = (f"{ticker} is in a fragile spot: as it falls, big market players are "
                   f"forced to sell even more, so drops tend to snowball.")
            wrong = (f"If it climbs back above {flip:g}, that snowball effect switches "
                     f"off — get out.")
        else:
            why = (f"{ticker} is in breakout mode: as it rises, big market players are "
                   f"forced to buy even more, so rallies tend to feed themselves.")
            wrong = f"If it slips back under {flip:g}, that fuel is gone — get out."
    else:
        why = (f"{ticker} is in a calm stretch: big market players make money keeping it "
               f"inside roughly {put_wall:g} to {call_wall:g}, so it tends to stay there.")
        wrong = "If it closes outside that range, the calm is over — get out."

    return f"{why} The trade: {trade}. {wrong} Risk about one percent of your account, no more."
