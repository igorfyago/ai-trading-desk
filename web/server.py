"""The desk's web app: one conversation UI for all six agents, text and voice.

Endpoints
---------
GET  /                          the app
GET  /agents                    agent + persona catalog for the sidebar
POST /chat/{agent_id}           NDJSON stream: node progress, tool chips,
                                tokens, HITL interrupts, final answer
POST /chat/analyst/resume       continue the desk analyst after approve/revise/reject
POST /session/{persona}         ephemeral Realtime secret for a voice-native persona
POST /session/bridge/{agent_id} ephemeral secret for the VOICE BRIDGE: a Realtime
                                session whose only tool is the chosen text agent
POST /tool/{persona}            server-side execution of voice function calls
POST /tool/bridge/{agent_id}    ditto for the bridge (runs the text agent)

Observability: with LANGSMITH_TRACING=true every chat run, every graph node
and every voice tool call lands in LangSmith under LANGSMITH_PROJECT.

Run:  uvicorn web.server:app --reload   (from the repo root)
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents" / "06_voice"))

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from personas import PERSONAS, run_tool  # noqa: E402

load_dotenv()


async def _pnl_loop():
    """Mark open trades to the live feed and push P&L to the dock every 5s."""
    import asyncio

    from common import bus, trades

    while True:
        rows = []
        try:
            rows = await asyncio.to_thread(trades.positions_snapshot)
            if rows:
                bus.publish({"type": "pnl", "positions": rows})
        except Exception:
            pass
        await asyncio.sleep(5 if rows else 15)


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _lifespan(_app):
    """One live feed for the whole desk: the watch loop pushes ticks onto the
    bus; SSE endpoints fan them out to every open chart."""
    import asyncio

    from common import bus, quotes

    bus.set_loop(asyncio.get_running_loop())
    tasks = [asyncio.create_task(_pnl_loop())]
    if quotes.provider_order():
        tasks.append(asyncio.create_task(quotes.watch_loop()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="ai-trading-desk", lifespan=_lifespan)

# The landing portal (apex origin) calls /api/* from the browser.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://b4rruf3t.com", "https://www.b4rruf3t.com",
                   "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["GET", "POST"],   # POST = the sim's position buttons
    allow_headers=["*"],
)
STATIC = Path(__file__).resolve().parent / "static"
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime-2.1")


def AUDIO_CONFIG(voice: str) -> dict:
    """Voice out + SEMANTIC turn detection in: the model waits for end-of-thought,
    not just end-of-silence. Eagerness LOW = patient turn-taking, far fewer
    responses triggered by coughs/taps/background noise.

    interrupt_response FALSE is the anti-cutoff rule. By default the server
    TRUNCATES the agent mid-word the instant its VAD believes the caller
    started speaking · a breath, a chair, music in the room all qualify, so
    the agent kept killing his own sentences while the caller sat silent.
    Off, he finishes the thought and answers the turn next; a caller who
    really wants to cut in just talks, and their turn is served after the
    sentence instead of shredding it."""
    return {
        "output": {"voice": voice},
        "input": {
            "turn_detection": {"type": "semantic_vad", "eagerness": "low",
                               "interrupt_response": False},
            # transcribe the caller so their words render in the chat log
            "transcription": {"model": "gpt-4o-mini-transcribe"},
        },
    }


@app.get("/")
def index():
    """minitrade: the trading app IS this host now. The agent chat UI moved to
    /marcus so the cockpit can embed it from the same origin."""
    return FileResponse(STATIC.parent / "trade" / "index.html")


@app.get("/marcus")
def marcus_ui():
    return FileResponse(STATIC / "index.html")


@app.get("/api/summary/{ticker}")
def ticker_summary(ticker: str):
    """Everything the dashboard needs for one ticker, in one call."""
    from common import market, news, signals, social

    snap = market.latest_snapshot(ticker)
    if snap is None:
        raise HTTPException(404, f"no data for '{ticker}' · covered: SPY, QQQ, IWM")
    feed = market.resolve_feed(ticker)[0]
    return {
        "snapshot": snap,
        "live_spot": market.live_spot(ticker),
        "gex_live": market.live_gex(ticker),
        "walls": signals._latest_walls(feed),
        "trade": signals.recommend_trade(ticker),
        "headlines": news.fetch_news(ticker),
        "buzz": social.fetch_buzz(ticker),
        "trending": social.fetch_trending(),
        "ta_signals": latest_ta_signals(ticker),
    }


@app.post("/webhook/openai")
async def openai_webhook(request: Request):
    """Incoming phone calls (Realtime SIP): verify, accept as Riley, and run
    her tools server-side for the duration of the call. See docs/PHONE.md."""
    from web import phone

    if not phone.webhook_secret():
        raise HTTPException(503, "phone line not configured (OPENAI_WEBHOOK_SECRET)")
    event = phone.verify_and_parse(await request.body(), request.headers)
    if event is None:
        raise HTTPException(400, "invalid webhook signature")
    if event.get("type") == "realtime.call.incoming":
        return phone.handle_incoming(event)
    return {"handled": False, "type": event.get("type")}


@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request, token: str = ""):
    """Receives TradingView alert webhooks: custom TA indicators fire ->
    the desk knows within seconds. Token-gated."""
    expected = os.getenv("TV_WEBHOOK_TOKEN")
    if not expected or token != expected:
        raise HTTPException(403, "bad token")
    raw = (await request.body()).decode("utf-8", errors="ignore")[:2000]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"signal": raw}
    from datetime import datetime, timezone

    from common.db import get_connection

    conn = get_connection()
    conn.execute(
        "INSERT INTO ta_signals (created_at, ticker, signal, price, interval, payload)"
        " VALUES (?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(),
         str(data.get("ticker", "?")).upper()[:12],
         str(data.get("signal", raw))[:200],
         data.get("price"), str(data.get("interval", ""))[:12], raw),
    )
    conn.commit()
    conn.close()
    return {"stored": True}


def latest_ta_signals(ticker: str, limit: int = 5) -> list[dict]:
    from common.db import get_connection

    conn = get_connection()
    rows = conn.execute(
        "SELECT created_at, signal, price, interval FROM ta_signals"
        " WHERE ticker = ? ORDER BY id DESC LIMIT ?", (ticker.upper(), limit)).fetchall()
    conn.close()
    return [{"created_at": r[0], "signal": r[1], "price": r[2], "interval": r[3]} for r in rows]


@app.get("/api/replay/moments")
def replay_moments(ticker: str = "SPY"):
    """Snapshot timestamps available for replay, oldest first."""
    from common import replay

    ms = replay.moments(ticker)
    return {"ticker": ticker.upper(), "n": len(ms), "moments": ms}


@app.get("/api/replay/run")
def replay_run(ticker: str = "SPY", at: str = ""):
    """One blindfolded decision at a past moment, graded forward by the tape."""
    from common import replay

    if not at:
        raise HTTPException(400, "at=ISO timestamp required")
    return replay.run(ticker, at)


@app.get("/api/replay/sweep")
def replay_sweep(ticker: str = "SPY", start: str = "", end: str = "",
                 step: int = 60):
    """A decision every `step` minutes across the range, aggregated."""
    from common import replay

    if not start or not end:
        raise HTTPException(400, "start & end ISO timestamps required")
    return replay.sweep(ticker, start, end, step_minutes=max(15, step))


@app.get("/api/xfeed/{ticker}")
def xfeed(ticker: str):
    """The X card's real posts: the pulse's citations as oEmbed HTML."""
    from common import xpulse

    if not xpulse.available():
        return {"available": False, "posts": []}
    return {"available": True, "posts": xpulse.embeds_for(ticker)}


@app.get("/api/spot/{ticker}")
def spot(ticker: str):
    """Lightweight spot for always-on price chips: LIVE feed first, the
    collector snapshot as the offline fallback."""
    from common import market, signals

    live = market.live_spot(ticker)
    if live:
        out = {"ticker": ticker.upper(), "spot": live["price"], "as_of": live["ts"],
               "source": live["source"], "delayed": live["delayed"],
               "session": live.get("session")}
    else:
        snap = market.latest_snapshot(ticker)
        if snap is None:
            raise HTTPException(404, f"no data for '{ticker}'")
        out = {"ticker": snap["ticker"], "spot": snap["spot"],
               "as_of": snap["captured_at"], "source": "snapshot", "delayed": True}
    if ticker.upper() in ("SPY", "XSP"):
        out["xsp_est"] = round(out["spot"] + signals.XSP_OFFSET, 2)
    return out


@app.get("/api/watch")
def watch(symbols: str = ""):
    """Watchlist rows: last / day chg / chg% / extended-session move, batched.
    One alpaca call covers every US stock; crypto/futures/indices ride yahoo."""
    from common import quotes

    syms = [s for s in symbols.split(",") if s.strip()][:150]
    if not syms:
        raise HTTPException(400, "symbols=CSV required")
    from datetime import datetime, timezone

    return {"rows": quotes.watch_quotes(syms),
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# ------------------------------------------------------------ ticker logos --
# TradingView-style company icons: fetched once per symbol from keyless logo
# CDNs, cached on DISK forever, served with long-lived cache headers. A miss
# falls back to a generated monogram SVG so every row always has an icon.

from common.db import DATA_DIR as _DATA_DIR  # noqa: E402

_LOGO_DIR = _DATA_DIR / "logos"              # the deskdata volume: survives rebuilds
_LOGO_DIR.mkdir(parents=True, exist_ok=True)
_LOGO_SOURCES = [
    "https://assets.parqet.com/logos/symbol/{sym}?format=png",
    "https://financialmodelingprep.com/image-stock/{sym}.png",
]
_LOGO_MISS: dict[str, float] = {}       # sym -> monotonic time of last miss


def _monogram_svg(sym: str) -> bytes:
    hue = sum(ord(c) * 37 for c in sym) % 360
    letter = sym[0] if sym else "?"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
            f'<circle cx="20" cy="20" r="20" fill="hsl({hue},42%,32%)"/>'
            f'<text x="20" y="26" text-anchor="middle" font-family="Arial,sans-serif" '
            f'font-size="19" font-weight="600" fill="#fff">{letter}</text></svg>').encode()


@app.get("/api/logo/{sym}")
def ticker_logo(sym: str):
    import re as _re
    import time as _time

    from fastapi.responses import Response

    s = _re.sub(r"[^A-Z0-9.]", "", sym.upper())[:10]
    if not s:
        raise HTTPException(400, "bad symbol")
    headers = {"Cache-Control": "public, max-age=604800, immutable"}
    png = _LOGO_DIR / f"{s}.png"
    if png.exists():
        return Response(png.read_bytes(), media_type="image/png", headers=headers)
    # real fetch at most once per day per symbol; monogram in between
    if _time.monotonic() - _LOGO_MISS.get(s, -1e9) > 86400 and "!" not in sym:
        import httpx
        for url in _LOGO_SOURCES:
            try:
                r = httpx.get(url.format(sym=s), timeout=6, follow_redirects=True)
                if r.status_code == 200 and r.content[:4] != b"<svg" and len(r.content) > 200:
                    png.write_bytes(r.content)
                    return Response(r.content, media_type="image/png", headers=headers)
            except Exception:
                continue
        _LOGO_MISS[s] = _time.monotonic()
    return Response(_monogram_svg(s), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/bars/{ticker}")
def bars(ticker: str, interval: str = "5m", limit: int = 600):
    """Candles for the site's own chart (lightweight-charts): live feed data,
    keyless fallbacks included. Interval labels: 1m 3m 5m 15m 45m 4h D W."""
    from common import quotes

    if quotes.normalize_interval(interval) is None:
        raise HTTPException(422, f"unknown interval '{interval}'")
    data = quotes.get_bars(ticker, interval, min(max(limit, 50), 1000))
    if data is None:
        raise HTTPException(503, "no candle feed available (set ALPACA keys, or "
                                 "the fallback providers are unreachable)")
    return data


def _sse(gen):
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# The bus only carries ticks for WATCH_SYMBOLS (what watch_loop polls); a
# stream focused on any other ticker side-polls it itself at this cadence,
# batched through the provider chain so the request budget stays tiny.
SIDE_POLL_S = 25.0


@app.get("/api/stream/quotes")
async def stream_quotes(symbols: str = ""):
    """SSE: live ticks for the requested symbols (default: the watch list).
    One shared upstream feed, any number of browser subscribers; symbols the
    watch loop doesn't cover keep ticking via a per-stream side poll."""
    import asyncio

    from common import bus, quotes

    want = {s.strip().upper() for s in symbols.split(",") if s.strip()} or \
        set(quotes.watch_symbols())

    async def gen():
        q = bus.subscribe()
        loop = asyncio.get_running_loop()
        extra = sorted(want - set(quotes.watch_symbols()))
        seen: dict[str, tuple] = {}    # side-channel dedup: (price, ts)
        next_side = loop.time() + SIDE_POLL_S
        try:
            for sym in sorted(want):   # current state first, ticks after
                spot = quotes.get_spot(sym)
                if spot:
                    if sym in extra:
                        seen[sym] = (spot["price"], spot["ts"])
                    yield f"data: {json.dumps({'type': 'quote', **spot})}\n\n"
            while True:
                if extra and loop.time() >= next_side:
                    next_side = loop.time() + SIDE_POLL_S
                    try:
                        spots = await asyncio.to_thread(
                            quotes.poll_spots, extra, SIDE_POLL_S * 0.8)
                    except Exception:
                        spots = {}
                    for sym in extra:
                        spot = spots.get(sym)
                        if spot and seen.get(sym) != (spot["price"], spot["ts"]):
                            seen[sym] = (spot["price"], spot["ts"])
                            yield f"data: {json.dumps({'type': 'quote', **spot}, default=str)}\n\n"
                timeout = 15.0 if not extra else \
                    max(0.2, min(15.0, next_side - loop.time()))
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if ev.get("type") == "quote" and ev.get("ticker") in want:
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
        finally:
            bus.unsubscribe(q)

    return _sse(gen())


