"""Trade log: the desk remembers what Marcus quoted and what the desk took.

Lifecycle (all transitions come from the CALLER's words, never the model's
initiative — HITL by design):

    quoted   engine authored a trade in a conversation (auto-pinned to chart)
    open     "I'm in / bought it"        -> confirm_entry
    trimmed  "sold half"                 -> trim_half  (house +50% rule)
    closed   "I'm out / flat"            -> close_trade

Every transition publishes a bus event so the dock chart updates live.
P&L marks are Black-Scholes estimates at the snapshot's ATM IV against the
LIVE spot — indicative, same convention as the engine's entry estimate.
"""

import json
import math
from datetime import datetime, timezone

from common import bus, market
from common.db import get_connection

_COLS = ("id", "created_at", "updated_at", "session", "source", "underlying",
         "contract_ticker", "kind", "strike", "strike_underlying", "expiry",
         "contracts_total", "contracts_open", "status", "quoted_px", "entry_px",
         "trim_px", "close_px", "entry_underlying", "tp50_px", "tp50_underlying",
         "thesis_reference", "iv_entry", "entry_at", "trim_at", "close_at",
         "realized_usd", "note")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return dict(zip(_COLS, row))


def _fetch(where: str = "1=1", params: tuple = (), limit: int = 20) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM trades WHERE {where}"
        f" ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def _emit(event: str, trade: dict) -> None:
    bus.publish({"type": "trade", "event": event, "trade": trade})


def _latest(session: str | None, statuses: tuple[str, ...]) -> dict | None:
    """Newest trade in the given statuses — this session first, then any
    (voice sessions on the landing page reload per call; the desk keeps one book)."""
    marks = ",".join("?" * len(statuses))
    if session:
        hit = _fetch(f"session = ? AND status IN ({marks})", (session, *statuses), 1)
        if hit:
            return hit[0]
    hit = _fetch(f"status IN ({marks})", statuses, 1)
    return hit[0] if hit else None


# ------------------------------------------------------------- lifecycle ----

def log_quote(session: str, rec: dict, source: str = "marcus") -> dict | None:
    """Pin an engine-authored trade the first time it comes up in a convo.
    Re-quoting the same contract refreshes the pin instead of duplicating."""
    ex, cp = rec.get("execution"), rec.get("contract_plan") or {}
    if not ex:
        return None
    cp = cp or ex.get("contract_plan") or {}
    underlying = "SPY" if rec.get("ticker") in ("SPY", "XSP") else rec["ticker"]
    contract_ticker = cp.get("contract_ticker", underlying)
    strike = cp.get("contract_strike", ex["strike"])
    now = _now()

    conn = get_connection()
    dup = conn.execute(
        "SELECT id FROM trades WHERE status = 'quoted' AND contract_ticker = ?"
        " AND strike = ? AND kind = ? AND expiry = ? ORDER BY id DESC LIMIT 1",
        (contract_ticker, strike, ex["kind"], ex["expiry"])).fetchone()
    if dup:
        conn.execute("UPDATE trades SET updated_at = ?, quoted_px = ?, session = ?,"
                     " entry_underlying = ?, tp50_px = ?, tp50_underlying = ? WHERE id = ?",
                     (now, ex["entry_option_price_est"], session, ex["entry_underlying"],
                      ex["tp50_option_price"], ex["tp50_underlying_est"], dup[0]))
        conn.commit()
        trade = _fetch("id = ?", (dup[0],), 1)[0]
        conn.close()
        _emit("quoted", trade)
        return trade

    cur = conn.execute(
        "INSERT INTO trades (created_at, updated_at, session, source, underlying,"
        " contract_ticker, kind, strike, strike_underlying, expiry, contracts_total,"
        " contracts_open, status, quoted_px, entry_underlying, tp50_px,"
        " tp50_underlying, thesis_reference, iv_entry, payload)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, now, session, source, underlying, contract_ticker, ex["kind"],
         strike, ex["strike"], ex["expiry"], cp.get("contracts_now", 1),
         cp.get("contracts_now", 1), "quoted", ex["entry_option_price_est"],
         ex["entry_underlying"], ex["tp50_option_price"], ex["tp50_underlying_est"],
         ex.get("thesis_reference"), rec.get("snapshot_iv") or None,
         json.dumps(rec, default=str)[:20000]))
    conn.commit()
    trade = _fetch("id = ?", (cur.lastrowid,), 1)[0]
    conn.close()
    _emit("quoted", trade)
    return trade


