"""Live spot + candles: the ONE market feed every desk surface drinks from.

House rule: all financial agents, the dashboard chart, the GEX tiles and the
trade dock read the SAME source. This module is that source.

Provider chain (first healthy wins):
  alpaca — real-time IEX, free API key (ALPACA_KEY_ID / ALPACA_SECRET_KEY).
           The "live" the site advertises. Free tier: 200 req/min; the watch
           loop batches all symbols into ONE request per tick.
  yahoo  — unofficial, keyless, near-real-time. Dev + fallback. Labeled.
  cboe   — official free CDN, 15-min delayed. Last resort. Labeled delayed.
The collector's DB snapshot (common/market.py) stays the final fallback for
agents when this module returns None.

Sync REST + tiny TTL caches so agents can call get_spot() from any context;
the web server runs watch_loop() to push ticks onto the bus for SSE clients.
QUOTES_PROVIDER=off kills all network (tests / CI).
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

_UA = {"User-Agent": "Mozilla/5.0 (desk.b4rruf3t.com market panel)"}
_client = httpx.Client(timeout=6, headers=_UA, follow_redirects=True)
_lock = threading.Lock()

# label -> (alpaca timeframe, yahoo interval, yahoo range, resample factor, seconds)
INTERVALS = {
    "1m":  ("1Min", "1m", "5d", None, 60),
    "3m":  ("3Min", "1m", "5d", 3, 180),
    "5m":  ("5Min", "5m", "30d", None, 300),
    "15m": ("15Min", "15m", "60d", None, 900),
    "45m": ("45Min", "15m", "60d", 3, 2700),
    "4h":  ("4Hour", "60m", "2y", 4, 14400),
    "D":   ("1Day", "1d", "2y", None, 86400),
    "W":   ("1Week", "1wk", "5y", None, 604800),
}
# old TradingView-widget values still live in visitors' localStorage
_TV_ALIASES = {"1": "1m", "3": "3m", "5": "5m", "15": "15m", "45": "45m",
               "240": "4h", "1D": "D", "1W": "W"}

_ALPACA_DATA = "https://data.alpaca.markets/v2/stocks"
_YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"
_CBOE = "https://cdn.cboe.com/api/global/delayed_quotes/quotes"

_spot_cache: dict[str, tuple[float, dict]] = {}   # sym -> (monotonic, quote)
_bars_cache: dict[tuple, tuple[float, dict]] = {}  # (sym, interval) -> (monotonic, payload)
_SPOT_TTL = {"alpaca": 2.0, "yahoo": 15.0, "cboe": 60.0}


def normalize_interval(label: str) -> str | None:
    label = (label or "").strip()
    if label in INTERVALS:
        return label
    return _TV_ALIASES.get(label)


def _alpaca_keys() -> tuple[str, str] | None:
    k, s = os.getenv("ALPACA_KEY_ID"), os.getenv("ALPACA_SECRET_KEY")
    return (k, s) if k and s else None


def provider_order() -> list[str]:
    mode = (os.getenv("QUOTES_PROVIDER") or "auto").lower()
    if mode == "off":
        return []
    if mode in ("alpaca", "yahoo", "cboe"):
        return [mode]
    order = []
    if _alpaca_keys():
        order.append("alpaca")
    return order + ["yahoo", "cboe"]


# ------------------------------------------------------------------ spot ----

def _spots_alpaca(symbols: list[str]) -> dict[str, dict]:
    keys = _alpaca_keys()
    if not keys:
        return {}
    r = _client.get(f"{_ALPACA_DATA}/trades/latest",
                    params={"symbols": ",".join(symbols), "feed": "iex"},
                    headers={"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]})
    r.raise_for_status()
    out = {}
    for sym, t in (r.json().get("trades") or {}).items():
        out[sym] = {"ticker": sym, "price": round(float(t["p"]), 2), "ts": t["t"],
                    "source": "alpaca·iex", "delayed": False}
    return out


def _spot_yahoo(symbol: str) -> dict | None:
    r = _client.get(f"{_YAHOO}/{symbol}", params={"interval": "1m", "range": "1d"})
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    px = meta.get("regularMarketPrice")
    if px is None:
        return None
    ts = meta.get("regularMarketTime")
    return {"ticker": symbol, "price": round(float(px), 2),
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None,
            "source": "yahoo", "delayed": False}


def _spot_cboe(symbol: str) -> dict | None:
    r = _client.get(f"{_CBOE}/{symbol}.json")
    r.raise_for_status()
    d = r.json().get("data") or {}
    px = d.get("current_price") or d.get("last_trade_price") or d.get("close")
    if not px:
        return None
    return {"ticker": symbol, "price": round(float(px), 2),
            "ts": d.get("last_trade_time"), "source": "cboe·15m", "delayed": True}


def fetch_spots(symbols: list[str]) -> dict[str, dict]:
    """Batched fetch down the provider chain; partial results are fine."""
    out: dict[str, dict] = {}
    for prov in provider_order():
        missing = [s for s in symbols if s not in out]
        if not missing:
            break
        try:
            if prov == "alpaca":
                out.update(_spots_alpaca(missing))
            elif prov == "yahoo":
                for s in missing:
                    q = _spot_yahoo(s)
                    if q:
                        out[s] = q
            elif prov == "cboe":
                for s in missing:
                    q = _spot_cboe(s)
                    if q:
                        out[s] = q
        except Exception:
            continue  # next provider
    now = time.monotonic()
    with _lock:
        for s, q in out.items():
            _spot_cache[s] = (now, q)
    return out


def get_spot(ticker: str) -> dict | None:
    """Freshest spot within the source's TTL, else refetch. None = no feed."""
    sym = ticker.upper()
    now = time.monotonic()
    with _lock:
        hit = _spot_cache.get(sym)
    if hit and now - hit[0] < _SPOT_TTL.get(hit[1]["source"].split("·")[0], 15.0):
        return hit[1]
    return fetch_spots([sym]).get(sym)


