/* DeskChart — the site's own chart engine.
   TradingView's open-source lightweight-charts (vendored, v5) + our own live
   feed + our own study math. One implementation drives BOTH the landing
   dashboard chart and the desk trade dock, so every surface reads the same
   candles the agents read.

   The desk's study stack, all computed here (the free TV embed couldn't do
   the bands): VWAP with 1σ/2σ bands, EMA21, SMA100, SMA200, DC96, volume.

   Theme: every color comes from the live CSS vars and repaints on the
   themes.js "themechange" event. The library's TradingView attribution logo
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

  /* ------------------------------------------------------------ studies ---- */

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

  /* -------------------------------------------------------------- chart ---- */

  const LC = () => window.LightweightCharts;
  const addSeries = (chart, kind, opts) =>
    chart.addSeries ? chart.addSeries(LC()[kind], opts)
                    : chart[`add${kind}`](opts);            // v4 fallback

  function create(container, opts = {}) {
    const state = {
      intervalSec: opts.intervalSec || 300,
      studies: opts.studies !== false,
      bars: [],
      priceLines: [],
      markers: [],
      markersApi: null,
      lastStudyPaint: 0,
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

    const candles = addSeries(chart, "CandlestickSeries", {});
    const volume = addSeries(chart, "HistogramSeries",
      { priceScaleId: "", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });

    const lines = {};
    if (state.studies) {
      const mk = (o) => addSeries(chart, "LineSeries",
        { lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false, ...o });
      lines.vwap = mk({ lineWidth: 2, title: "vwap" });
      lines.u1 = mk({ lineStyle: 2 }); lines.d1 = mk({ lineStyle: 2 });
      lines.u2 = mk({ lineStyle: 2 }); lines.d2 = mk({ lineStyle: 2 });
      lines.ema21 = mk({ title: "ema21" });
      lines.sma100 = mk({ lineStyle: 2, title: "sma100" });
      lines.sma200 = mk({ title: "sma200" });
      lines.dcU = mk({ lineStyle: 3 }); lines.dcL = mk({ lineStyle: 3 });
    }

    function paintTheme() {
      const c = palette();
      chart.applyOptions({ layout: { textColor: c.dim, fontFamily: c.mono } });
      candles.applyOptions({
        upColor: c.green, downColor: c.red, borderVisible: false,
        wickUpColor: alpha(c.green, 0.7), wickDownColor: alpha(c.red, 0.7),
      });
      if (state.studies) {
        lines.vwap.applyOptions({ color: c.accent });
        for (const k of ["u1", "d1"]) lines[k].applyOptions({ color: alpha(c.accent, 0.4) });
        for (const k of ["u2", "d2"]) lines[k].applyOptions({ color: alpha(c.accent, 0.22) });
        lines.ema21.applyOptions({ color: alpha(c.text, 0.55) });
        lines.sma100.applyOptions({ color: alpha(c.dim, 0.8) });
        lines.sma200.applyOptions({ color: c.dim });
        lines.dcU.applyOptions({ color: alpha(c.dim, 0.45) });
        lines.dcL.applyOptions({ color: alpha(c.dim, 0.45) });
      }
      repaintVolume();
    }

    function repaintVolume() {
      const c = palette();
      volume.setData(state.bars.map((b) => ({
        time: b.t, value: b.v,
        color: alpha(b.c >= b.o ? c.green : c.red, 0.35),
      })));
    }

    function paintStudies() {
      if (!state.studies || !state.bars.length) return;
      const intraday = state.intervalSec < 86400;
      if (intraday) {
        const vw = vwapBands(state.bars);
        lines.vwap.setData(vw.v);
        lines.u1.setData(vw.u1); lines.d1.setData(vw.d1);
        lines.u2.setData(vw.u2); lines.d2.setData(vw.d2);
      } else {
        for (const k of ["vwap", "u1", "d1", "u2", "d2"]) lines[k].setData([]);
      }
      lines.ema21.setData(ema(state.bars, 21));
      lines.sma100.setData(sma(state.bars, 100));
      lines.sma200.setData(sma(state.bars, 200));
      const dc = donchian(state.bars, 96);
      lines.dcU.setData(dc.up); lines.dcL.setData(dc.lo);
    }

    function setData(bars, intervalSec) {
      if (intervalSec) state.intervalSec = intervalSec;
      state.bars = bars.slice();
      chart.applyOptions({ timeScale: { timeVisible: state.intervalSec < 86400 } });
      candles.setData(bars.map((b) => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c })));
      repaintVolume();
      paintStudies();
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
      } else {
        last.c = price;
        last.h = Math.max(last.h, price);
        last.l = Math.min(last.l, price);
      }
      candles.update({ time: last.t, open: last.o, high: last.h, low: last.l, close: last.c });
      const now = Date.now();
      if (now - state.lastStudyPaint > 3000) {   // bands follow, gently
        state.lastStudyPaint = now;
        paintStudies();
      }
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
      if (!state.markers.length) return;
      if (LC().createSeriesMarkers) {
        if (!state.markersApi) state.markersApi = LC().createSeriesMarkers(candles, state.markers);
        else state.markersApi.setMarkers(state.markers);
      } else if (candles.setMarkers) {
        candles.setMarkers(state.markers);
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
      chart, setData, applyTick, setLevels, setMarkers,
      setIntervalSec: (s) => { state.intervalSec = s; },
      destroy() {
        window.removeEventListener("themechange", onTheme);
        chart.remove();
      },
    };
  }

  window.DeskChart = { create, palette };
})();