def confirm_entry(session: str, fill_price: float | None = None,
                  contracts: int | None = None) -> dict:
    trade = _latest(session, ("quoted",))
    if trade is None:
        return {"error": "no quoted trade to confirm — ask for the trade first"}
    now = _now()
    entry = round(float(fill_price), 2) if fill_price else trade["quoted_px"]
    total = int(contracts) if contracts else trade["contracts_total"]
    conn = get_connection()
    conn.execute("UPDATE trades SET status='open', entry_px=?, entry_at=?,"
                 " updated_at=?, contracts_total=?, contracts_open=? WHERE id=?",
                 (entry, now, now, total, total, trade["id"]))
    conn.commit()
    conn.close()
    trade = _fetch("id = ?", (trade["id"],), 1)[0]
    _emit("opened", trade)
    return trade


def trim_half(session: str, price: float | None = None) -> dict:
    trade = _latest(session, ("open",))
    if trade is None:
        return {"error": "no open position to trim"}
    now = _now()
    px = round(float(price), 2) if price else (_model_mark(trade) or trade["tp50_px"])
    sold = max(trade["contracts_open"] // 2, 1)
    remaining = trade["contracts_open"] - sold
    realized = trade["realized_usd"] + (px - trade["entry_px"]) * 100 * sold
    status = "trimmed" if remaining else "closed"
    conn = get_connection()
    conn.execute("UPDATE trades SET status=?, trim_px=?, trim_at=?, updated_at=?,"
                 " contracts_open=?, realized_usd=? WHERE id=?",
                 (status, px, now, now, remaining, round(realized, 2), trade["id"]))
    conn.commit()
    conn.close()
    trade = _fetch("id = ?", (trade["id"],), 1)[0]
    _emit("trimmed", trade)
    return trade


def close_trade(session: str, price: float | None = None) -> dict:
    trade = _latest(session, ("open", "trimmed"))
    if trade is None:
        return {"error": "no position to close"}
    now = _now()
    px = round(float(price), 2) if price else (_model_mark(trade) or trade["entry_px"])
    realized = trade["realized_usd"] + (px - trade["entry_px"]) * 100 * trade["contracts_open"]
    conn = get_connection()
    conn.execute("UPDATE trades SET status='closed', close_px=?, close_at=?,"
                 " updated_at=?, contracts_open=0, realized_usd=? WHERE id=?",
                 (px, now, now, round(realized, 2), trade["id"]))
    conn.commit()
    conn.close()
    trade = _fetch("id = ?", (trade["id"],), 1)[0]
    _emit("closed", trade)
    return trade


# ------------------------------------------------------------------ marks ----

def _model_mark(trade: dict, spot: float | None = None,
                iv: float | None = None) -> float | None:
    """BS reprice of the contract at the live spot — the engine's convention:
    underlying-level strike against the underlying spot, ATM IV."""
    if spot is None:
        from common import quotes

        q = quotes.get_spot(trade["underlying"])
        spot = q["price"] if q else None
    if spot is None or not trade.get("strike_underlying"):
        return None
    if iv is None:
        snap = market.latest_snapshot(trade["underlying"])
        iv = (snap or {}).get("atm_iv") or trade.get("iv_entry") or 0.15
    dte = market.days_to(trade["expiry"])
    px = market.black_scholes(spot, trade["strike_underlying"], dte, iv, trade["kind"])
    return px["price"]


def positions_snapshot() -> list[dict]:
    """Open/trimmed trades marked to the live feed — powers the P&L badge."""
    out = []
    for trade in _fetch("status IN ('open','trimmed')", (), 10):
        from common import quotes

        q = quotes.get_spot(trade["underlying"])
        spot = q["price"] if q else None
        mark = _model_mark(trade, spot=spot)
        row = {**trade, "spot": spot, "mark": mark,
               "spot_source": (q or {}).get("source"),
               "unreal_usd": None, "unreal_pct": None, "tp_hit": False}
        if mark is not None and trade["entry_px"]:
            row["unreal_usd"] = round((mark - trade["entry_px"]) * 100 * trade["contracts_open"], 2)
            row["unreal_pct"] = round((mark / trade["entry_px"] - 1) * 100, 1)
            row["tp_hit"] = trade["status"] == "open" and mark >= (trade["tp50_px"] or math.inf)
        out.append(row)
    return out


def recent_trades(limit: int = 12) -> list[dict]:
    return _fetch(limit=limit)
