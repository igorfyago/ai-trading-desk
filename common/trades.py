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
    (voice sessions on the landing page reload per call; the desk runs one
    shared book)."""
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


def _fill_px(price, trade: dict, *fallback_cols: str) -> float | None:
    """Order-ticket fill price: caller's price, else the live model mark, else
    stored fallbacks. A 0.00 mark is a REAL price (options expire worthless —
    the house holds to zero), so only None means 'missing'."""
    if price is not None:
        return round(float(price), 2)
    mark = _model_mark(trade)
    if mark is not None:
        return mark
    for col in fallback_cols:
        if trade.get(col) is not None:
            return trade[col]
    return None


def confirm_entry(session: str, fill_price: float | None = None,
                  contracts: int | None = None) -> dict:
    trade = _latest(session, ("quoted",))
    if trade is None:
        return {"error": "no quoted trade to confirm, ask for the trade first"}
    now = _now()
    entry = round(float(fill_price), 2) if fill_price is not None else trade["quoted_px"]
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
    px = _fill_px(price, trade, "tp50_px")
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
    px = _fill_px(price, trade, "entry_px")
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


def book_line() -> str:
    """The book in one line — appended to trading tool results so the agent's
    picture refreshes on EVERY tool touch (positions move from the chart's
    ADD/SELL/CLOSE buttons at any moment, not just from the conversation)."""
    rows = positions_snapshot()
    s = score()
    if not rows:
        return f"flat; net P&L {s['score']:+.0f} USD"
    pos = "; ".join(
        f"{r['contract_ticker']} {r['strike']:g}{r['kind'][0]} x{r['contracts_open']}"
        f" @ {r['entry_px']}"
        + (f" ({r['unreal_usd']:+.0f} USD)" if r["unreal_usd"] is not None else "")
        + (" TP-ZONE" if r["tp_hit"] else "")
        for r in rows)
    return f"{pos} | net P&L {s['score']:+.0f} USD"


def book_block() -> str:
    """Multi-line book context for prompts (agent context injection, voice
    session mint). Empty string when there is nothing worth saying."""
    rows = positions_snapshot()
    s = score()
    if not rows and not s["closed_trades"]:
        return ""
    if not rows:
        return (f"BOOK: flat. Realized P&L {s['realized_usd']:+.0f} USD across "
                f"{s['closed_trades']} closed trades.")
    lines = []
    for r in rows:
        mark = f"mark {r['mark']:.2f}" if r["mark"] is not None else "mark n/a"
        pnl = (f", {r['unreal_usd']:+.0f} USD ({r['unreal_pct']:+.1f}%)"
               if r["unreal_usd"] is not None else "")
        lines.append(
            f"- {r['contract_ticker']} {r['strike']:g}{r['kind'][0]} x{r['contracts_open']}"
            f" @ {r['entry_px']} ({r['status']}), {mark}{pnl}"
            + (" · TP ZONE (+50% trim level hit)" if r["tp_hit"] else ""))
    return (f"OPEN BOOK ({len(rows)} position{'s' if len(rows) != 1 else ''}, "
            f"net P&L {s['score']:+.0f} USD, realized {s['realized_usd']:+.0f}):\n"
            + "\n".join(lines))


# ------------------------------------------------------------- the game ----

def adjust(trade_id: int, action: str, qty: int | None = None,
           price: float | None = None) -> dict:
    """Position buttons (ADD / SELL / CLOSE) — the sim's order ticket.
    price defaults to the live model mark; adds re-average the entry."""
    rows = _fetch("id = ?", (trade_id,), 1)
    if not rows:
        return {"error": f"no trade #{trade_id}"}
    t = rows[0]
    if t["status"] not in ("quoted", "open", "trimmed"):
        return {"error": f"trade #{trade_id} is {t['status']}"}
    px = _fill_px(price, t, "quoted_px", "entry_px")
    if px is None:
        return {"error": "no price available to fill at"}
    qty = max(int(qty or 1), 1)
    now = _now()
    conn = get_connection()

    if action == "add":
        if t["status"] == "quoted":   # first ADD = the entry
            conn.execute("UPDATE trades SET status='open', entry_px=?, entry_at=?,"
                         " updated_at=?, contracts_total=?, contracts_open=? WHERE id=?",
                         (px, now, now, qty, qty, t["id"]))
            event = "opened"
        else:
            open_n, total_n = t["contracts_open"] + qty, t["contracts_total"] + qty
            avg = round((t["entry_px"] * t["contracts_open"] + px * qty) / open_n, 2)
            conn.execute("UPDATE trades SET entry_px=?, updated_at=?,"
                         " contracts_total=?, contracts_open=? WHERE id=?",
                         (avg, now, total_n, open_n, t["id"]))
            event = "added"
    elif action == "sell":
        if t["status"] == "quoted":
            conn.close()
            return {"error": "nothing filled yet, ADD first"}
        sold = min(qty, t["contracts_open"])
        remaining = t["contracts_open"] - sold
        realized = t["realized_usd"] + (px - t["entry_px"]) * 100 * sold
        status = "closed" if remaining == 0 else "trimmed"
        conn.execute("UPDATE trades SET status=?, trim_px=?, trim_at=?, updated_at=?,"
                     " contracts_open=?, realized_usd=?" +
                     (", close_px=?, close_at=?" if remaining == 0 else "") +
                     " WHERE id=?",
                     (status, px, now, now, remaining, round(realized, 2),
                      *((px, now) if remaining == 0 else ()), t["id"]))
        event = "closed" if remaining == 0 else "trimmed"
    elif action == "close":
        if t["status"] == "quoted":
            conn.execute("UPDATE trades SET status='closed', close_at=?, updated_at=?,"
                         " contracts_open=0 WHERE id=?", (now, now, t["id"]))
            event = "closed"
        else:
            realized = t["realized_usd"] + (px - t["entry_px"]) * 100 * t["contracts_open"]
            conn.execute("UPDATE trades SET status='closed', close_px=?, close_at=?,"
                         " updated_at=?, contracts_open=0, realized_usd=? WHERE id=?",
                         (px, now, now, round(realized, 2), t["id"]))
            event = "closed"
    else:
        conn.close()
        return {"error": f"unknown action '{action}'"}

    conn.commit()
    conn.close()
    trade = _fetch("id = ?", (t["id"],), 1)[0]
    _emit(event, trade)
    return trade


def score() -> dict:
    """The scoreboard: realized + live-marked unrealized, across the book."""
    realized = sum(t["realized_usd"] or 0 for t in _fetch("status = 'closed'", (), 200))
    positions = positions_snapshot()
    unreal = sum(p["unreal_usd"] or 0 for p in positions)
    realized += sum(t["realized_usd"] or 0 for t in positions)   # banked trims
    closed = _fetch("status = 'closed' AND close_px IS NOT NULL", (), 200)
    wins = sum(1 for t in closed if (t["realized_usd"] or 0) > 0)
    return {
        "score": round(realized + unreal, 2),
        "realized_usd": round(realized, 2),
        "unrealized_usd": round(unreal, 2),
        "open_positions": len(positions),
        "closed_trades": len(closed),
        "win_rate": round(wins / len(closed) * 100) if closed else None,
    }