@app.get("/api/stream/events")
async def stream_events():
    """SSE: trade lifecycle + P&L marks for the dock chart."""
    import asyncio

    from common import bus, trades

    async def gen():
        q = bus.subscribe()
        try:
            boot = {"type": "boot",
                    "positions": trades.positions_snapshot(),
                    "recent": trades.recent_trades(8)}
            yield f"data: {json.dumps(boot, default=str)}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if ev.get("type") in ("trade", "pnl"):
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
        finally:
            bus.unsubscribe(q)

    return _sse(gen())


# ----------------------------------------------------------------- identity
# PERMISSIVE SSO, per the estate rollout: recognise a token if one arrives,
# never reject a request for not having one. The desk is a public demo and
# most of its traffic will never hold an account.
#
# The desk's idea of "who" is a session string the browser mints into
# localStorage. That is a conversation key, not an identity: anyone who knows
# one can read its transcript and act on its book. Once a token can prove who
# is calling, the token has to WIN over that string, or the parameter becomes
# an instruction to act as somebody else.
#
# Settled now, while nothing is enforced and it is free, rather than on
# activation day when it is a live IDOR. Same rule, same reasoning, as
# dev.minibank.broker.CallerIdentity and dev.minibank.ledger.SsoIdentity.


async def sso_user(request: Request):
    """The SSO user behind this request, or None. Never raises, never 401s."""
    try:
        from common.sso_client import validate_bearer

        return await validate_bearer(request.headers.get("Authorization"))
    except Exception:
        # An identity provider being down is an outage in the directory, not
        # in the desk. It degrades to anonymous, which is exactly today's
        # behaviour on every route.
        return None


