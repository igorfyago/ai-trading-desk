"""Trade log lifecycle: quoted -> open -> trimmed -> closed, with the house
rules (sell half at +50%, no stop, runner rides) and P&L math verified."""

import pytest

from common import signals, trades
from common.db import get_connection


@pytest.fixture(autouse=True)
def clean_trades():
    """The trade log deliberately spans sessions (one desk, one book), so
    tests must wipe it to stay independent."""
    conn = get_connection()
    conn.execute("DELETE FROM trades")
    conn.commit()
    conn.close()


@pytest.fixture()
def captured_events(monkeypatch):
    events = []
    monkeypatch.setattr(trades.bus, "publish", events.append)
    return events


@pytest.fixture()
def quoted(captured_events):
    rec = signals.recommend_trade("SPY")
    assert "error" not in rec
    trade = trades.log_quote("sess-1", rec, source="marcus")
    assert trade is not None
    return trade


def test_log_quote_pins_engine_trade(quoted, captured_events):
    assert quoted["status"] == "quoted"
    assert quoted["underlying"] == "SPY"
    assert quoted["contract_ticker"] == "XSP"      # house rule: fills in XSP
    assert quoted["kind"] in ("call", "put")
    assert quoted["tp50_px"] == round(quoted["quoted_px"] * 1.5, 2)
    assert captured_events[-1]["event"] == "quoted"


def test_requoting_same_contract_updates_not_duplicates(quoted):
    rec = signals.recommend_trade("SPY")
    again = trades.log_quote("sess-2", rec, source="marcus")
    assert again["id"] == quoted["id"]


def test_full_lifecycle_pnl(quoted, captured_events):
    t = trades.confirm_entry("sess-1", fill_price=3.00, contracts=4)
    assert t["status"] == "open" and t["entry_px"] == 3.0
    assert t["contracts_open"] == 4

    t = trades.trim_half("sess-1", price=4.50)      # +50% on half the clip
    assert t["status"] == "trimmed"
    assert t["contracts_open"] == 2
    assert t["realized_usd"] == pytest.approx((4.50 - 3.00) * 100 * 2)

    t = trades.close_trade("sess-1", price=6.00)
    assert t["status"] == "closed" and t["contracts_open"] == 0
    assert t["realized_usd"] == pytest.approx(300 + (6.00 - 3.00) * 100 * 2)
    assert [e["event"] for e in captured_events] == ["quoted", "opened", "trimmed", "closed"]


def test_confirm_without_quote_is_a_clean_error(captured_events):
    out = trades.confirm_entry("sess-empty")
    assert "error" in out


def test_adjust_buttons_add_sell_close(quoted):
    t = trades.adjust(quoted["id"], "add", qty=2, price=2.00)   # first ADD = the entry
    assert t["status"] == "open" and t["contracts_open"] == 2 and t["entry_px"] == 2.0

    t = trades.adjust(t["id"], "add", qty=2, price=3.00)        # adds re-average
    assert t["contracts_open"] == 4
    assert t["entry_px"] == pytest.approx(2.50)

    t = trades.adjust(t["id"], "sell", qty=1, price=3.50)       # partial sell realizes
    assert t["status"] == "trimmed" and t["contracts_open"] == 3
    assert t["realized_usd"] == pytest.approx((3.50 - 2.50) * 100)

    t = trades.adjust(t["id"], "close", price=3.00)             # flatten the rest
    assert t["status"] == "closed" and t["contracts_open"] == 0
    assert t["realized_usd"] == pytest.approx(100 + (3.00 - 2.50) * 100 * 3)

    s = trades.score()
    assert s["closed_trades"] >= 1
    assert s["realized_usd"] == pytest.approx(t["realized_usd"])


def test_zero_mark_is_a_real_fill_price(quoted, monkeypatch):
    """Hold-to-zero is the house style: closing a worthless contract must
    realize the full loss, not silently fall back to the entry price."""
    trades.confirm_entry("sess-1", fill_price=2.30, contracts=1)
    monkeypatch.setattr(trades, "_model_mark", lambda t, **kw: 0.0)
    t = trades.close_trade("sess-1")
    assert t["close_px"] == 0.0
    assert t["realized_usd"] == pytest.approx(-230.0)


def test_adjust_guards(quoted):
    assert "error" in trades.adjust(99999, "add")               # unknown id
    assert "error" in trades.adjust(quoted["id"], "sell")       # nothing filled yet
    assert "error" in trades.adjust(quoted["id"], "burn")       # unknown action


def test_positions_snapshot_marks_against_snapshot_iv(quoted, monkeypatch):
    trades.confirm_entry("sess-1", fill_price=2.00, contracts=2)
    # live feed off in tests: inject a spot 1% above the entry level
    live = {"ticker": "SPY", "price": round(quoted["entry_underlying"] * 1.01, 2),
            "ts": None, "source": "test", "delayed": False}
    from common import quotes

    monkeypatch.setattr(quotes, "get_spot", lambda t: live)
    rows = trades.positions_snapshot()
    assert len(rows) == 1
    row = rows[0]
    assert row["mark"] is not None and row["unreal_usd"] is not None
    # a 1% move in the right direction must move an ATM option's mark
    direction = 1 if quoted["kind"] == "call" else -1
    assert (row["mark"] - 2.00) * direction != 0
