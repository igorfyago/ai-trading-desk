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

# 24h-freshness contract: the desk always shows the LATEST print — close,
# post-market, overnight, premarket. A print older than this makes us hunt
# the other tapes (delayed SIP, Blue Ocean overnight, yahoo pre/post).
_FRESH_S = 120.0


def _ts_utc(iso) -> datetime | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _age_s(iso) -> float:
    ts = _ts_utc(iso)
    return (datetime.now(timezone.utc) - ts).total_seconds() if ts else 1e9


def _et_wall(ts: datetime) -> datetime:
    """US-Eastern wall clock without tzdata (US DST: 2nd Sun Mar -> 1st Sun Nov)."""
    def nth_sunday(month: int, n: int) -> datetime:
        first = datetime(ts.year, month, 1, tzinfo=timezone.utc)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))

    dst_start = nth_sunday(3, 2).replace(hour=7)    # 02:00 EST == 07:00 UTC
    dst_end = nth_sunday(11, 1).replace(hour=6)     # 02:00 EDT == 06:00 UTC
    offset = -4 if dst_start <= ts < dst_end else -5
    return ts + timedelta(hours=offset)


def session_label(iso) -> str | None:
    """Which tape printed this: rth / pre / post / overnight (ET clock)."""
    ts = _ts_utc(iso)
    if ts is None:
        return None
    et = _et_wall(ts)
    if et.weekday() >= 5:
        return "overnight"                     # weekend prints = the 24h tape
    hm = et.hour * 60 + et.minute
    if 4 * 60 <= hm < 9 * 60 + 30:
        return "pre"
    if 9 * 60 + 30 <= hm < 16 * 60:
        return "rth"
    if 16 * 60 <= hm < 20 * 60:
        return "post"
    return "overnight"


# ------------------------------------------------------- symbol dialects ----
# The watchlist speaks TradingView ("NASDAQ:TSLA", "ES1!", "VIX"); providers
# each have their own dialect. Canonical form = bare uppercase ("TSLA").
_SPECIAL = {          # canonical -> (alpaca symbol or None, yahoo symbol)
    "BTCUSD": (None, "BTC-USD"), "ETHUSD": (None, "ETH-USD"),
    "ES1!": (None, "ES=F"), "NQ1!": (None, "NQ=F"),
    "VIX": (None, "^VIX"), "US10Y": (None, "^TNX"), "IBEX35": (None, "^IBEX"),
}


def clean_symbol(sym: str) -> str:
    """'NASDAQ:TSLA' -> 'TSLA'; keeps specials like 'ES1!' intact."""
    return (sym or "").split(":")[-1].strip().upper()


def _alpaca_sym(sym: str) -> str | None:
    return _SPECIAL[sym][0] if sym in _SPECIAL else sym


def _yahoo_sym(sym: str) -> str:
    return _SPECIAL[sym][1] if sym in _SPECIAL else sym.replace(".", "-")


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

