/* DeskChart v2 — the site's own chart engine.
   TradingView's open-source lightweight-charts (vendored, v5) + our own live
   feed + our own study math. One implementation drives BOTH the landing
   dashboard chart and the desk trade dock, so every surface reads the same
   candles the agents read.

   v2 replicates the desk's house TradingView layout: Heikin Ashi display
   candles (raw OHLC underneath — every study computes on raw), Europe/Dublin
   axis time, interval-dependent study sets (session VWAP ±1σ/2σ intraday,
   EMA21/SMA100/SMA200 daily, DC96 always), an RSI-14 pane, and a
   visible-range volume profile drawn as a series primitive.

   Theme: layout colors come from the live CSS vars and repaint on the
   themes.js "themechange" event; the MA/RSI/profile colors are fixed
   identity colors on purpose. The library's TradingView attribution logo
   stays ON (license requirement — do not disable). */

(function () {
  "use strict";

  const cssVar = (name, fb) =>
    (getComputedStyle(document.documentElement).getPropertyValue(name) || "").trim() || fb;

  function alpha(color, a) {
    // hex (#rgb / #rrggbb) -> rgba with alpha; rgb/rgba strings pass through.
    if (!color.startsWith("#")) return color;
    let h = color.slice(1);
    if (h.length === 3) h = [...h].map((c) => c + c).join("");
    const n = parseInt(h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  function palette() {
    const text = cssVar("--text", "#eceef4"), dim = cssVar("--dim", "#9ba3b2");
    return {
      text, dim,
      line: cssVar("--line", "rgba(255,255,255,.09)"),
      accent: cssVar("--accent", "#7c8aff"),
      green: cssVar("--green", "#3ecf8e"),
      red: cssVar("--red", "#f4657f"),
      mono: cssVar("--mono", "IBM Plex Mono, monospace"),
    };
  }

  // TradingView-clone palette. The CHART is a fixed instrument — it looks like
  // the house TV layout on every site theme (product spec: clone TV exactly).
  const FIXED = {
    bg: "#131722", text: "#b2b5be", grid: "#1e222d",
    scaleBorder: "#2a2e39", crosshair: "#758696",
    // EXTRACTED from the house TradingView layout ("Stocks", 2026-07-17) —
    // these are the boss's exact study styles, not TV defaults. Change only
    // against a fresh extraction.
    up: "#089981", down: "#f23645",
    volUp: "#26a69a", volDown: "rgba(242,54,69,0.5)",   // yes: solid up, 50% down
    volMa: "#ffffff",
    // VWAP AA: azure core; green above / deep-orange below; 2σ dashed; the
    // script's DEFAULT inner shades: green wash above the core, orange below
    vwap: "#0496ff", bandUp: "#4caf50", bandDn: "#e65100",
    // the VWAP shade WINS over every backdrop (DC fill, session tint) — you
    // can always tell the band area apart, like on the boss's charts
    vwapFillU1: "rgba(76,175,80,0.16)", vwapFillU2: "rgba(76,175,80,0.09)",
    vwapFillD1: "rgba(230,81,0,0.14)", vwapFillD2: "rgba(230,81,0,0.08)",
    // Donchian 96: white 50% ceiling, red 25% floor, blue 5% channel fill —
    // that fill is the navy wash you see across the whole chart
    dcU: "rgba(255,255,255,0.50)", dcL: "rgba(242,54,69,0.25)",
    dcFill: "rgba(33,150,243,0.05)",
    ema21: "#ff9800", sma100: "#e91e63", sma200: "#2962ff",
    rsi: "#7e57c2", rsiMa: "#fdd835", rsiGuide: "#787b86",
    rsiFill: "rgba(126,87,194,0.10)",
    rsiHot: "76,175,80", rsiCold: "242,54,69",          // gradient bases >70 / <30
    // VRVP: cyan up / pink down, Value-Area rows bright, the rest faint
    profUp: "rgba(38,198,218,0.25)", profDn: "rgba(236,64,122,0.25)",
    profUpVA: "rgba(38,198,218,0.70)", profDnVA: "rgba(236,64,122,0.70)",
    eth: "rgba(251,140,0,0.045)",                        // stocks: warm pre/post
    ethFut: "rgba(41,98,255,0.05)",                      // futures: cool overnight
  };

  /* ------------------------------------------------------------ studies ---- */
  /* All study math runs on RAW bars — Heikin Ashi is display-only. */

  const sma = (bars, n) => bars.map((b, i) => {
    if (i < n - 1) return null;
    let s = 0;
    for (let j = i - n + 1; j <= i; j++) s += bars[j].c;
    return { time: b.t, value: s / n };
  }).filter(Boolean);

  function ema(bars, n) {
    if (bars.length < n) return [];
    const k = 2 / (n + 1);
    let e = bars.slice(0, n).reduce((s, b) => s + b.c, 0) / n;
    const out = [{ time: bars[n - 1].t, value: e }];
    for (let i = n; i < bars.length; i++) {
      e = bars[i].c * k + e * (1 - k);
      out.push({ time: bars[i].t, value: e });
    }
    return out;
  }

  function donchian(bars, n) {
    const up = [], lo = [];
    for (let i = n - 1; i < bars.length; i++) {
      let h = -Infinity, l = Infinity;
      for (let j = i - n + 1; j <= i; j++) { h = Math.max(h, bars[j].h); l = Math.min(l, bars[j].l); }
      up.push({ time: bars[i].t, value: h });
      lo.push({ time: bars[i].t, value: l });
    }
    return { up, lo };
  }

  function vwapBands(bars) {
    // TV's "VWAP Auto Anchored", constrained to TODAY: cumulative hlc3 VWAP +
    // volume-weighted σ bands from the anchor forward. The anchor is the most
    // recent pivot high/low (14 bars each side) WITHIN today's session, else
    // today's first bar — intraday VWAP always starts fresh with the day,
    // never dragging yesterday's tape into today's read.
    const L = 14, n = bars.length;
    const v = [], u1 = [], d1 = [], u2 = [], d2 = [];
    if (!n) return { v, u1, d1, u2, d2 };
    const nyDay = (t) => Math.floor((t - 4 * 3600) / 86400);   // same NY trading day as the server
    const today = nyDay(bars[n - 1].t);
    let sess = n - 1;
    while (sess > 0 && nyDay(bars[sess - 1].t) === today) sess--;
    let anchor = sess;
    for (let i = n - 1 - L; i >= Math.max(L, sess); i--) {
      let hi = true, lo = true;
      for (let j = i - L; j <= i + L && (hi || lo); j++) {
        if (j === i) continue;
        // ties on the LEFT don't disqualify — the most RECENT swing of a
        // flat cluster anchors, matching how the TV script picks its pivot
        if (j < i ? bars[j].h > bars[i].h : bars[j].h >= bars[i].h) hi = false;
        if (j < i ? bars[j].l < bars[i].l : bars[j].l <= bars[i].l) lo = false;
      }
      if (hi || lo) { anchor = i; break; }
    }
    let pv = 0, vol = 0, m2 = 0;
    for (let i = anchor; i < n; i++) {
      const b = bars[i];
      const tp = (b.h + b.l + b.c) / 3, w = b.v || 1;
      pv += tp * w; vol += w;
      const vw = pv / vol;
      m2 += w * (tp - vw) * (tp - vw);
      const sd = Math.sqrt(m2 / vol);
      v.push({ time: b.t, value: vw });
      u1.push({ time: b.t, value: vw + sd }); d1.push({ time: b.t, value: vw - sd });
      u2.push({ time: b.t, value: vw + 2 * sd }); d2.push({ time: b.t, value: vw - 2 * sd });
    }
    return { v, u1, d1, u2, d2 };
  }

  function rsiWilder(bars, n) {
    // Wilder smoothing on raw closes.
    if (bars.length < n + 1) return [];
    let g = 0, l = 0;
    for (let i = 1; i <= n; i++) {
      const d = bars[i].c - bars[i - 1].c;
      if (d >= 0) g += d; else l -= d;
    }
    g /= n; l /= n;
    const val = () => (l === 0 ? 100 : 100 - 100 / (1 + g / l));
    const out = [{ time: bars[n].t, value: val() }];
    for (let i = n + 1; i < bars.length; i++) {
      const d = bars[i].c - bars[i - 1].c;
      g = (g * (n - 1) + Math.max(d, 0)) / n;
      l = (l * (n - 1) + Math.max(-d, 0)) / n;
      out.push({ time: bars[i].t, value: val() });
    }
    return out;
  }

  const smaOfLine = (pts, n) => pts.map((p, i) => {
    if (i < n - 1) return null;
    let s = 0;
    for (let j = i - n + 1; j <= i; j++) s += pts[j].value;
    return { time: p.time, value: s / n };
  }).filter(Boolean);

  /* -------------------------------------------------------- heikin ashi ---- */

  function haNext(prev, b) {
    const c = (b.o + b.h + b.l + b.c) / 4;
    const o = prev ? (prev.o + prev.c) / 2 : (b.o + b.c) / 2;
    return { t: b.t, o, h: Math.max(b.h, o, c), l: Math.min(b.l, o, c), c };
  }

  /* NY session clock for the extended-hours tint (RTH = 09:30–16:00 ET) */
  const _etFmt = new Intl.DateTimeFormat("en-US",
    { timeZone: "America/New_York", hour12: false, hourCycle: "h23",
      weekday: "short", hour: "2-digit", minute: "2-digit" });

  function isEth(tSec) {
    const parts = _etFmt.formatToParts(new Date(tSec * 1000));
    const get = (t) => (parts.find((p) => p.type === t) || {}).value;
    const wd = get("weekday");
    if (wd === "Sat" || wd === "Sun") return true;
    const hm = parseInt(get("hour"), 10) * 60 + parseInt(get("minute"), 10);
    return hm < 9 * 60 + 30 || hm >= 16 * 60;
  }

  /* session-tint flavor per instrument: futures nights are cool, stock
     pre/post is warm, crypto never sleeps so it never tints */
  function tintFor(sym) {
    const s = (sym || "").toUpperCase();
    if (/^(BTC|ETH|SOL|DOGE)/.test(s)) return null;
    if (/1!$|=F$/.test(s)) return "fut";
    return "eq";
  }

  function computeHA(bars) {
    const out = [];
    for (const b of bars) out.push(haNext(out[out.length - 1] || null, b));
    return out;
  }

  /* ------------------------------------------------------ volume profile ---- */

  function computeProfile(bars, rowsN) {
    if (!bars.length) return null;
    let lo = Infinity, hi = -Infinity;
    for (const b of bars) { lo = Math.min(lo, b.l); hi = Math.max(hi, b.h); }
    if (!(hi > lo)) return null;
    const N = rowsN || 55, step = (hi - lo) / N;
    const rows = Array.from({ length: N }, (_, i) =>
      ({ p0: lo + i * step, p1: lo + (i + 1) * step, up: 0, dn: 0 }));
    const clamp = (i) => Math.max(0, Math.min(N - 1, i));
    for (const b of bars) {
      const vol = b.v || 0;
      if (!vol) continue;
      const up = b.c >= b.o;
      if (b.h <= b.l) {   // degenerate bar — all volume to its close row
        const r = rows[clamp(Math.floor((b.c - lo) / step))];
        if (up) r.up += vol; else r.dn += vol;
        continue;
      }
      const range = b.h - b.l;
      const i0 = clamp(Math.floor((b.l - lo) / step));
      const i1 = clamp(Math.floor((b.h - lo) / step));
      for (let i = i0; i <= i1; i++) {
        const overlap = Math.min(b.h, rows[i].p1) - Math.max(b.l, rows[i].p0);
        if (overlap <= 0) continue;
        const share = vol * (overlap / range);   // split by row overlap / bar range
        if (up) rows[i].up += share; else rows[i].dn += share;
      }
    }
    let max = 0, poc = 0, total = 0;
    for (let i = 0; i < N; i++) {
      const t = rows[i].up + rows[i].dn;
      total += t;
      if (t > max) { max = t; poc = i; }
    }
    if (max <= 0) return null;
    // Value Area: expand from the POC until 70% of traded volume is inside —
    // VA rows render bright, the rest faint (the house VRVP look)
    let covered = rows[poc].up + rows[poc].dn, a = poc, b = poc;
    while (covered < 0.7 * total && (a > 0 || b < N - 1)) {
      const upNext = b < N - 1 ? rows[b + 1].up + rows[b + 1].dn : -1;
      const dnNext = a > 0 ? rows[a - 1].up + rows[a - 1].dn : -1;
      if (upNext >= dnNext) { b++; covered += upNext; }
      else { a--; covered += dnNext; }
    }
    for (let i = a; i <= b; i++) rows[i].inVA = true;
    return { rows, max, poc };
  }

  /* -------------------------------------------------------------- chart ---- */

  const LC = () => window.LightweightCharts;

  function create(container, opts = {}) {
    const state = {
      mode: opts.mode === "mini" ? "mini" : "full",
      intervalSec: opts.intervalSec || 300,
      tz: opts.timezone || "Europe/Dublin",
      heikin: opts.heikin !== false,
      daily: false,
      bars: [],            // RAW UTC bars — single source of truth
      ha: [],              // derived Heikin Ashi, kept in lockstep
      dispTimes: [],       // display (Dublin-shifted) times, parallel to bars
      dispByRaw: new Map(),
      priceLines: [],
      markers: [],
      markersApi: null,
      profile: null,
      tickCount: 0,
      lastStudyPaint: 0,
      tzCache: new Map(),
      symbol: opts.symbol || "",
      ivLabel: opts.label || "",
    };
    const full = state.mode === "full";

    // VRVP is a VISIBLE-RANGE profile (like TV): it recomputes over the bars
    // on screen, not the whole loaded history — pan to premarket and it shows
    // premarket; sit on today and it shows TODAY.
    let profileReq = null;      // the primitive's requestUpdate, set on attach
    let profileTimer = null;

    function visibleBars() {
      try {
        const lr = chart.timeScale().getVisibleLogicalRange();
        if (!lr) return state.bars;
        const a = Math.max(0, Math.floor(lr.from));
        const b = Math.min(state.bars.length, Math.ceil(lr.to) + 1);
        const s = state.bars.slice(a, b);
        return s.length >= 10 ? s : state.bars;
      } catch { return state.bars; }
    }

    function recomputeProfile() {
      if (!full) return;
      state.profile = computeProfile(visibleBars(),
        Math.min(300, Math.max(60, Math.floor((container.clientHeight || 420) / 2))));
      if (profileReq) profileReq();
    }

    // lightweight-charts renders epochs as UTC — display-shift every time we
    // hand it by the Dublin UTC-offset at that timestamp (DST-safe, cached
    // per 6h bucket). Internal state stays RAW UTC.
    function tzShift(ts) {
      const key = Math.floor(ts / 21600);
      let off = state.tzCache.get(key);
      if (off === undefined) {
        const d = new Date(ts * 1000);
        off = (new Date(d.toLocaleString("en-US", { timeZone: state.tz })) -
               new Date(d.toLocaleString("en-US", { timeZone: "UTC" }))) / 1000;
        state.tzCache.set(key, off);
      }
      return ts + off;
    }

    function rebuildDispTimes() {
      // strictly ascending even across a DST fall-back (clamp forward 1s)
      const n = state.bars.length;
      state.dispTimes = new Array(n);
      state.dispByRaw = new Map();
      let prev = -Infinity;
      for (let i = 0; i < n; i++) {
        let t = tzShift(state.bars[i].t);
        if (t <= prev) t = prev + 1;
        state.dispTimes[i] = t;
        state.dispByRaw.set(state.bars[i].t, t);
        prev = t;
      }
    }
    const dispOf = (rawT) => {
      const t = state.dispByRaw.get(rawT);
      return t !== undefined ? t : tzShift(rawT);
    };

    const p = palette();
    const chart = LC().createChart(container, {
      autoSize: true,
      layout: { background: { type: "solid", color: FIXED.bg },
                textColor: FIXED.text, fontFamily: p.mono, fontSize: 11 },
      grid: { vertLines: { color: FIXED.grid }, horzLines: { color: FIXED.grid } },
      rightPriceScale: { borderVisible: true, borderColor: FIXED.scaleBorder },
      timeScale: { borderVisible: true, borderColor: FIXED.scaleBorder,
                   timeVisible: state.intervalSec < 86400,
                   secondsVisible: false, rightOffset: 5 },
      crosshair: {
        mode: 0,
        vertLine: { color: FIXED.crosshair, style: 3, labelBackgroundColor: FIXED.scaleBorder },
        horzLine: { color: FIXED.crosshair, style: 3, labelBackgroundColor: FIXED.scaleBorder },
      },
    });

    const candles = chart.addSeries(LC().CandlestickSeries, {
      upColor: FIXED.up, downColor: FIXED.down,
      borderVisible: true, borderUpColor: FIXED.up, borderDownColor: FIXED.down,
      wickUpColor: FIXED.up, wickDownColor: FIXED.down,
    }, 0);
    const volume = chart.addSeries(LC().HistogramSeries,
      { priceScaleId: "", priceFormat: { type: "volume" },
        lastValueVisible: false, priceLineVisible: false }, 0);
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
    const volMa = chart.addSeries(LC().LineSeries,
      { priceScaleId: "", color: FIXED.volMa, lineWidth: 1,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false }, 0);

    const lines = {};
    let rsiGuides = [];
    if (full) {
      const mk = (o, pane = 0) => chart.addSeries(LC().LineSeries,
        { lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false, ...o }, pane);
      lines.vwap = mk({ color: FIXED.vwap, title: "vwap" });
      lines.u1 = mk({ color: FIXED.bandUp });
      lines.d1 = mk({ color: FIXED.bandDn });
      lines.u2 = mk({ color: FIXED.bandUp, lineStyle: 2 });
      lines.d2 = mk({ color: FIXED.bandDn, lineStyle: 2 });
      lines.ema21 = mk({ color: FIXED.ema21, lineWidth: 2, title: "ema21" });
      lines.sma100 = mk({ color: FIXED.sma100, lineWidth: 2, title: "sma100" });
      lines.sma200 = mk({ color: FIXED.sma200, lineWidth: 2, title: "sma200" });
      lines.dcU = mk({ color: FIXED.dcU });
      lines.dcL = mk({ color: FIXED.dcL });

      // Two backdrop layers, both extracted from the house layout:
      //  1. extended-hours session tint (pre/post bars get a warm wash)
      //  2. the Donchian channel's blue 5% fill — the navy wash TV shows
      candles.attachPrimitive({
        updateAllViews() {},
        paneViews() {
          return [{
            zOrder() { return "bottom"; },
            renderer() {
              return {
                draw(target) {
                  target.useBitmapCoordinateSpace(
                    ({ context: ctx, bitmapSize, horizontalPixelRatio: hpr, verticalPixelRatio: vpr }) => {
                      const ts = chart.timeScale();
                      for (const rg of state.ethRanges || []) {
                        const x0 = ts.timeToCoordinate(rg.t0);
                        const x1 = ts.timeToCoordinate(rg.t1);
                        if (x0 === null && x1 === null) continue;
                        const pad = rg.halfStepPx || 3;
                        const a = ((x0 === null ? 0 : x0) - pad) * hpr;
                        const b = ((x1 === null ? bitmapSize.width / hpr : x1) + pad) * hpr;
                        ctx.fillStyle = state.ethColor || FIXED.eth;
                        ctx.fillRect(a, 0, Math.max(1, b - a), bitmapSize.height);
                      }
                      const poly = (top, bot, fill) => {
                        if (!top || !bot || !top.length) return;
                        ctx.beginPath();
                        let started = false;
                        for (const pt of top) {
                          const x = ts.timeToCoordinate(pt.time);
                          const y = candles.priceToCoordinate(pt.value);
                          if (x === null || y === null) continue;
                          if (!started) { ctx.moveTo(x * hpr, y * vpr); started = true; }
                          else ctx.lineTo(x * hpr, y * vpr);
                        }
                        for (let i = bot.length - 1; i >= 0; i--) {
                          const x = ts.timeToCoordinate(bot[i].time);
                          const y = candles.priceToCoordinate(bot[i].value);
                          if (x === null || y === null) continue;
                          ctx.lineTo(x * hpr, y * vpr);
                        }
                        if (!started) return;
                        ctx.closePath();
                        ctx.fillStyle = fill;
                        ctx.fill();
                      };
                      const dp = state.dcPts;
                      if (dp) poly(dp.up, dp.lo, FIXED.dcFill);
                      const vp = state.vwapPts;
                      if (vp) {
                        poly(vp.u2, vp.u1, FIXED.vwapFillU2);
                        poly(vp.u1, vp.v, FIXED.vwapFillU1);
                        poly(vp.v, vp.d1, FIXED.vwapFillD1);
                        poly(vp.d1, vp.d2, FIXED.vwapFillD2);
                      }
                    });
                },
              };
            },
          }];
        },
      });

      // RSI pane (index 1, ~24% height) — TV look: purple RSI, yellow MA,
      // dotted 30/70 guides with the translucent purple band between them
      lines.rsi = mk({ color: FIXED.rsi, title: "rsi14" }, 1);
      lines.rsiMa = mk({ color: FIXED.rsiMa, lineWidth: 1 }, 1);
      rsiGuides = [30, 70].map((price) => lines.rsi.createPriceLine({
        price, lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: "",
        color: FIXED.rsiGuide,
      }));
      lines.rsi.attachPrimitive({
        updateAllViews() {},
        paneViews() {
          return [{
            zOrder() { return "bottom"; },
            renderer() {
              return {
                draw(target) {
                  target.useBitmapCoordinateSpace(
                    ({ context: ctx, bitmapSize, horizontalPixelRatio: hpr, verticalPixelRatio: vpr }) => {
                      const y70 = lines.rsi.priceToCoordinate(70);
                      const y30 = lines.rsi.priceToCoordinate(30);
                      if (y70 === null || y30 === null) return;
                      ctx.fillStyle = FIXED.rsiFill;
                      ctx.fillRect(0, Math.min(y70, y30) * vpr,
                                   bitmapSize.width, Math.abs(y30 - y70) * vpr);
                      // the house RSI: gradient heat above 70 / below 30
                      const pts = state.rsiPts;
                      if (!pts || !pts.length) return;
                      const ts = chart.timeScale();
                      const heat = (limitY, dir, rgb, yFar) => {
                        ctx.beginPath();
                        let firstX = null, lastX = null;
                        for (const pt of pts) {
                          const x = ts.timeToCoordinate(pt.time);
                          const yy = lines.rsi.priceToCoordinate(pt.value);
                          if (x === null || yy === null) continue;
                          const yc = dir < 0 ? Math.min(yy, limitY) : Math.max(yy, limitY);
                          if (firstX === null) { firstX = x; ctx.moveTo(x * hpr, yc * vpr); }
                          else ctx.lineTo(x * hpr, yc * vpr);
                          lastX = x;
                        }
                        if (firstX === null) return;
                        ctx.lineTo(lastX * hpr, limitY * vpr);
                        ctx.lineTo(firstX * hpr, limitY * vpr);
                        ctx.closePath();
                        const g = ctx.createLinearGradient(0, yFar * vpr, 0, limitY * vpr);
                        g.addColorStop(0, `rgba(${rgb},0.9)`);
                        g.addColorStop(1, `rgba(${rgb},0)`);
                        ctx.fillStyle = g;
                        ctx.fill();
                      };
                      const y100 = lines.rsi.priceToCoordinate(100) ?? 0;
                      const y0 = lines.rsi.priceToCoordinate(0) ?? bitmapSize.height / vpr;
                      heat(y70, -1, FIXED.rsiHot, y100);
                      heat(y30, +1, FIXED.rsiCold, y0);
                    });
                },
              };
            },
          }];
        },
      });
      try {
        chart.panes()[1].setHeight(
          Math.max(70, Math.round((container.clientHeight || 420) * 0.24)));
      } catch { /* pane sizing is cosmetic */ }

      // VRVP — drawn right-edge-anchored by a series primitive (TV style):
      // up volume from the right edge leftward, down volume stacked to its
      // left, Value-Area rows bright. VISIBLE-RANGE: recomputed on pan/zoom.
      candles.attachPrimitive({
        attached(p) { profileReq = p.requestUpdate; },
        detached() { profileReq = null; },
        updateAllViews() {},
        paneViews() {
          return [{
            zOrder() { return "bottom"; },
            renderer() {
              return {
                draw(target) {
                  const prof = state.profile;
                  if (!prof) return;
                  target.useBitmapCoordinateSpace(
                    ({ context: ctx, bitmapSize, verticalPixelRatio: vpr }) => {
                      const right = bitmapSize.width;
                      const maxW = right * 0.30;   // his VRVP: percentWidth 30
                      for (let i = 0; i < prof.rows.length; i++) {
                        const r = prof.rows[i];
                        if (r.up + r.dn <= 0) continue;
                        const yA = candles.priceToCoordinate(r.p1);   // media space
                        const yB = candles.priceToCoordinate(r.p0);
                        if (yA === null || yB === null) continue;
                        const y = Math.min(yA, yB) * vpr;
                        const h = Math.max(1, Math.abs(yB - yA) * vpr - 1);
                        const bright = !!r.inVA;
                        const upW = (r.up / prof.max) * maxW;
                        const dnW = (r.dn / prof.max) * maxW;
                        ctx.fillStyle = bright ? FIXED.profUpVA : FIXED.profUp;
                        ctx.fillRect(right - upW, y, upW, h);
                        ctx.fillStyle = bright ? FIXED.profDnVA : FIXED.profDn;
                        ctx.fillRect(right - upW - dnW, y, dnW, h);
                      }
                    });
                },
              };
            },
          }];
        },
      });

      // pan/zoom re-anchors the profile to what's on screen (debounced)
      chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
        clearTimeout(profileTimer);
        profileTimer = setTimeout(recomputeProfile, 160);
      });
    }

    // The chart is DE-THEMED by design: every color above is a fixed TV-clone
    // identity color, so site themes restyle the chrome around the chart, never
    // the instrument itself. (rsiGuides kept for future style tweaks.)
    void rsiGuides;

    /* TV-style legend: symbol · interval, then O H L C and change, colored by
       the bar's direction; follows the crosshair, falls back to the last bar. */
    let legend = null;
    if (full) {
      container.style.position = "relative";
      legend = document.createElement("div");
      Object.assign(legend.style, {
        position: "absolute", top: "6px", left: "10px", zIndex: 3,
        pointerEvents: "none", whiteSpace: "nowrap",
        font: "11.5px " + p.mono, color: FIXED.text,
      });
      container.appendChild(legend);
      chart.subscribeCrosshairMove((param) =>
        paintLegend(param && param.time !== undefined ? param.time : null));
    }

    const fmtPx = (x) => (x >= 1000 ? x.toFixed(1) : x.toFixed(2));

    function paintLegend(dispTime) {
      if (!legend || !state.bars.length) return;
      let i = state.bars.length - 1;
      if (dispTime != null) {
        const idx = state.dispTimes.indexOf(dispTime);
        if (idx >= 0) i = idx;
      }
      const b = state.bars[i];                       // raw OHLC, like TV's readout
      const prevC = i > 0 ? state.bars[i - 1].c : b.o;
      const chg = b.c - prevC;
      const pct = prevC ? (chg / prevC) * 100 : 0;
      const col = b.c >= b.o ? FIXED.up : FIXED.down;
      const chgCol = chg >= 0 ? FIXED.up : FIXED.down;
      const v = (n) => `<span style="color:${col}">${fmtPx(n)}</span>`;
      legend.innerHTML =
        `<span style="color:#d1d4dc;font-weight:600">${state.symbol || ""}</span>` +
        `<span style="color:${FIXED.text}"> · ${state.ivLabel || ""}</span>&nbsp;&nbsp;` +
        `O ${v(b.o)} H ${v(b.h)} L ${v(b.l)} C ${v(b.c)} ` +
        `<span style="color:${chgCol}">${chg >= 0 ? "+" : ""}${fmtPx(chg)} ` +
        `(${chg >= 0 ? "+" : ""}${pct.toFixed(2)}%)</span>`;
    }

    function displayBar(i) {
      const b = state.heikin ? state.ha[i] : state.bars[i];
      return { time: state.dispTimes[i], open: b.o, high: b.h, low: b.l, close: b.c };
    }

    function repaintCandles() {
      candles.setData(state.bars.map((_, i) => displayBar(i)));
    }

    function repaintVolume() {
      volume.setData(state.bars.map((b, i) => ({
        time: state.dispTimes[i], value: b.v,
        color: b.c >= b.o ? FIXED.volUp : FIXED.volDown,
      })));
      // TV's white Vol MA(30) over the histogram
      const pts = [];
      let s = 0;
      for (let i = 0; i < state.bars.length; i++) {
        s += state.bars[i].v || 0;
        if (i >= 30) s -= state.bars[i - 30].v || 0;
        if (i >= 29) pts.push({ time: state.dispTimes[i], value: s / 30 });
      }
      volMa.setData(pts);
    }

    const shiftPts = (pts) => pts.map((q) => ({ time: dispOf(q.time), value: q.value }));

    function paintStudies() {
      if (!full || !state.bars.length) return;
      const intraday = !state.daily && state.intervalSec < 86400;
      // house rule: the moving averages live on the 1D chart ONLY — intraday
      // gets VWAP+σ bands instead, and weekly gets neither
      const dailyExactly = state.intervalSec === 86400;
      if (intraday) {
        const vw = vwapBands(state.bars);
        lines.vwap.setData(shiftPts(vw.v));
        lines.u1.setData(shiftPts(vw.u1)); lines.d1.setData(shiftPts(vw.d1));
        lines.u2.setData(shiftPts(vw.u2)); lines.d2.setData(shiftPts(vw.d2));
        state.vwapPts = { v: shiftPts(vw.v),
                          u1: shiftPts(vw.u1), d1: shiftPts(vw.d1),
                          u2: shiftPts(vw.u2), d2: shiftPts(vw.d2) };
        for (const k of ["ema21", "sma100", "sma200"]) lines[k].setData([]);
      } else {
        for (const k of ["vwap", "u1", "d1", "u2", "d2"]) lines[k].setData([]);
        state.vwapPts = null;
        if (dailyExactly) {
          lines.ema21.setData(shiftPts(ema(state.bars, 21)));
          lines.sma100.setData(shiftPts(sma(state.bars, 100)));
          lines.sma200.setData(shiftPts(sma(state.bars, 200)));
        } else {
          for (const k of ["ema21", "sma100", "sma200"]) lines[k].setData([]);
        }
      }
      const dc = donchian(state.bars, 96);
      lines.dcU.setData(shiftPts(dc.up)); lines.dcL.setData(shiftPts(dc.lo));
      state.dcPts = { up: shiftPts(dc.up), lo: shiftPts(dc.lo) };
      const r = rsiWilder(state.bars, 14);
      lines.rsi.setData(shiftPts(r));
      lines.rsiMa.setData(shiftPts(smaOfLine(r, 14)));
      state.rsiPts = shiftPts(r);

      // extended-hours ranges (intraday): consecutive pre/post bars, in
      // display time, for the backdrop tint
      state.ethRanges = null;
      const tint = tintFor(state.symbol);
      state.ethColor = tint === "fut" ? FIXED.ethFut : FIXED.eth;
      if (intraday && tint) {
        const rgs = [];
        let cur = null;
        for (let i = 0; i < state.bars.length; i++) {
          if (!isEth(state.bars[i].t)) { cur = null; continue; }
          if (cur) { cur.i1 = i; }
          else { cur = { i0: i, i1: i }; rgs.push(cur); }
        }
        state.ethRanges = rgs.map((g) => ({
          t0: state.dispTimes[g.i0], t1: state.dispTimes[g.i1],
        }));
      }
    }

    function setData(bars, intervalSec, extra) {
      if (intervalSec) state.intervalSec = intervalSec;
      state.daily = !!(extra && extra.daily) || state.intervalSec >= 86400;
      const symChanged = !!(extra && extra.symbol && extra.symbol !== state.symbol);
      if (extra && extra.symbol) state.symbol = extra.symbol;
      if (extra && extra.label) state.ivLabel = extra.label;
      state.bars = (bars || []).map((b) => ({ ...b }));   // own copies; ticks mutate
      state.lastTickTs = 0;                               // fresh tick baseline
      state.ha = computeHA(state.bars);
      rebuildDispTimes();
      chart.applyOptions({ timeScale: {
        timeVisible: !state.daily && state.intervalSec < 86400 } });
      repaintCandles();
      repaintVolume();
      paintStudies();
      recomputeProfile();
      applyMarkers();
      paintLegend(null);
      if (symChanged) {
        // new instrument, new price regime (SPY ~745 vs ES ~7500): snap the
        // scales back to auto so the chart refocuses instead of squashing
        try {
          chart.priceScale("right").applyOptions({ autoScale: true });
          chart.timeScale().resetTimeScale();
          chart.timeScale().scrollToRealTime();
        } catch { /* cosmetic */ }
      }
    }

    function applyTick(price, tsSec, symbol) {
      if (!state.bars.length) return;
      // mid symbol-switch the quote stream races the bar fetch: a NOW print
      // must never land on SPY's candle (paints a monster red bar)
      if (symbol && state.symbol && symbol !== state.symbol) return;
      const step = state.intervalSec;
      let last = state.bars[state.bars.length - 1];
      // un-timestamped or future-stamped prints can't be ordered: fold them
      // into the live candle without advancing the ordering gate
      const ordered = tsSec > 0 && tsSec <= Math.floor(Date.now() / 1000) + 300;
      if (ordered) {
        if (state.lastTickTs && tsSec < state.lastTickTs) return;  // out-of-order print
        state.lastTickTs = tsSec;
      }
      // the server may append the freshest quote as a partial bar stamped
      // mid-bucket — order by bucket, not raw t, or live ticks get dropped
      const lastBucket = last.t - (last.t % step);
      const bucket = ordered ? tsSec - (tsSec % step) : lastBucket;
      if (bucket > lastBucket) {
        last = { t: bucket, o: price, h: price, l: price, c: price, v: 0 };
        state.bars.push(last);
        state.ha.push(haNext(state.ha[state.ha.length - 1] || null, last));
        const prevT = state.dispTimes[state.dispTimes.length - 1] ?? -Infinity;
        const t = Math.max(tzShift(bucket), prevT + 1);
        state.dispTimes.push(t);
        state.dispByRaw.set(bucket, t);
      } else if (bucket === lastBucket || state.daily) {
        // mutate the raw last bar, re-derive just the last HA candle
        // (daily merges even when bucket lands earlier: providers stamp the
        // session bar at 04:00Z/13:30Z while ticks bucket to midnight UTC)
        last.c = price;
        last.h = Math.max(last.h, price);
        last.l = Math.min(last.l, price);
        const prev = state.ha.length > 1 ? state.ha[state.ha.length - 2] : null;
        state.ha[state.ha.length - 1] = haNext(prev, last);
      } else {
        return;   // a print from an older candle never smears the live one
      }
      candles.update(displayBar(state.bars.length - 1));
      state.tickCount++;
      if (full && state.tickCount % 50 === 0) recomputeProfile();
      const now = Date.now();
      if (now - state.lastStudyPaint > 3000) {   // studies + RSI follow, gently
        state.lastStudyPaint = now;
        paintStudies();
        repaintVolume();
      }
      paintLegend(null);
    }

    function setHeikin(on) {
      on = !!on;
      if (on === state.heikin) return;
      state.heikin = on;
      if (state.bars.length) repaintCandles();
    }

    function setLevels(levels) {
      for (const l of state.priceLines) candles.removePriceLine(l);
      state.priceLines = (levels || []).filter((l) => l && l.price).map((l) =>
        candles.createPriceLine({
          price: l.price, color: l.color || palette().dim,
          lineWidth: 1, lineStyle: l.style ?? 2, axisLabelVisible: true,
          title: l.title || "",
        }));
    }

    function applyMarkers() {
      // markers only render on bars the chart actually loaded — a checklist
      // time outside the visible history must not invent a phantom position
      const shifted = state.markers
        .map((m) => ({ ...m, time: state.dispByRaw.get(m.time) }))
        .filter((m) => m.time !== undefined);
      if (!state.markersApi) {
        if (!shifted.length) return;
        state.markersApi = LC().createSeriesMarkers(candles, shifted);
      } else {
        state.markersApi.setMarkers(shifted);
      }
    }

    function setMarkers(markers) {
      // snap marker times onto bar buckets so they always render
      const step = state.intervalSec;
      state.markers = (markers || []).map((m) => ({ ...m, time: m.time - (m.time % step) }))
        .sort((a, b) => a.time - b.time);
      applyMarkers();
    }

    return {
      chart, setData, applyTick, setLevels, setMarkers, setHeikin,
      setIntervalSec: (s) => { state.intervalSec = s; },
      lastBar: () => (state.bars.length ? { ...state.bars[state.bars.length - 1] } : null),
      symbol: () => state.symbol,
      destroy() {
        if (legend) legend.remove();
        chart.remove();
      },
    };
  }

  window.DeskChart = { create, palette };
})();
