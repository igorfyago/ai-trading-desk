"""In-process event bus: trade events and live ticks fan out to SSE clients.

publish() is safe from ANY thread (FastAPI runs sync endpoints and voice tool
calls in a threadpool) and is a silent no-op when no server loop is running,
so the CLI agents can import modules that publish without caring.
"""

import asyncio
import threading

_loop: asyncio.AbstractEventLoop | None = None
_subs: list[asyncio.Queue] = []
_lock = threading.Lock()


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once by the web server at startup; enables cross-thread publish."""
    global _loop
    _loop = loop


def subscribe(maxsize: int = 500) -> asyncio.Queue:
    """New subscriber queue (call from the event loop, i.e. an async endpoint)."""
    q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    with _lock:
        _subs.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _lock:
        if q in _subs:
            _subs.remove(q)


def _fanout(event: dict) -> None:
    with _lock:
        subs = list(_subs)
    for q in subs:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:  # slow client: drop, never block the desk
            pass


def publish(event: dict) -> None:
    """Fire-and-forget from any thread. Dropped when the server isn't running."""
    loop = _loop
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(_fanout, event)
    except RuntimeError:  # loop shutting down
        pass