async def caller_session(request: Request, requested: str) -> str:
    """Which session this request may act as · the precedence rule.

    A proven identity wins over the supplied string; without one the string
    stands, byte for byte as before.
    """
    user = await sso_user(request)
    # `user.sub` and not just `user`: the validator accepts an empty-string
    # sub for parity with the Java client, and an empty owner would book
    # trades against nobody. An empty sub is not an identity.
    if user is not None and user.sub:
        return user.sub
    return requested


@app.get("/api/trades")
def list_trades(limit: int = 12):
    """Trade log + live-marked open positions (the dock's boot state)."""
    from common import trades

    return {"positions": trades.positions_snapshot(),
            "recent": trades.recent_trades(min(max(limit, 1), 50))}


class TradeAction(BaseModel):
    action: str                 # add | sell | close
    qty: int = 1
    price: float | None = None  # defaults to the live model mark


@app.post("/api/trades/{trade_id}/action")
def trade_action(trade_id: int, body: TradeAction):
    """The sim's order ticket: ADD / SELL / CLOSE buttons on a position."""
    from common import trades

    out = trades.adjust(trade_id, body.action, body.qty, body.price)
    if "error" in out:
        raise HTTPException(422, out["error"])
    return out


@app.get("/api/chatlog")
async def chat_history(request: Request, session: str, limit: int = 60):
    """The shared transcript: one conversation across the dashboard panel and
    the full desk (same browser = same session id)."""
    from common.db import get_connection

    # a transcript is readable by anyone who knows the session string, which
    # is fine while nobody has an account and is not once somebody does
    session = await caller_session(request, session)

    conn = get_connection()
    rows = conn.execute(
        "SELECT created_at, agent, role, content FROM chat_log"
        " WHERE session = ? ORDER BY id DESC LIMIT ?",
        (session, min(max(limit, 1), 200))).fetchall()
    conn.close()
    return {"messages": [dict(zip(("created_at", "agent", "role", "content"), r))
                         for r in reversed(rows)]}


