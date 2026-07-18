"""Build web/trade/index.html (minitrade) from the old portal page.

The trading cockpit already lived inside the apex landing as its Trade /
Positions / Replay / GEX tabs. This lifts exactly those out, drops the
portfolio tabs (overview, voice kit, atlas, agents, observatory, bank) and
re-skins the result in the house look shared with minibank: GitHub-dark
palette, one lowercase word for a title, a TOP tab bar instead of the
hover-out left rail.

Idempotent: rerun after editing the source page and it rebuilds.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "web" / "landing" / "index.html"
OUT = ROOT / "web" / "trade" / "index.html"

# panes that belong to the portal, not to the trading app
DROP_PANES = ["overview", "kit", "atlas", "desk", "observatory", "bank"]

MINIBANK_ROOT = """  :root {
    /* the house palette · identical to minibank and the rest of the estate */
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2230; --line:#21262d;
    --text:#e6edf3; --dim:#8b949e;
    --accent:#58a6ff; --green:#3fb950; --red:#f85149; --amber:#d29922;
    --violet:#bc8cff;
    --mono:"IBM Plex Mono",ui-monospace,monospace;
  }
"""

TOPBAR = """  <header class="topbar">
    <span class="brand">minitrade</span>
    <button class="tab-btn on" data-pane="dash">Trade</button>
    <button class="tab-btn" data-pane="positions">Positions</button>
    <button class="tab-btn" data-pane="replay">Replay</button>
    <button class="tab-btn" data-pane="gex">GEX</button>
    <a class="tab-btn" href="https://b4rruf3t.com">All apps</a>
    <a class="tab-btn" href="https://github.com/igorfyago/ai-trading-desk"
       target="_blank">GitHub</a>
    <span class="tb-spacer"></span>
  </header>
"""

TOPBAR_CSS = """
  /* ---- top tab bar · the estate's shared chrome (see minibank) ---------- */
  body { flex-direction:column; }
  .topbar { flex:none; display:flex; align-items:center; gap:4px; height:46px;
            padding:0 12px; background:var(--panel); border-bottom:1px solid var(--line);
            overflow-x:auto; scrollbar-width:none; }
  .topbar::-webkit-scrollbar { display:none; }
  .brand { font-weight:700; font-size:15px; letter-spacing:-.01em; margin-right:10px;
           white-space:nowrap; }
  .tab-btn { background:none; border:1px solid transparent; border-radius:7px;
             color:var(--dim); font:inherit; font-size:13px; padding:5px 11px;
             cursor:pointer; white-space:nowrap; text-decoration:none; }
  .tab-btn:hover { color:var(--text); background:var(--panel2); }
  .tab-btn.on { color:var(--text); background:var(--panel2); border-color:var(--line); }
  .tb-spacer { flex:1; }
  main { flex:1; min-width:0; display:flex; margin-left:0 !important; }
  @media (max-width:700px) {
    .topbar { height:42px; }
    .brand { font-size:14px; margin-right:6px; }
    .tab-btn { padding:5px 9px; font-size:12.5px; }
  }
"""


def drop_pane(html: str, pane_id: str) -> str:
    """Remove <section class="pane" id="X"> ... </section> by tag balance."""
    m = re.search(rf'<section class="pane[^"]*" id="{pane_id}">', html)
    if not m:
        return html
    i, depth = m.end(), 1
    while depth and i < len(html):
        nxt = re.search(r"</?section\b", html[i:])
        if not nxt:
            break
        tag_at = i + nxt.start()
        depth += -1 if html[tag_at:tag_at + 9] == "</section" else 1
        i = tag_at + 9
    end = html.find(">", i)
    return html[:m.start()] + html[end + 1:]


def main() -> None:
    html = SRC.read_text(encoding="utf-8")

    # 1. identity
    html = html.replace("<title>b4rruf3t · AI Trading Desk</title>",
                        "<title>minitrade</title>")
    html = html.replace('<meta name="theme-color" content="#13151b">',
                        '<meta name="theme-color" content="#0d1117">')

    # 2. the shared palette
    html = re.sub(r"  :root \{.*?\n  \}\n", MINIBANK_ROOT, html, count=1, flags=re.S)

    # 3. ONE house palette everywhere: the theme switcher used to paint its own
    # colours onto documentElement, which is the opposite of a shared look
    html = re.sub(r'<script src="[^"]*themes\.js[^"]*"[^>]*></script>\n', "", html)

    # 4. the left rail becomes a top tab bar
    nav = re.search(r"  <nav>.*?  </nav>\n", html, flags=re.S)
    if nav:
        html = html[:nav.start()] + TOPBAR + html[nav.end():]
    # the phone drawer belonged to the rail · drop its markup AND its wiring,
    # or the script dies on a null button before it ever boots the dash
    html = re.sub(r'  <button id="m-nav-btn".*?</div>\n', "", html, flags=re.S)
    html = re.sub(r"\n  // phone drawer:.*?\n  \}\);\n", "\n", html, flags=re.S)
    html = html.replace("</style>", TOPBAR_CSS + "</style>", 1)

    # 4. portal-only panes go back to the portal
    for pane in DROP_PANES:
        html = drop_pane(html, pane)

    # 4b. and their JS with them · the voice-kit calculators and the atlas
    # renderer run at load and reach for elements that no longer exist, which
    # killed the whole script before the dash ever booted
    html = re.sub(r"  /\*\s*-+\s*agent atlas\s*-+\s*\*/.*?(?=  /\*\s*-+\s*dashboard\s*-+\s*\*/)",
                  "", html, flags=re.S)
    html = html.replace('    if (name === "atlas") initAtlas();\n', "")

    # 5. same origin now: the API and the Marcus panel are served by this app
    html = re.sub(r'const API = .*?;\n', 'const API = "";\n', html, count=1, flags=re.S)
    html = html.replace('dock.src = API === "" ? "/?agent=marcus&embed=1" : dock.dataset.src;',
                        'dock.src = "/marcus?agent=marcus&embed=1";')
    html = re.sub(r'data-src="https://desk\.b4rruf3t\.com/\?agent=marcus&embed=1"',
                  'data-src="/marcus?agent=marcus&embed=1"', html)

    # 6. the tab machinery: .tab -> .tab-btn, active -> on
    html = html.replace('document.querySelectorAll(".tab[data-pane]").forEach(t =>\n'
                        '      t.classList.toggle("active", t.dataset.pane === name));',
                        'document.querySelectorAll(".tab-btn[data-pane]").forEach(t =>\n'
                        '      t.classList.toggle("on", t.dataset.pane === name));')

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    kept = re.findall(r'<section class="pane[^"]*" id="([^"]+)"', html)
    print(f"wrote {OUT.relative_to(ROOT)}  ({len(html):,} bytes)")
    print("panes kept:", kept)
    print("nav is top bar:", "class=\"topbar\"" in html)
    print("api same-origin:", 'const API = "";' in html)


if __name__ == "__main__":
    main()
