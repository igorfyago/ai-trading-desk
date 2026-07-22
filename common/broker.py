"""Execution mirror: a confirmed desk trade becomes a REAL order in the
linked customer's broker account.

Same shape as the watchlist write-through in web/server.py, and for the same
reasons: a SERVER-SIDE hop (the browser never talks cross-origin to the
broker), and LOCAL-FIRST (the desk's own trade row is committed before this
module is even called, so nothing here can lose or block a desk trade).

What this module records is exactly what it observed, keyed by
`broker_status` on the trade row:

    filled/accepted/...  the broker's own answer · broker_order_id is set and
                         ONLY then may any surface say "in your broker account"
    rejected             the broker answered and refused · the reason travels
    unlinked             this session is bound to no customer · no call placed
    unreachable          the call failed in transport · NOT a claim about the
                         broker's book (a timed-out order may have landed; the
                         clientOrderId gate makes a later identical submit
                         return that same order rather than a duplicate)
    unmirrorable         the desk could not construct an honest order from
                         the row (bad expiry, bad root) · nothing was sent

There is no retry loop by design: a failed mirror is recorded, visible, and
final for that attempt. `mirror_entry` never raises.

Idempotency: the broker's orders API gates on clientOrderId (same request,
same answer — Broker.place returns the existing order). The desk uses the
trade's own id, "desk-<id>", so its own retries cannot double-place.
"""

import os
import re
from datetime import date

import httpx

_OCC_ROOT = re.compile(r"[A-Z.]{1,6}$")


def _url() -> str:
    return os.getenv("BROKER_URL", "http://minibank-broker:8091")


def _timeout() -> float:
    return float(os.getenv("BROKER_TIMEOUT", "2.0"))


# ------------------------------------------------------------- the symbol ----

def occ_symbol(root: str, expiry: str, kind: str, strike: float) -> str:
    """OCC contract symbol: root + yymmdd + C/P + strike*1000 in 8 digits.
    Raises ValueError with the reason when the row cannot express one —
    the caller records that reason rather than guessing."""
    root = (root or "").strip().upper()
    if not _OCC_ROOT.match(root):
        raise ValueError(f"'{root}' is not an OCC option root")
    try:
        d = date.fromisoformat(str(expiry)[:10])
    except (TypeError, ValueError):
        raise ValueError(f"unusable expiry '{expiry}'")
    if kind not in ("call", "put"):
        raise ValueError(f"unusable kind '{kind}'")
    thousandths = round(float(strike) * 1000)
    if thousandths <= 0 or thousandths > 99_999_999:
        raise ValueError(f"unusable strike {strike}")
    return f"{root}{d.strftime('%y%m%d')}{'C' if kind == 'call' else 'P'}{thousandths:08d}"


def contract_for(trade: dict) -> tuple[str, str]:
    """(occ_symbol, source) for a desk trade row.

    source is 'plan' when the row carries an execution plan of its own
    (contract_ticker differs from the analysis underlying — the XSP case), or
    'recommendation' when the row is underlying-only and the contract is
    built from the recommendation's underlying-terms strike. The caller must
    SAY SO in the record when it is the latter.
    """
    symbol = occ_symbol(trade["contract_ticker"], trade["expiry"],
                        trade["kind"], trade["strike"])
    source = ("plan" if (trade.get("contract_ticker") or "") != (trade.get("underlying") or "")
              else "recommendation")
    return symbol, source


# -------------------------------------------------------------- the mirror ----

def _outcome(status: str, reason: str | None = None, *, order_id: str | None = None,
             customer: int | None = None, contract: str | None = None,
             note_extra: str | None = None) -> dict:
    return {"broker_order_id": order_id, "broker_customer": customer,
            "broker_contract": contract, "broker_status": status,
            "broker_reason": reason, "note_extra": note_extra}


def mirror_entry(trade: dict) -> dict | None:
    """Place the entry order for a just-opened desk trade in the linked
    customer's broker account. Returns the outcome to record on the trade
    row, or None when the row already carries a broker order (nothing to do).
    Never raises, never blocks beyond BROKER_TIMEOUT per call.
    """
    if trade.get("broker_order_id"):
        return None                       # already mirrored · same answer

    session = trade.get("session")
    if not session:
        return _outcome("unlinked", "no session to link a broker account to")

    try:
        qty = int(trade["contracts_total"] or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty < 1:
        return _outcome("unmirrorable", "entry has no whole-contract quantity")

    customer = None
    try:
        with httpx.Client(timeout=_timeout()) as http:
            link = http.get(f"{_url()}/api/link", params={"session": session})
            if link.status_code != 200:
                return _outcome("unreachable", f"link {link.status_code}")
            customer = link.json().get("customer")
            if customer is None:
                return _outcome("unlinked", "session not linked to a broker account")

            try:
                symbol, source = contract_for(trade)
            except ValueError as e:
                return _outcome("unmirrorable", str(e), customer=customer)
            note_extra = (None if source == "plan" else
                          "broker contract built from the recommendation's"
                          " underlying-terms strike (no execution plan on the record)")

            r = http.post(f"{_url()}/api/orders", json={
                "clientOrderId": f"desk-{trade['id']}",
                "customer": int(customer),
                "symbol": symbol,
                "side": "buy",
                # a STRING on purpose: the broker's Json.str reads quoted
                # values only, and options qty is whole contracts end to end
                "qty": str(qty),
            })
            body = {}
            try:
                body = r.json()
            except ValueError:
                pass
            if r.status_code == 200 and body.get("result") not in (None, "rejected"):
                return _outcome(body["result"], order_id=body.get("id"),
                                customer=customer, contract=symbol,
                                note_extra=note_extra)
            if body.get("result") == "rejected" or r.status_code in (400, 409):
                return _outcome("rejected",
                                body.get("error") or f"broker {r.status_code}",
                                customer=customer, contract=symbol,
                                note_extra=note_extra)
            return _outcome("unreachable", f"orders {r.status_code}",
                            customer=customer, contract=symbol)
    except Exception as e:
        # connect refused, DNS miss, timeout, junk on the port — all the same
        # answer. The desk is not down because the bank is, and the trade is
        # already on the desk's own log.
        return _outcome("unreachable", type(e).__name__, customer=customer)
