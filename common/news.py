"""Market headlines for the desk — one shared source for every agent.

v1 uses Yahoo Finance's per-ticker RSS (free, no key, good-enough latency
for a demo desk). The functions are the seam where a paid feed (X API,
Benzinga, Polygon news) plugs in later without touching agent code.
Failure-safe by design: news being down must never break an agent.
"""

import time
import xml.etree.ElementTree as ET

import httpx

_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TTL_SECONDS = 300


def fetch_news(ticker: str, limit: int = 6) -> list[dict]:
    """Latest headlines for a ticker: [{title, published, source, link}]."""
    key = ticker.upper()
    now = time.time()
    if key in _CACHE and now - _CACHE[key][0] < _TTL_SECONDS:
        return _CACHE[key][1][:limit]
    try:
        resp = httpx.get(
            "https://feeds.finance.yahoo.com/rss/2.0/headline",
            params={"s": key, "region": "US", "lang": "en-US"},
            timeout=6,
            headers={"User-Agent": "ai-trading-desk/0.1"},
        )
        resp.raise_for_status()
        items = _parse_rss(resp.text)
    except Exception:
        items = []
    _CACHE[key] = (now, items)
    return items[:limit]


def _parse_rss(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        out.append({
            "title": (item.findtext("title") or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
        })
    return [i for i in out if i["title"]]


def headlines_block(ticker: str) -> str:
    """Compact text block for prompt injection; empty string when no news."""
    items = fetch_news(ticker)
    if not items:
        return ""
    lines = "\n".join(f"- {i['title']} ({i['published']})" for i in items)
    return f"Recent {ticker.upper()} headlines:\n{lines}"