# ------------------------------------------------------------------ bars ----

def _resample(bars: list[dict], factor: int, step_s: int) -> list[dict]:
    """Aggregate consecutive bars into buckets of `factor` (yahoo lacks 3m/45m/4h)."""
    out: list[dict] = []
    for b in bars:
        bucket = b["t"] - (b["t"] % step_s)
        if out and out[-1]["t"] == bucket:
            last = out[-1]
            last["h"] = max(last["h"], b["h"])
            last["l"] = min(last["l"], b["l"])
            last["c"] = b["c"]
            last["v"] += b["v"]
        else:
            out.append({"t": bucket, "o": b["o"], "h": b["h"], "l": b["l"],
                        "c": b["c"], "v": b["v"]})
    return out


def _bars_alpaca(symbol: str, interval: str, limit: int) -> list[dict] | None:
    keys = _alpaca_keys()
    if not keys:
        return None
    tf, _, yrange, _, step = INTERVALS[interval]
    span_days = {"1m": 5, "3m": 5, "5m": 30, "15m": 60, "45m": 120,
                 "4h": 365, "D": 730, "W": 1825}[interval]
    start = (datetime.now(timezone.utc) - timedelta(days=span_days)).isoformat()
    r = _client.get(f"{_ALPACA_DATA}/bars",
                    params={"symbols": symbol, "timeframe": tf, "start": start,
                            "limit": min(limit, 1000), "adjustment": "split",
                            "feed": "iex", "sort": "desc"},
                    headers={"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]})
    r.raise_for_status()
    raw = (r.json().get("bars") or {}).get(symbol) or []
    bars = [{"t": int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()),
             "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v", 0)}
            for b in raw]
    bars.reverse()  # requested desc for recency; charts want ascending
    return bars


def _bars_yahoo(symbol: str, interval: str, limit: int) -> list[dict] | None:
    _, yiv, yrange, factor, step = INTERVALS[interval]
    r = _client.get(f"{_YAHOO}/{symbol}",
                    params={"interval": yiv, "range": yrange, "includePrePost": "false"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    bars = [{"t": int(t), "o": q["open"][i], "h": q["high"][i],
             "l": q["low"][i], "c": q["close"][i], "v": q["volume"][i] or 0}
            for i, t in enumerate(ts)
            if q["open"][i] is not None and q["close"][i] is not None]
    if factor:
        bars = _resample(bars, factor, step)
    return bars


def get_bars(ticker: str, interval: str, limit: int = 600) -> dict | None:
    """Candles for the chart: {source, delayed, bars:[{t,o,h,l,c,v}]}."""
    norm = normalize_interval(interval)
    if norm is None:
        return None
    sym = ticker.upper()
    key = (sym, norm)
    now = time.monotonic()
    with _lock:
        hit = _bars_cache.get(key)
    if hit and now - hit[0] < max(20.0, INTERVALS[norm][4] / 4):
        return hit[1]
    for prov in provider_order():
        try:
            if prov == "alpaca":
                bars = _bars_alpaca(sym, norm, limit)
                src, delayed = "alpaca·iex", False
            elif prov == "yahoo":
                bars = _bars_yahoo(sym, norm, limit)
                src, delayed = "yahoo", False
            else:
                continue  # cboe has no candle history
            if bars:
                payload = {"ticker": sym, "interval": norm, "source": src,
                           "delayed": delayed, "bars": bars[-limit:]}
                with _lock:
                    _bars_cache[key] = (now, payload)
                return payload
        except Exception:
            continue
    return None


# ------------------------------------------------------------ watch loop ----

def watch_symbols() -> list[str]:
    raw = os.getenv("WATCH_SYMBOLS", "SPY,QQQ,IWM")
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def status() -> dict:
    order = provider_order()
    with _lock:
        last = {s: q for s, (_, q) in _spot_cache.items()}
    return {"providers": order, "live": bool(order) and order[0] != "cboe",
            "alpaca_configured": _alpaca_keys() is not None, "last": last}


async def watch_loop(symbols: list[str] | None = None) -> None:
    """Server-side: poll the feed and publish changed ticks onto the bus.
    Alpaca free tier allows 200 req/min; one batched call/sec uses 60."""
    import asyncio

    from common import bus

    symbols = symbols or watch_symbols()
    last_px: dict[str, float] = {}
    while True:
        order = provider_order()
        if not order:
            return  # QUOTES_PROVIDER=off
        cadence = 1.0 if order[0] == "alpaca" else (15.0 if order[0] == "yahoo" else 45.0)
        try:
            spots = await asyncio.to_thread(fetch_spots, symbols)
            for sym, q in spots.items():
                if last_px.get(sym) != q["price"]:
                    last_px[sym] = q["price"]
                    bus.publish({"type": "quote", **q})
        except Exception:
            pass  # provider hiccup: keep the loop alive
        await asyncio.sleep(cadence)
