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
    <span class="brand">mini<span>trade</span></span>
    <div class="tb-nav">
      <button class="tab-btn on" data-pane="dash">Trade</button>
      <button class="tab-btn" data-pane="positions">Positions</button>
      <button class="tab-btn" data-pane="replay">Replay</button>
      <button class="tab-btn" data-pane="gex">GEX</button>
      <a class="tab-btn" href="https://bank.b4rruf3t.com">Bank</a>
      <a class="tab-btn" href="https://mart.b4rruf3t.com">Mart</a>
      <a class="tab-btn" href="https://b4rruf3t.com">All apps</a>
      <a class="tab-btn" href="https://github.com/igorfyago/ai-trading-desk"
         target="_blank">GitHub</a>
      <div id="g-score" onclick="show('positions')"
           title="net P&amp;L: realized + open · positions">P&amp;L &ndash;</div>
    </div>
  </header>
"""

TOPBAR_CSS = """
  /* ---- the estate's chrome, taken from minibank VERBATIM -----------------
     Not "inspired by". The pill shape, the sizes, the accent fill and the
     two-tone wordmark are copied, because four properties that each look
     slightly different read as four projects and one that looks the same
     everywhere reads as one product. */
  body { flex-direction:column; overflow:hidden;
         /* the HOUSE FONT. This page used Inter at 15px/1.65 while the rest of
            the estate uses the system stack at 14px/1.5, so the same wordmark
            at the same nominal size rendered visibly different here. Identical
            type is most of what "consistent" actually means. */
         font:14px/1.5 system-ui,'Segoe UI',sans-serif; }
  .topbar { flex:none; display:flex; align-items:center; gap:20px;
            padding:14px 22px; border-bottom:1px solid var(--line);
            overflow-x:auto; scrollbar-width:none; }
  .topbar::-webkit-scrollbar { display:none; }
  .brand { font-size:17px; font-weight:650; letter-spacing:-0.02em; white-space:nowrap; }
  .brand span { color:var(--accent); }
  .tb-nav { display:flex; gap:6px; margin-left:auto; align-items:center; }
  .tab-btn { background:none; border:1px solid var(--line); color:var(--dim);
             padding:6px 16px; border-radius:999px; font:600 13px system-ui;
             cursor:pointer; white-space:nowrap; text-decoration:none;
             transition:color .13s, border-color .13s, background .13s; }
  .tab-btn:hover { color:var(--text); border-color:var(--dim); }
  .tab-btn.on { background:var(--accent); border-color:var(--accent); color:#fff; }

  /* A pill laid out in the row, not a chip pinned to the window corner, which
     is why it used to align with nothing. */
  #g-score { position:static; margin-left:2px; cursor:pointer;
             border:1px solid var(--line); color:var(--dim);
             padding:6px 16px; border-radius:999px; font:600 13px system-ui;
             white-space:nowrap; }
  #g-score.pos { color:var(--green); border-color:rgba(63,185,80,.45); }
  #g-score.neg { color:var(--red); border-color:rgba(248,81,73,.45); }

  /* ---- one screen ------------------------------------------------------
     min-height:0 is the whole reason this fits. A flex item defaults to
     min-height:auto and REFUSES to shrink below its content, so main grew to
     whatever the chart wanted and pushed the page to two screens while
     #dash's overflow:hidden sat there with no height to clip inside. */
  main { flex:1; min-width:0; min-height:0; display:flex; margin-left:0 !important; }
  .pane { min-height:0; }
  /* 22px matches the bar exactly, so the pill, the chart and the watchlist
     share one right edge. At 18px the pill sat four pixels inside everything
     below it. */
  #dash { padding:14px 22px 16px !important; }

  /* The strip reserved 150px on its right for the P&L chip that now lives in
     the bar, leaving the chart's chips 149px short of every other edge. On
     desktop it has nothing left to show; it returns on a phone, where the P&L
     is seated in it. */
  .dtop { display:none !important; padding-right:0 !important; }
  @media (max-width:700px) { .dtop { display:flex !important; } }

  /* The timeframes and the chart's chips are ONE row. They were two stacked
     bands, which is what made that area look like two different pages. */
  .chartbar { display:flex; align-items:center; gap:10px; flex:none; }
  .chartbar #intervals { flex:1; min-width:0; }
  .chartbar .watch { flex:none; margin-left:auto; }

  /* A hard pixel floor is fine on a big monitor and is exactly what forces a
     laptop into a second screen. */
  #tv-wrap { min-height:min(340px, 34vh) !important; }
  #agent-dock { min-height:min(200px, 20vh) !important; }

  @media (max-width:820px) {
    .topbar { padding:10px 12px; gap:10px; }
    .brand { font-size:15px; }
    .tab-btn, #g-score { padding:5px 11px; font-size:12.5px; }
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

    # 5a. The bar carries its own P&L pill, and the source page has a
    # standalone one that used to float in the window corner. Leaving both
    # gives the document TWO elements with the same id, which makes
    # getElementById a coin toss and left the loose one rendering as a
    # full-width bar at the foot of the page.
    loose_pill = (r'\n\s*<div id="g-score" onclick="show'
                  r'\(&apos;positions&apos;\)"[^>]*>[^<]*</div>')
    html = re.sub(loose_pill.replace("&apos;", "'"), "\n", html, count=1)

    # 5b. THIS IS the full desk, so a link that opens the full desk is
    # nonsense here. It belongs in the portal, where the trade pane really is
    # a window onto something else.
    html = re.sub(r'\n\s*<a href="https://desk\.b4rruf3t\.com/\?agent=marcus"[^>]*>open full desk[^<]*</a>',
                  "", html)

    # 5c. The chart's own chips join the timeframe row instead of floating in a
    # strip above it. Two bands for one set of controls is what made the area
    # read as two pages.
    chips = re.search(r'\n\s*<div class="watch" style="margin-left:auto">\s*'
                      r'<span id="feed-chip".*?</a>\s*</div>', html, flags=re.S)
    if chips:
        html = html[:chips.start()] + html[chips.end():]
        html = html.replace('<div class="watch" id="intervals"></div>',
                            '<div class="chartbar">\n'
                            '            <div class="watch" id="intervals"></div>\n'
                            '            <div class="watch">\n'
                            '              <span id="feed-chip" class="wchip"'
                            ' style="pointer-events:none;display:none"></span>\n'
                            '              <a id="tv-full" href="#" target="_blank" class="wchip add"\n'
                            '                 style="text-decoration:none">TradingView \u2197</a>\n'
                            '            </div>\n'
                            '          </div>', 1)

    # 5d. The P&L is seated by LAYOUT now, so it is seated into the bar. It used
    # to be re-parented onto document.body, correct for a fixed corner chip and
    # wrong for a pill: it rendered as a full-width bar at the foot of the page.
    html = html.replace('    else document.body.appendChild(g);',
                        '    else document.querySelector(".tb-nav").appendChild(g);')
    html = html.replace('    if (_mqPhone.matches) document.querySelector(".dtop").appendChild(g);',
                        '    if (!g) return;\n'
                        '    if (_mqPhone.matches) document.querySelector(".dtop").appendChild(g);')

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
