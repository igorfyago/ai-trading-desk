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

  // Identity colors — deliberately NOT themed (they match the house TV layout).
  const FIXED = {
    ema21: "#ff9800", sma100: "#e91e63", sma200: "#2962ff",
    rsi: "#b39ddb", rsiMa: "#f0c000",
    profUp: "rgba(38,166,154,.55)", profDn: "rgba(239,83,80,.50)",
    profUpPoc: "rgba(38,166,154,.85)", profDnPoc: "rgba(239,83,80,.85)",
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
    // Session-anchored VWAP + volume-weighted σ bands. Session = NY trading
    // day (UTC-4 approximation is fine for an index chart).
    const v = [], u1 = [], d1 = [], u2 = [], d2 = [];
    let day = null, pv = 0, vol = 0, m2 = 0;
    for (const b of bars) {
      const d = Math.floor((b.t - 4 * 3600) / 86400);
      if (d !== day) { day = d; pv = 0; vol = 0; m2 = 0; }
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
    let max = 0, poc = 0;
    for (let i = 0; i < N; i++) {
      const t = rows[i].up + rows[i].dn;
      if (t > max) { max = t; poc = i; }
    }
    return max > 0 ? { rows, max, poc } : null;
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
    };
    const full = state.mode === "full";

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
      layout: { background: { color: "transparent" }, textColor: p.dim,
                fontFamily: p.mono, fontSize: 11 },
      grid: { vertLines: { color: alpha("#888888", 0.06) },
              horzLines: { color: alpha("#888888", 0.06) } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: state.intervalSec < 86400,
                   secondsVisible: false, rightOffset: 5 },
      crosshair: { mode: 0 },
    });

    const candles = chart.addSeries(LC().CandlestickSeries, {}, 0);
    const volume = chart.addSeries(LC().HistogramSeries,
      { priceScaleId: "", priceFormat: { type: "volume" },
        lastValueVisible: false, priceLineVisible: false }, 0);
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });

    const lines = {};
    let rsiGuides = [];
    if (full) {
      const mk = (o, pane = 0) => chart.addSeries(LC().LineSeries,
        { lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false, ...o }, pane);
      lines.vwap = mk({ lineWidth: 2, title: "vwap" });
      lines.u1 = mk({ lineStyle: 2 }); lines.d1 = mk({ lineStyle: 2 });
      lines.u2 = mk({ lineStyle: 2 }); lines.d2 = mk({ lineStyle: 2 });
      lines.ema21 = mk({ color: FIXED.ema21, lineWidth: 1.5, title: "ema21" });
      lines.sma100 = mk({ color: FIXED.sma100, lineWidth: 1.5, title: "sma100" });
      lines.sma200 = mk({ color: FIXED.sma200, lineWidth: 1.5, title: "sma200" });
      lines.dcU = mk({ lineStyle: 1 }); lines.dcL = mk({ lineStyle: 1 });

      // RSI pane (index 1, ~24% height)
      lines.rsi = mk({ color: FIXED.rsi, lineWidth: 1.5, title: "rsi14" }, 1);
      lines.rsiMa = mk({ color: FIXED.rsiMa, lineWidth: 1 }, 1);
      rsiGuides = [30, 70].map((price) => lines.rsi.createPriceLine({
        price, lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: "",
        color: alpha(palette().dim, 0.6),
      }));
      try {
        chart.panes()[1].setHeight(
          Math.max(70, Math.round((container.clientHeight || 420) * 0.24)));
      } catch { /* pane sizing is cosmetic */ }

      // VRVP — drawn right-edge-anchored by a series primitive (TV style):
      // up volume from the right edge leftward, down volume stacked to its
      // left, POC row brighter.
      candles.attachPrimitive({
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
                      const maxW = right * 0.26;
                      for (let i = 0; i < prof.rows.length; i++) {
                        const r = prof.rows[i];
                        if (r.up + r.dn <= 0) continue;
                        const yA = candles.priceToCoordinate(r.p1);   // media space
                        const yB = candles.priceToCoordinate(r.p0);
                        if (yA === null || yB === null) continue;
                        const y = Math.min(yA, yB) * vpr;
                        const h = Math.max(1, Math.abs(yB - yA) * vpr - 1);
                        const poc = i === prof.poc;
                        const upW = (r.up / prof.max) * maxW;
                        const dnW = (r.dn / prof.max) * maxW;
                        ctx.fillStyle = poc ? FIXED.profUpPoc : FIXED.profUp;
                        ctx.fillRect(right - upW, y, upW, h);
                        ctx.fillStyle = poc ? FIXED.profDnPoc : FIXED.profDn;
                        ctx.fillRect(right - upW - dnW, y, dnW, h);
                      }
                    });
                },
              };
            },
          }];
        },
      });
    }

    function paintTheme() {
      const c = palette();
      chart.applyOptions({ layout: { textColor: c.dim, fontFamily: c.mono } });
      candles.applyOptions({
        upColor: c.green, downColor: c.red, borderVisible: false,
        wickUpColor: alpha(c.green, 0.7), wickDownColor: alpha(c.red, 0.7),
      });
      if (full) {
        lines.vwap.applyOptions({ color: c.accent });
        for (const k of ["u1", "d1"]) lines[k].applyOptions({ color: alpha(c.accent, 0.4) });
        for (const k of ["u2", "d2"]) lines[k].applyOptions({ color: alpha(c.accent, 0.22) });
        lines.dcU.applyOptions({ color: alpha(c.dim, 0.45) });
        lines.dcL.applyOptions({ color: alpha(c.dim, 0.45) });
        for (const g of rsiGuides) g.applyOptions({ color: alpha(c.dim, 0.6) });
        // ema21/sma100/sma200/rsi keep their fixed identity colors.
      }
      repaintVolume();
    }

    function displayBar(i) {
      const b = state.heikin ? state.ha[i] : state.bars[i];
      return { time: state.dispTimes[i], open: b.o, high: b.h, low: b.l, close: b.c };
    }

    function repaintCandles() {
      candles.setData(state.bars.map((_, i) => displayBar(i)));
    }

    function repaintVolume() {
      const c = palette();
      volume.setData(state.bars.map((b, i) => ({
        time: state.dispTimes[i], value: b.v,
        color: alpha(b.c >= b.o ? c.green : c.red, 0.35),
      })));
    }

    const shiftPts = (pts) => pts.map((q) => ({ time: dispOf(q.time), value: q.value }));

    function paintStudies() {
      if (!full || !state.bars.length) return;
      const intraday = !state.daily && state.intervalSec < 86400;
      if (intraday) {
        const vw = vwapBands(state.bars);
        lines.vwap.setData(shiftPts(vw.v));
        lines.u1.setData(shiftPts(vw.u1)); lines.d1.setData(shiftPts(vw.d1));
        lines.u2.setData(shiftPts(vw.u2)); lines.d2.setData(shiftPts(vw.d2));
        for (const k of ["ema21", "sma100", "sma200"]) lines[k].setData([]);
      } else {
        for (const k of ["vwap", "u1", "d1", "u2", "d2"]) lines[k].setData([]);
        lines.ema21.setData(shiftPts(ema(state.bars, 21)));
        lines.sma100.setData(shiftPts(sma(state.bars, 100)));
        lines.sma200.setData(shiftPts(sma(state.bars, 200)));
      }
      const dc = donchian(state.bars, 96);
      lines.dcU.setData(shiftPts(dc.up)); lines.dcL.setData(shiftPts(dc.lo));
      const r = rsiWilder(state.bars, 14);
      lines.rsi.setData(shiftPts(r));
      lines.rsiMa.setData(shiftPts(smaOfLine(r, 14)));
    }

    function setData(bars, intervalSec, extra) {
      if (intervalSec) state.intervalSec = intervalSec;
      state.daily = !!(extra && extra.daily) || state.intervalSec >= 86400;
      state.bars = (bars || []).map((b) => ({ ...b }));   // own copies; ticks mutate
      state.ha = computeHA(state.bars);
      rebuildDispTimes();
      chart.applyOptions({ timeScale: {
        timeVisible: !state.daily && state.intervalSec < 86400 } });
      repaintCandles();
      repaintVolume();
      paintStudies();
      if (full) state.profile = computeProfile(state.bars);
      applyMarkers();
    }

    function applyTick(price, tsSec) {
      if (!state.bars.length) return;
      const step = state.intervalSec;
      const bucket = tsSec - (tsSec % step);
      let last = state.bars[state.bars.length - 1];
      if (bucket > last.t) {
        last = { t: bucket, o: price, h: price, l: price, c: price, v: 0 };
        state.bars.push(last);
        state.ha.push(haNext(state.ha[state.ha.length - 1] || null, last));
        const prevT = state.dispTimes[state.dispTimes.length - 1] ?? -Infinity;
        const t = Math.max(tzShift(bucket), prevT + 1);
        state.dispTimes.push(t);
        state.dispByRaw.set(bucket, t);
      } else {
        // mutate the raw last bar, re-derive just the last HA candle
        last.c = price;
        last.h = Math.max(last.h, price);
        last.l = Math.min(last.l, price);
        const prev = state.ha.length > 1 ? state.ha[state.ha.length - 2] : null;
        state.ha[state.ha.length - 1] = haNext(prev, last);
      }
      candles.update(displayBar(state.bars.length - 1));
      state.tickCount++;
      if (full && state.tickCount % 50 === 0) state.profile = computeProfile(state.bars);
      const now = Date.now();
      if (now - state.lastStudyPaint > 3000) {   // studies + RSI follow, gently
        state.lastStudyPaint = now;
        paintStudies();
      }
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
      const shifted = state.markers.map((m) => ({ ...m, time: dispOf(m.time) }));
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

    const onTheme = () => paintTheme();
    window.addEventListener("themechange", onTheme);
    paintTheme();

    return {
      chart, setData, applyTick, setLevels, setMarkers, setHeikin,
      setIntervalSec: (s) => { state.intervalSec = s; },
      destroy() {
        window.removeEventListener("themechange", onTheme);
        chart.remove();
      },
    };
  }

  window.DeskChart = { create, palette };
})();