@app.get("/api/score")
def game_score():
    """The scoreboard chip: realized + live unrealized P&L across the book."""
    from common import trades

    return trades.score()


@app.get("/api/tape/{ticker}")
def tape_read(ticker: str, interval: str = "15m"):
    """The house tape read: VWAP bands + RSI state + volume-profile walls/gaps
    + Heikin-Ashi thickness, staged the way the desk trades it."""
    from common import tape

    out = tape.get_tape_read(ticker, interval)
    if out is None:
        raise HTTPException(503, "no candle feed available for a tape read")
    return out


@app.get("/api/xpulse/{ticker}")
def x_pulse(ticker: str):
    """X chatter summary via Grok x_search — separate endpoint because the
    live search takes seconds; the dashboard loads it async."""
    from common import xpulse

    if not xpulse.available():
        return {"available": False}
    p = xpulse.pulse(ticker)
    return {"available": True, "pulse": p}


DESK_PERSONAS = ["marcus"]   # the agency demos moved to the observatory


@app.get("/agents")
def agents():
    """The persona catalog. Also the container healthcheck and the k8s probe,
    so this route stays cheap and dependency-free even though the agent
    gallery it used to feed now lives in the observatory."""
    return {
        "voice_personas": [
            {"id": pid, "label": PERSONAS[pid]["label"],
             "tagline": PERSONAS[pid]["tagline"], "category": "finance"}
            for pid in DESK_PERSONAS if pid in PERSONAS
        ],
        "realtime_model": REALTIME_MODEL,
    }