def _alpaca_feed(symbols: list[str], feed: str, label: str, delayed: bool,
                 keys: tuple[str, str]) -> dict[str, dict]:
    req = {m: s for s in symbols if (m := _alpaca_sym(s))}   # crypto/futures skip
    if not req:
        return {}
    r = _client.get(f"{_ALPACA_DATA}/trades/latest",
                    params={"symbols": ",".join(req), "feed": feed},
                    headers={"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]})
    if r.status_code != 200:     # a tape the plan doesn't carry: not an error
        return {}
    out = {}
    for sym, t in (r.json().get("trades") or {}).items():
        canon = req.get(sym, sym)
        out[canon] = {"ticker": canon, "price": round(float(t["p"]), 2), "ts": t["t"],
                      "source": f"alpaca·{label}", "delayed": delayed,
                      "session": session_label(t["t"])}
    return out


def _spots_alpaca(symbols: list[str]) -> dict[str, dict]:
    """Freshest alpaca print per symbol. IEX is real-time but goes quiet at
    the close — when its print is stale, the 15-min delayed full SIP tape and
    the Blue Ocean overnight tape compete; the newest trade wins."""
    keys = _alpaca_keys()
    if not keys:
        return {}
    out = _alpaca_feed(symbols, "iex", "iex", False, keys)
    for feed, label in (("delayed_sip", "sip15"), ("overnight", "on")):
        stale = [s for s in symbols if s not in out or _age_s(out[s]["ts"]) > _FRESH_S]
        if not stale:
            break
        try:
            cand = _alpaca_feed(stale, feed, label, True, keys)
        except Exception:
            cand = {}
        for s, q in cand.items():
            if s not in out or _age_s(q["ts"]) < _age_s(out[s]["ts"]):
                out[s] = q
    return out


def _spot_yahoo(symbol: str) -> dict | None:
    """Last pre/post-inclusive 1m candle — during extended hours this is the
    real-time print (meta.regularMarketPrice would be the stale 16:00 close)."""
    r = _client.get(f"{_YAHOO}/{_yahoo_sym(symbol)}",
                    params={"interval": "1m", "range": "1d", "includePrePost": "true"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    px, ts = None, None
    stamps = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    for i in range(len(closes) - 1, -1, -1):
        if closes[i] is not None:
            px, ts = closes[i], stamps[i]
            break
    if px is None:                                   # holiday: meta fallback
        meta = res["meta"]
        px, ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if px is None:
        return None
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
    return {"ticker": symbol, "price": round(float(px), 2), "ts": iso,
            "source": "yahoo", "delayed": False, "session": session_label(iso)}


def _spot_cboe(symbol: str) -> dict | None:
    if symbol in _SPECIAL:                    # cboe quotes equities only
        return None
    r = _client.get(f"{_CBOE}/{symbol}.json")
    r.raise_for_status()
    d = r.json().get("data") or {}
    px = d.get("current_price") or d.get("last_trade_price") or d.get("close")
    if not px:
        return None
    return {"ticker": symbol, "price": round(float(px), 2),
            "ts": d.get("last_trade_time"), "source": "cboe·15m", "delayed": True,
            "session": session_label(d.get("last_trade_time"))}


def fetch_spots(symbols: list[str]) -> dict[str, dict]:
    """Batched fetch down the provider chain; partial results are fine.
    24h contract: providers keep being asked while a symbol's best print is
    older than _FRESH_S, and the NEWEST print wins — so after the close the
    chip shows post-market, then overnight, then premarket, never a frozen
    16:00 close."""
    out: dict[str, dict] = {}

    def _keep_newest(cands: dict[str, dict]) -> None:
        for s, q in cands.items():
            if s not in out or _age_s(q["ts"]) < _age_s(out[s]["ts"]):
                out[s] = q

    for prov in provider_order():
        want = [s for s in symbols if s not in out or _age_s(out[s]["ts"]) > _FRESH_S]
        if not want:
            break
        try:
            if prov == "alpaca":
                _keep_newest(_spots_alpaca(want))
            elif prov == "yahoo":
                for s in want:
                    q = _spot_yahoo(s)
                    if q:
                        _keep_newest({s: q})
            elif prov == "cboe":
                for s in want:
                    q = _spot_cboe(s)
                    if q:
                        _keep_newest({s: q})
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
    asym = _alpaca_sym(symbol)
    if asym is None:                          # crypto/futures: yahoo has them
        return None
    r = _client.get(f"{_ALPACA_DATA}/bars",
                    params={"symbols": asym, "timeframe": tf, "start": start,
                            "limit": min(limit, 1000), "adjustment": "split",
                            "feed": "iex", "sort": "desc"},
                    headers={"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]})
    r.raise_for_status()
    raw = (r.json().get("bars") or {}).get(asym) or []
    bars = [{"t": int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()),
             "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v", 0)}
            for b in raw]
    bars.reverse()  # requested desc for recency; charts want ascending
    return bars


def _bars_yahoo(symbol: str, interval: str, limit: int) -> list[dict] | None:
    _, yiv, yrange, factor, step = INTERVALS[interval]
    r = _client.get(f"{_YAHOO}/{_yahoo_sym(symbol)}",
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


# -------------------------------------------------------------- watchlist ----

_closes_cache: dict[str, tuple[float, float | None, float | None]] = {}
_CLOSES_TTL = 2 * 3600.0     # prev-close changes once a session
_watch_cache: tuple[float, tuple, list] | None = None   # (t, key, rows)
_rescue_at: dict[str, float] = {}    # sym -> last stale-rescue (monotonic)


def _closes(symbol: str) -> tuple[float | None, float | None]:
    """(previous session close, latest regular close) from yahoo meta, cached."""
    now = time.monotonic()
    hit = _closes_cache.get(symbol)
    if hit and now - hit[0] < _CLOSES_TTL:
        return hit[1], hit[2]
    prev = reg = None
    try:
        r = _client.get(f"{_YAHOO}/{_yahoo_sym(symbol)}",
                        params={"interval": "1d", "range": "1d"})
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        reg = meta.get("regularMarketPrice")
    except Exception:
        pass
    _closes_cache[symbol] = (now, prev, reg)
    return prev, reg


def watch_quotes(symbols: list[str]) -> list[dict]:
    """One row per symbol for the watchlist: last, day change vs previous
    close, and the extended-session move vs the regular close — the same
    numbers TradingView's watchlist shows. One batched alpaca call covers
    every US stock; specials (crypto, futures, indices) ride yahoo."""
    global _watch_cache
    syms, seen = [], set()
    for s in symbols:
        c = clean_symbol(s)
        if c and c not in seen:
            seen.add(c)
            syms.append(c)
    syms = syms[:150]
    key = tuple(syms)
    now = time.monotonic()
    if _watch_cache and _watch_cache[1] == key and now - _watch_cache[0] < 3.0:
        return _watch_cache[2]      # many open tabs share one provider hit

    quotes_out: dict[str, dict] = {}
    keys = _alpaca_keys()
    stocks = [s for s in syms if s not in _SPECIAL]
    if keys and stocks and "alpaca" in provider_order():
        try:
            quotes_out.update(_spots_alpaca(stocks))
        except Exception:
            pass
    if provider_order():
        # yahoo fills the gaps AND rescues frozen prints (e.g. IEX stuck at
        # the close): hunt when 30min < age < 12h. Beyond 12h the market is
        # simply closed — nothing anywhere is fresher, so don't ask. Bounded:
        # >=300s between rescues per symbol, <=25 rescues per call.
        now_m = time.monotonic()
        rescued = 0
        for s in syms:
            q = quotes_out.get(s)
            age = _age_s(q["ts"]) if q else 1e9
            missing = q is None
            frozen = 30 * 60 < age < 12 * 3600
            if not missing and not frozen:
                continue
            if rescued >= 25 or (not missing and now_m - _rescue_at.get(s, -1e9) < 300):
                continue
            _rescue_at[s] = now_m
            rescued += 1
            try:
                y = _spot_yahoo(s)
            except Exception:
                continue
            if y and (missing or _age_s(y["ts"]) < age):
                quotes_out[s] = y

    rows = []
    for s in syms:
        q = quotes_out.get(s)
        row: dict = {"sym": s}
        if q:
            row.update({"price": q["price"], "ts": q["ts"],
                        "session": q.get("session"), "source": q["source"]})
            prev, reg = _closes(s)
            if prev:
                row["chg"] = round(q["price"] - prev, 2)
                row["chg_pct"] = round((q["price"] / prev - 1) * 100, 2)
            if (reg and q.get("session") in ("pre", "post", "overnight")
                    and abs(q["price"] - reg) > 1e-9):
                row["ext_pct"] = round((q["price"] / reg - 1) * 100, 2)
        rows.append(row)
    _watch_cache = (now, key, rows)
    return rows


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
            # off-hours the tape prints slowly and every tick hunts several
            # feeds — ease off so the request budget stays tiny
            freshest = min((_age_s(q["ts"]) for q in spots.values()), default=1e9)
            if freshest > 90:
                cadence = max(cadence, 10.0)
        except Exception:
            pass  # provider hiccup: keep the loop alive
        await asyncio.sleep(cadence)
