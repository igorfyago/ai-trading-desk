"""X pulse — what traders on X are saying, via the official xAI (Grok) API.

Grok's server-side x_search tool reads live X posts and returns a grounded
summary with citations. This is the legitimate version of "the agents can
see X": the desk's own API key, no user login, no scraping.

Requires XAI_API_KEY. Failure-safe and cache-heavy: the pulse being down
never breaks anything, and repeated dashboard loads don't re-bill Grok.
"""

import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

_CACHE: dict[str, tuple[float, dict | None]] = {}
_TTL_SECONDS = 240

# Public-endpoint billing guard: at most N uncached Grok calls per hour,
# whole process. Cached reads are unlimited.
_MAX_LIVE_CALLS_PER_HOUR = 30
_window: list[float] = []


def _budget_ok() -> bool:
    now = time.time()
    _window[:] = [t for t in _window if now - t < 3600]
    if len(_window) >= _MAX_LIVE_CALLS_PER_HOUR:
        return False
    _window.append(now)
    return True


def available() -> bool:
    return bool(os.getenv("XAI_API_KEY"))


def list_handles() -> list[str]:
    """The desk's curated X list (X_LIST_HANDLES=comma,separated,handles).
    When set, the pulse reads ONLY those accounts — the signal list, not the
    whole firehose. (The X API can't enumerate a list URL without auth, so
    the membership is mirrored into env by hand.)"""
    raw = os.getenv("X_LIST_HANDLES", "")
    return [h.strip().lstrip("@") for h in raw.split(",") if h.strip()][:50]


def pulse(ticker: str) -> dict | None:
    """{summary, citations:[urls]} for the last ~24h of X chatter, or None."""
    if not available():
        return None
    key = ticker.upper()
    now = time.time()
    if key in _CACHE and now - _CACHE[key][0] < _TTL_SECONDS:
        return _CACHE[key][1]
    if not _budget_ok():
        return _CACHE.get(key, (0, None))[1]

    since = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    handles = list_handles()
    tool: dict = {"type": "x_search", "from_date": since}
    scope = "traders on X"
    if handles:
        tool["allowed_x_handles"] = handles
        scope = "the desk's watched accounts"
    try:
        resp = httpx.post(
            "https://api.x.ai/v1/responses",
            headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}"},
            json={
                "model": "grok-4.5",
                "input": [{
                    "role": "user",
                    "content": (
                        f"What are {scope} saying about ${key} in the last 24 hours? "
                        "Answer with 3-5 terse bullets: concrete catalysts, overall mood, "
                        "and any widely-repeated claims (mark rumors as rumors). "
                        "Quote levels and prices as plain digits. "
                        "Plain text bullets starting with '- ', no preamble, no markdown headers."
                    ),
                }],
                "tools": [tool],
            },
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json()
        summary, inline_cites = _clean_inline_citations(_extract_text(data))
        citations = _extract_citations(data) or inline_cites
        result = {"summary": summary, "citations": citations}
        if not result["summary"]:
            result = None
    except Exception:
        result = None
    _CACHE[key] = (now, result)
    return result


def _extract_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    parts = []
    for item in data.get("output", []) or []:
        for chunk in item.get("content", []) or []:
            if isinstance(chunk, dict) and chunk.get("type") in ("output_text", "text"):
                parts.append(chunk.get("text", ""))
    return "\n".join(p for p in parts if p).strip()


def _clean_inline_citations(text: str) -> tuple[str, list[str]]:
    """Grok often embeds citations as markdown links; lift them out so the
    summary reads clean and the links render as [1] [2] chips."""
    urls: list[str] = []

    def repl(m):
        url = m.group(1)
        if url not in urls:
            urls.append(url)
        return f" [{urls.index(url) + 1}]"

    cleaned = re.sub(r"\[+[^\]]*\]+\((https?://[^)]+)\)", repl, text)
    return cleaned.strip(), urls[:8]


def _extract_citations(data: dict) -> list[str]:
    cites = data.get("citations") or []
    if isinstance(cites, list):
        return [c if isinstance(c, str) else c.get("url", "") for c in cites][:8]
    return []


def pulse_block(ticker: str) -> str:
    """Prompt/tool-friendly text block; empty string when unavailable."""
    p = pulse(ticker)
    if not p:
        return ""
    return f"X chatter on {ticker.upper()} (via Grok x_search):\n{p['summary']}"
