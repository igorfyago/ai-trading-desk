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
    the membership is mirrored into env.)"""
    raw = os.getenv("X_LIST_HANDLES", "")
    return [h.strip().lstrip("@") for h in raw.split(",") if h.strip()][:100]


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
        # xAI caps allowed_x_handles at 20 per request; the desk list is
        # bigger, so rotate a 20-account window each cache cycle — the whole
        # list gets read over successive refreshes at single-call cost.
        if len(handles) > 20:
            start = (int(time.time()) // _TTL_SECONDS) % len(handles)
            handles = (handles + handles)[start:start + 20]
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
                        "Plain text bullets starting with '- ', no preamble, no markdown headers. "
                        "After the bullets add one line 'POSTS:' followed by the x.com "
                        "URLs of the 3-6 most load-bearing posts you used, one per line."
                    ),
                }],
                "tools": [tool],
            },
            timeout=90,   # x_search over a 20-handle window can run long
        )
        resp.raise_for_status()
        data = resp.json()
        summary, inline_cites = _clean_inline_citations(_extract_text(data))
        summary, post_urls = _lift_posts_block(summary)
        citations = post_urls or _extract_citations(data) or inline_cites
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


def _lift_posts_block(text: str) -> tuple[str, list[str]]:
    """The prompt asks for a trailing 'POSTS:' block of x.com URLs — lift it
    out so the bullets stay clean and the URLs become embeddable citations."""
    m = re.search(r"\n?\s*POSTS:\s*\n?((?:\s*https?://\S+\s*\n?)+)", text, re.I)
    if not m:
        return text, []
    urls = re.findall(r"https?://\S+", m.group(1))
    return (text[:m.start()] + text[m.end():]).strip(), urls[:8]


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


# ------------------------------------------------------ real-post embeds ----

_OEMBED = "https://publish.twitter.com/oembed"
_embed_cache: dict[str, tuple[float, dict | None]] = {}


def embeds_for(ticker: str, limit: int = 6) -> list[dict]:
    """The pulse's cited posts as REAL X embeds (oEmbed — public, keyless,
    no login): the card renders the actual posts with media, not a summary.
    Rides the cached pulse, so this never adds a Grok call of its own."""
    p = pulse(ticker)
    urls = [u for u in (p or {}).get("citations") or []
            if "x.com" in u or "twitter.com" in u]
    out: list[dict] = []
    now = time.time()
    for u in urls[:limit]:
        hit = _embed_cache.get(u)
        if hit and now - hit[0] < 6 * 3600:
            if hit[1]:
                out.append(hit[1])
            continue
        item = None
        try:
            r = httpx.get(_OEMBED, params={
                "url": u, "theme": "dark", "omit_script": "true",
                "dnt": "true", "hide_thread": "true"},
                timeout=6, follow_redirects=True)
            if r.status_code == 200:
                d = r.json()
                if d.get("html"):
                    item = {"url": u, "html": d["html"],
                            "author": d.get("author_name")}
        except Exception:
            item = None
        _embed_cache[u] = (now, item)
        if item:
            out.append(item)
    return out