# ------------------------------------------------------------------ voice ----

async def _mint_secret(session_payload: dict) -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(500, "OPENAI_API_KEY not configured on the server")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"session": session_payload},
        )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"OpenAI error: {resp.text[:300]}")
    return resp.json()


@app.post("/session/{persona}")
async def create_session(persona: str):
    p = PERSONAS.get(persona)
    if p is None:
        raise HTTPException(404, f"unknown persona '{persona}'")
    instructions = p["instructions"]
    if "position_status" in p.get("implementations", {}):
        # trading personas start the call already knowing the clock and the book
        from common import quotes, trades

        instructions += "\n\n" + quotes.trading_clock_block()
        book = trades.book_block() or "BOOK: flat, nothing on."
        instructions += (
            "\n\n# The book at call start\n" + book +
            "\n(Snapshot from when the call connected — the caller can ADD/SELL/"
            "CLOSE from the chart at any moment; the 'book' line on tool results "
            "and position_status are the live truth.)")
    data = await _mint_secret({
        "type": "realtime", "model": REALTIME_MODEL,
        "instructions": instructions,
        "tools": p["tools"], "tool_choice": "auto",
        "audio": AUDIO_CONFIG(p["voice"]),
    })
    return {"client_secret": data["value"], "label": p["label"], "model": REALTIME_MODEL}


class ToolCall(BaseModel):
    name: str
    arguments: dict
    session: str = "voice"


@app.post("/tool/{persona}")
async def execute_tool(persona: str, call: ToolCall, request: Request):
    if persona not in PERSONAS:
        raise HTTPException(404, f"unknown persona '{persona}'")
    # confirm_entry, trim_half and close_position all act on a book keyed by
    # this session, so this is the route where the precedence rule earns its
    # keep. Anonymous callers are unaffected.
    session = await caller_session(request, call.session)
    return {"output": run_tool(persona, call.name, call.arguments,
                               session=session)}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
# Local dev convenience: preview the apex portal without deploying. Caddy serves
# it in production. Mounted at /portal, not /landing, which no route has ever
# answered.
app.mount("/portal", StaticFiles(directory=STATIC.parent / "portal", html=True),
          name="portal")
