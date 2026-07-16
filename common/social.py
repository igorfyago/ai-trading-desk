"""Social buzz per ticker — what the retail crowd is talking about.

v1 uses ApeWisdom's free API (aggregated Reddit mentions/upvotes/rank across
r/wallstreetbets and friends). Direct message feeds (StockTwits, Reddit JSON)
are bot-walled in 2026 and we don't bypass those; the official X API is the
paid upgrade path and plugs in behind these same functions.
Failure-safe: social being down never breaks the dashboard.
"""

import time

import httpx

_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TTL_SECONDS = 300
_UA = {"User-Agent": "ai-trading-desk/0.1 (public dashboard)"}


def _all_stocks() -> list[dict]:
    now = time.time()
    if "all" in _CACHE and now - _CACHE["all"][0] < _TTL_SECONDS:
        return _CACHE["all"][1]
    try:
        resp = httpx.get("https://apewisdom.io/api/v1.0/filter/all-stocks/page/1",
                         timeout=8, headers=_UA)
        resp.raise_for_status()
        rows = resp.json().get("results", []) or []
    except Exception:
        rows = []
    _CACHE["all"] = (now, rows)
    return rows


def fetch_buzz(ticker: str) -> dict | None:
    """Mention stats for one ticker, or None if it isn't on the board."""
    for row in _all_stocks():
        if row.get("ticker", "").upper() == ticker.upper():
            rank, prev = row.get("rank"), row.get("rank_24h_ago")
            return {
                "ticker": ticker.upper(), "rank": rank,
                "mentions_24h": row.get("mentions"),
                "upvotes": row.get("upvotes"),
                "rank_change": (prev - rank) if (rank and prev) else 0,
                "name": row.get("name", ""),
            }
    return None


def fetch_trending(limit: int = 6) -> list[dict]:
    """The tickers the crowd is loudest about right now."""
    return [{"ticker": r.get("ticker"), "rank": r.get("rank"),
             "mentions_24h": r.get("mentions")}
            for r in _all_stocks()[:limit]]
