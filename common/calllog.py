"""Every voice turn Marcus takes, on disk, so the persona gets corrected from
evidence instead of memory.

A transcript alone cannot tell you why a turn felt wrong. The complaints are
always about the clock: dead air before the first word, a tool that stalled,
a reply that ran over the caller. So a record is the words PLUS the timing,
stamped with the persona revision that produced it. Without that revision
hash you cannot tell whether last week's prompt edit actually helped or you
just got an easier caller, which is the whole point of keeping the log.

Append-only JSONL, one file per day. Nothing to migrate, greppable by hand,
and a crashed call costs one line instead of a table.

    python -m common.calllog review                    # last 200 turns
    python -m common.calllog review --day 2026-07-19 --surface phone
    python -m common.calllog review --rev a1b2c3d4     # one prompt version
    python -m common.calllog export                    # raw JSONL to stdout

Deliberately not readable over HTTP: the desk is public and these are whole
conversations. Read it on the box.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

CALLS_DIR = Path(__file__).resolve().parent.parent / "data" / "calls"
_WRITE_LOCK = threading.Lock()

# a turn that talks for four seconds is a different problem than one that
# waits four seconds, so the cap is generous on words and hard on nothing else
MAX_TEXT = 4000


def persona_rev(instructions: str) -> str:
    """Short hash of the prompt that produced a turn.

    Turns are only comparable across time if you know which persona said
    them. This is the join key between 'calls got worse on Tuesday' and
    'I edited VOICE_STYLE on Tuesday'.
    """
    return hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:8]


def _day_file(day: str | None = None) -> Path:
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return CALLS_DIR / f"{day}.jsonl"


def log_turn(rec: dict) -> None:
    """Append one turn. Never raises: a logging fault must not cost a call."""
    try:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "surface": str(rec.get("surface") or "web")[:16],
            "session": str(rec.get("session") or "")[:64],
            "persona": str(rec.get("persona") or "marcus")[:32],
            "rev": str(rec.get("rev") or "")[:16],
            "user": str(rec.get("user") or "")[:MAX_TEXT],
            "agent": str(rec.get("agent") or "")[:MAX_TEXT],
            "ms_dead_air": _int_or_none(rec.get("ms_dead_air")),
            "tools": [
                {"name": str(t.get("name"))[:64], "ms": _int_or_none(t.get("ms"))}
                for t in (rec.get("tools") or [])[:8]
                if isinstance(t, dict)
            ],
            "covered": _bool_or_none(rec.get("covered")),
            "barge_in": bool(rec.get("barge_in")),
        }
        if not row["user"] and not row["agent"]:
            return                      # nothing was said: not evidence
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _WRITE_LOCK:
            CALLS_DIR.mkdir(parents=True, exist_ok=True)
            with open(_day_file(), "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass


def _int_or_none(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _bool_or_none(v) -> bool | None:
    return None if v is None else bool(v)


def read_turns(day: str | None = None, surface: str = "",
               rev: str = "", limit: int = 0) -> list[dict]:
    """Turns oldest-first. `day=None` reads every day on disk."""
    files = sorted(CALLS_DIR.glob("*.jsonl")) if day is None else [_day_file(day)]
    out: list[dict] = []
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue            # a half-written line is not worth a crash
            if surface and row.get("surface") != surface:
                continue
            if rev and row.get("rev") != rev:
                continue
            out.append(row)
    return out[-limit:] if limit else out


def _pct(vals: list[int], p: float) -> int | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(int(len(s) * p), len(s) - 1)]


def review(day: str | None = None, surface: str = "", rev: str = "",
           limit: int = 200, worst: int = 10) -> str:
    """What to fix next, ranked by how long the caller sat in silence."""
    turns = read_turns(day, surface, rev, limit)
    if not turns:
        return "no turns logged yet · take a call first"

    dead = [t["ms_dead_air"] for t in turns if t.get("ms_dead_air") is not None]
    tool_turns = [t for t in turns if t.get("tools")]
    covered = [t for t in tool_turns if t.get("covered") is True]
    barged = [t for t in turns if t.get("barge_in")]
    revs = sorted({t.get("rev") or "?" for t in turns})

    L = [
        f"{len(turns)} turns · revs {', '.join(revs)}",
        f"dead air   p50 {_fmt_ms(_pct(dead, 0.50))}  p90 {_fmt_ms(_pct(dead, 0.90))}"
        f"  max {_fmt_ms(max(dead) if dead else None)}",
    ]
    if tool_turns:
        L.append(f"tool turns {len(tool_turns)} · wait covered by speech "
                 f"{len(covered)}/{len(tool_turns)} "
                 f"({100 * len(covered) // len(tool_turns)}%)")
    L.append(f"barge-ins  {len(barged)}")

    slow = sorted((t for t in turns if t.get("ms_dead_air") is not None),
                  key=lambda t: -t["ms_dead_air"])[:worst]
    if slow:
        L.append(f"\nworst {len(slow)} by dead air")
        for t in slow:
            tools = " ".join(f"{x['name']}:{_fmt_ms(x['ms'])}" for x in t.get("tools") or [])
            flag = "" if t.get("covered") is not False else "  UNCOVERED"
            L.append(f"  {_fmt_ms(t['ms_dead_air']):>7}  {t['rev']}  {tools}{flag}")
            L.append(f"           caller: {_clip(t.get('user'))}")
            L.append(f"           marcus: {_clip(t.get('agent'))}")
    return "\n".join(L)


def _fmt_ms(ms: int | None) -> str:
    return "-" if ms is None else (f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s")


def _clip(s: str | None, n: int = 100) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _main() -> None:
    import argparse
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")   # cp1252 mangles the separators
    except Exception:
        pass

    ap = argparse.ArgumentParser(prog="python -m common.calllog")
    ap.add_argument("cmd", choices=["review", "export"])
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (default: all)")
    ap.add_argument("--surface", default="", help="web | phone")
    ap.add_argument("--rev", default="", help="persona revision hash")
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()

    if a.cmd == "export":
        for t in read_turns(a.day, a.surface, a.rev, a.limit):
            print(json.dumps(t, ensure_ascii=False))
    else:
        print(review(a.day, a.surface, a.rev, a.limit))


if __name__ == "__main__":
    _main()
