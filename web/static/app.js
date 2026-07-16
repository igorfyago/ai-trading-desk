/* AI Trading Desk — one conversation, six agents, text + voice. */

const $ = (id) => document.getElementById(id);
// ONE conversation per browser, across every surface: the dashboard's Marcus
// panel and the full desk share this id (same origin = same localStorage),
// so threads, the trade log and the transcript all continue seamlessly.
const sessionId = (() => {
  const mint = () => (crypto.randomUUID
    ? crypto.randomUUID()   // secure contexts only (https/localhost)
    : "s-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10));
  try {
    let s = localStorage.getItem("desk-session");
    if (!s) { s = mint(); localStorage.setItem("desk-session", s); }
    return s;
  } catch { return mint(); }
})();
let catalog = null;
let current = null;            // {kind: 'text'|'persona', id, name, desc, hint}
let busy = false;
let voice = { live: false, pc: null, dc: null, mic: null, agentLine: null, lastActivity: 0 };
const VOICE_IDLE_MS = 3 * 60 * 1000;   // auto-hangup: an open mic left alone is
                                       // background-noise triggers + token burn
setInterval(() => {
  if (voice.live && Date.now() - voice.lastActivity > VOICE_IDLE_MS) {
    hangUp();
    divider("call ended — idle for 3 minutes");
  }
}, 15000);

const md = (text) => DOMPurify.sanitize(marked.parse(text ?? ""));

/* ------------------------------------------------------------ sidebar ---- */

const urlParams = new URLSearchParams(location.search);
if (urlParams.get("embed") === "1") document.body.classList.add("embed");

let spyLive = false;   // true once the SSE stream is painting the chip

function paintSpy(price, sourceNote) {
  const xsp = (price + 2).toFixed(2);   // display estimate; engine owns the real offset
  $("spy-chip").innerHTML =
    `SPY <b>${price}</b> &nbsp;→&nbsp; XSP ≈ <b>${xsp}</b>` +
    ` <span style="opacity:.6">· ${sourceNote}</span>`;
}

async function refreshSpy() {
  if (spyLive) return;                  // stream owns the chip when up
  try {
    const d = await fetch("/api/spot/SPY").then((r) => r.json());
    paintSpy(d.spot, [d.session, d.source].filter(Boolean).join(" · ") || "live");
  } catch { $("spy-chip").textContent = ""; }
}
refreshSpy();
setInterval(refreshSpy, 30000);

async function boot() {
  catalog = await fetch("/agents").then((r) => r.json());
  const ta = $("text-agents");
  ta.innerHTML = "";
  for (const a of catalog.text_agents) {
    ta.appendChild(agentButton({ kind: "text", id: a.id, name: a.name, desc: a.desc, hint: a.hint },
      `L${a.level}`));
  }
  const groups = { finance: $("finance-voice"), agency: $("agency-personas"), custom: $("custom-personas") };
  Object.values(groups).forEach((el) => (el.innerHTML = ""));
  for (const p of catalog.voice_personas) {
    const target = groups[p.category] || groups.agency;
    target.appendChild(agentButton({ kind: "persona", id: p.id, name: p.label, desc: p.tagline }, "🎙"));
  }
  $("custom-label").style.display = groups.custom.children.length ? "" : "none";
  renderBuilderOptions();
  await loadHistory();
  if (!current) {
    const want = urlParams.get("agent");
    const persona = want && catalog.voice_personas.find((p) => p.id === want);
    const textA = want && catalog.text_agents.find((a) => a.id === want);
    if (persona) select({ kind: "persona", id: persona.id, name: persona.label, desc: persona.tagline });
    else if (textA) select({ kind: "text", ...textA });
    else select({ kind: "text", ...catalog.text_agents[0] });
  }
}

let historyLoaded = false;

async function loadHistory() {
  // Replay the shared transcript: whatever was said on ANY surface with this
  // browser shows up here — one conversation, one desk.
  if (historyLoaded) return;
  historyLoaded = true;
  try {
    const h = await fetch(`/api/chatlog?session=${sessionId}&limit=40`).then((r) => r.json());
    if (!(h.messages || []).length) return;
    const names = {};
    for (const a of catalog.text_agents) names[a.id] = a.name;
    for (const m of h.messages) {
      const who = m.role === "user" ? "You" : (names[m.agent] || m.agent);
      const b = bubble(who, m.role === "user" ? "user" : "agent");
      if (m.role === "user") b.querySelector(".md").textContent = m.content;
      else b.querySelector(".md").innerHTML = md(m.content);
    }
    divider("live — same conversation on every screen");
  } catch { /* fresh start is fine */ }
}

function agentButton(sel, badge) {
  const b = document.createElement("button");
  b.className = "agent-btn";
  b.id = `btn-${sel.id}`;
  b.innerHTML = `<div class="lvl">${badge}</div><div><div class="nm">${sel.name}</div><div class="ds">${sel.desc}</div></div>`;
  b.onclick = () => select(sel);
  return b;
}

function select(sel) {
  if (voice.live) hangUp();
  if (current && current.id !== sel.id) divider(`switched to ${sel.name}`);
  current = sel;
  document.querySelectorAll(".agent-btn").forEach((b) => b.classList.remove("active"));
  $(`btn-${sel.id}`).classList.add("active");
  $("current-name").textContent = sel.name;
  $("current-desc").textContent = sel.desc;
  const voiceOnly = sel.kind === "persona";
  $("input").disabled = voiceOnly;
  $("send").disabled = voiceOnly;
  $("input").placeholder = voiceOnly
    ? "Voice-native agent — press the mic and talk"
    : (sel.hint ? `e.g. ${sel.hint}` : "Ask the agent…");
}

/* --------------------------------------------------------------- log ---- */

function divider(text) {
  const d = document.createElement("div");
  d.className = "divider";
  d.textContent = `— ${text} —`;
  $("log").appendChild(d);
}

function bubble(who, cls) {
  const b = document.createElement("div");
  b.className = `bubble ${cls}`;
  b.innerHTML = `<div class="who"></div><div class="chips"></div><div class="md"></div>`;
  b.querySelector(".who").textContent = who;
  $("log").appendChild(b);
  scroll();
  return b;
}

function chip(b, cls, text) {
  const existing = [...b.querySelectorAll(".chip")].find((c) => c.textContent === text);
  if (existing) return;
  const c = document.createElement("span");
  c.className = `chip ${cls}`;
  c.textContent = text;
  b.querySelector(".chips").appendChild(c);
  scroll();
}

const scroll = () => { $("log").scrollTop = $("log").scrollHeight; };

/* -------------------------------------------------------------- chat ---- */

async function send() {
  const text = $("input").value.trim();
  if (!text || busy || !current || current.kind !== "text") return;
  $("input").value = "";
  busy = true; $("send").disabled = true;
  const u = bubble("You", "user");
  u.querySelector(".md").textContent = text;
  await streamInto(`/chat/${current.id}`, { message: text, session: sessionId });
  busy = false; $("send").disabled = false;
  $("input").focus();
}

async function streamInto(url, payload) {
  const b = bubble(current.name, "agent");
  const body = b.querySelector(".md");
  body.innerHTML = `<span class="working">working</span>`;
  let streamed = "";
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        handleChatEvent(JSON.parse(line), b, body, (t) => {
          streamed += t;
          body.textContent = streamed;
        });
        scroll();
      }
    }
  } catch (err) {
    b.classList.add("error");
    body.textContent = "connection error: " + err.message;
  }
}

function handleChatEvent(ev, b, body, addToken) {
  switch (ev.type) {
    case "node": chip(b, "node", ev.name); break;
    case "tool": chip(b, "tool", `🔧 ${ev.name}(${ev.args ?? ""})`); break;
    case "token": addToken(ev.text); break;
    case "final": body.innerHTML = md(ev.text); break;
    case "interrupt": renderApproval(b, body, ev.memo); break;
    case "error":
      b.classList.add("error");
      b.querySelector(".who").textContent = "error";
      body.textContent = ev.text;
      break;
  }
}

function renderApproval(b, body, memo) {
  body.innerHTML = md(
    `**The desk needs your sign-off.**\n\n` +
    `**${(memo.bias || "").toUpperCase()}** · conviction ${memo.conviction}/10\n\n` +
    `${memo.thesis}\n\n**Trade:** ${memo.trade_idea}\n\n` +
    `**Levels:** ${(memo.key_levels || []).join(" · ")}\n\n` +
    `**Invalidation:** ${memo.invalidation}`
  );
  const box = document.createElement("div");
  box.className = "approval";
  box.innerHTML = `<h4>Human in the loop — your call</h4>
    <div class="btns">
      <button class="ok">Approve & publish</button>
      <button class="rev">Request changes</button>
      <button class="no">Reject</button>
    </div>
    <textarea placeholder="What should change?"></textarea>`;
  body.appendChild(box);
  const ta = box.querySelector("textarea");
  box.querySelector(".ok").onclick = () => decide("approve", "");
  box.querySelector(".no").onclick = () => decide("reject", "");
  box.querySelector(".rev").onclick = () => {
    if (ta.style.display !== "block") { ta.style.display = "block"; ta.focus(); return; }
    decide("revise", ta.value);
  };
  async function decide(action, notes) {
    box.querySelectorAll("button").forEach((x) => (x.disabled = true));
    divider(`you chose: ${action}`);
    await streamInto("/chat/analyst/resume", { session: sessionId, action, notes });
  }
  scroll();
}

/* -------------------------------------------------------------- voice ---- */

async function toggleVoice() {
  if (voice.live) { hangUp(); return; }
  if (!current) return;
  $("mic").disabled = true;
  $("voice-state").textContent = "connecting…";
  try {
    const url = current.kind === "persona"
      ? `/session/${current.id}` : `/session/bridge/${current.id}`;
    const sess = await fetch(url, { method: "POST" }).then((r) => {
      if (!r.ok) throw new Error("could not start a voice session");
      return r.json();
    });

    voice.mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    voice.pc = new RTCPeerConnection();
    voice.pc.ontrack = (e) => { const a = new Audio(); a.srcObject = e.streams[0]; a.play(); };
    voice.mic.getTracks().forEach((t) => voice.pc.addTrack(t, voice.mic));
    voice.dc = voice.pc.createDataChannel("oai-events");
    voice.dc.onmessage = (e) => handleVoiceEvent(JSON.parse(e.data));
    // The agent answers the phone — it speaks FIRST, like a real receptionist.
    voice.dc.onopen = () => voice.dc.send(JSON.stringify({ type: "response.create" }));

    const offer = await voice.pc.createOffer();
    await voice.pc.setLocalDescription(offer);
    const sdp = await fetch("https://api.openai.com/v1/realtime/calls", {
      method: "POST", body: offer.sdp,
      headers: { Authorization: `Bearer ${sess.client_secret}`, "Content-Type": "application/sdp" },
    }).then((r) => r.text());
    await voice.pc.setRemoteDescription({ type: "answer", sdp });

    voice.live = true;
    voice.lastActivity = Date.now();
    $("mic").classList.add("live");
    $("voice-state").textContent = `voice live — ${sess.label}`;
    $("voice-state").classList.add("live");
    divider(`voice call started — ${sess.label}`);
  } catch (err) {
    $("voice-state").textContent = "voice error: " + err.message;
    hangUp(true);
  }
  $("mic").disabled = false;
}

function hangUp(silent) {
  voice.dc?.close(); voice.pc?.close();
  voice.mic?.getTracks().forEach((t) => t.stop());
  const wasLive = voice.live;
  voice = { live: false, pc: null, dc: null, mic: null, agentLine: null };
  $("mic").classList.remove("live");
  if (!silent) {
    $("voice-state").textContent = "";
    $("voice-state").classList.remove("live");
    if (wasLive) divider("voice call ended");
  }
}

async function handleVoiceEvent(ev) {
  if (ev.type === "conversation.item.input_audio_transcription.completed" ||
      ev.type === "response.output_audio_transcript.delta") {
    voice.lastActivity = Date.now();   // real conversation keeps the line open
  }
  switch (ev.type) {
    case "response.output_audio_transcript.delta": {
      if (!voice.agentLine) {
        const b = bubble(`${current.name} (voice)`, "agent");
        voice.agentLine = b.querySelector(".md");
      }
      voice.agentLine.textContent += ev.delta;
      scroll();
      break;
    }
    case "conversation.item.input_audio_transcription.completed": {
      const b = bubble("You (voice)", "user");
      b.querySelector(".md").textContent = (ev.transcript || "").trim() || "(audio)";
      // transcription lands async — keep it ABOVE the agent's in-flight reply
      const agentBubble = voice.agentLine && voice.agentLine.closest(".bubble");
      if (agentBubble) $("log").insertBefore(b, agentBubble);
      scroll();
      break;
    }
    case "response.done": {
      voice.agentLine = null;
      for (const item of ev.response?.output || []) {
        if (item.type === "function_call") await runVoiceTool(item);
      }
      break;
    }
  }
}

async function runVoiceTool(item) {
  const b = [...document.querySelectorAll(".bubble.agent")].pop();
  if (b) chip(b, "tool", `🔧 ${item.name}`);
  const url = current.kind === "persona" ? `/tool/${current.id}` : `/tool/bridge/${current.id}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: item.name, arguments: JSON.parse(item.arguments), session: sessionId }),
  }).then((r) => r.json());
  voice.dc.send(JSON.stringify({
    type: "conversation.item.create",
    item: { type: "function_call_output", call_id: item.call_id, output: res.output },
  }));
  voice.dc.send(JSON.stringify({ type: "response.create" }));
}

/* --------------------------------------------------------------- wire ---- */

/* ------------------------------------------------------------ builder ---- */

function renderBuilderOptions() {
  const b = catalog.builder || { voices: [], tools: [] };
  $("b-voice").innerHTML = b.voices.map((v) => `<option>${v}</option>`).join("");
  $("b-tools").innerHTML = b.tools.map((t) =>
    `<label><input type="checkbox" value="${t}"> ${t}</label>`).join("");
}

$("open-builder").onclick = () => $("builder-overlay").classList.add("open");
$("b-cancel").onclick = () => $("builder-overlay").classList.remove("open");

$("b-create").onclick = async () => {
  const msg = $("b-msg");
  msg.className = ""; msg.textContent = "creating…";
  try {
    const resp = await fetch("/api/personas", {
      method: "POST",
      headers: { "Content-Type": "application/json",
                 "X-Admin-Token": $("b-token").value.trim() },
      body: JSON.stringify({
        label: $("b-name").value.trim(),
        tagline: $("b-tag").value.trim(),
        voice: $("b-voice").value,
        instructions: $("b-instr").value.trim(),
        tools: [...document.querySelectorAll("#b-tools input:checked")].map((i) => i.value),
      }),
    });
    const d = await resp.json();
    if (!resp.ok) throw new Error(d.detail || "failed");
    msg.className = "ok"; msg.textContent = `created "${d.label}" — it's in the sidebar`;
    await boot();
    setTimeout(() => $("builder-overlay").classList.remove("open"), 900);
  } catch (e) { msg.textContent = e.message; }
};

$("send").onclick = send;
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === "Return" || e.keyCode === 13) send();
});
$("mic").onclick = toggleVoice;
boot();

/* ---------------------------------------------------------- trade dock ----
   The chart that lives NEXT TO the conversation. Marcus pins every quoted
   trade here automatically; "I'm in" / "sold half" / "I'm out" move it
   through its lifecycle and the P&L badge marks to the live feed. Mounted in
   the shell (not a tab), so it survives agent switches — and the ⧉ button
   pops it into a Document Picture-in-Picture window that floats above every
   browser tab and app while a paper position is on. */

const dock = { chart: null, active: null, pip: null, quotesES: null, eventsES: null };
const DOCK_SECS = 300;   // 5m candles — the trade-management timeframe

function fmtUsd(x) {
  if (x === null || x === undefined) return "";
  return (x >= 0 ? "+$" : "−$") + Math.abs(x).toFixed(0);
}

function dockContract(t) {
  return `${t.contract_ticker} ${Number(t.strike)}${t.kind[0]}`;
}

function dockShow() {
  const d = $("dock");
  if (d.hidden) d.hidden = false;
}

function dockRender() {
  const t = dock.active;
  if (!t) return;
  dockShow();
  $("dock-status").className = t.status;
  $("dock-title").textContent = `${dockContract(t)} · ${t.expiry.slice(5)}`;
  const notes = {
    quoted: `quoted ~${t.quoted_px}`,
    open: `in @ ${t.entry_px} × ${t.contracts_open}`,
    trimmed: `runner × ${t.contracts_open} · banked ${fmtUsd(t.realized_usd)}`,
    closed: `closed · ${fmtUsd(t.realized_usd)} realized`,
  };
  $("dock-note").textContent = notes[t.status] || "";
  if (t.status === "closed") {
    const pnl = $("dock-pnl");
    pnl.textContent = fmtUsd(t.realized_usd);
    pnl.className = t.realized_usd >= 0 ? "pos" : "neg";
  } else if (t.status === "quoted") {
    $("dock-pnl").textContent = "";
  }
  dockLevels(t);
  dockMarkers(t);
}

function dockLevels(t) {
  if (!dock.chart) return;
  const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  dock.chart.setLevels([
    t.entry_underlying && {
      price: t.entry_underlying, color: cssv("--text") || "#eceef4",
      style: 0, title: t.status === "quoted" ? "entry" : "in",
    },
    t.tp50_underlying && {
      price: t.tp50_underlying, color: cssv("--green") || "#3ecf8e",
      style: 2, title: "trim +50%",
    },
    t.thesis_reference && {
      price: t.thesis_reference, color: cssv("--dim") || "#9ba3b2",
      style: 3, title: "thesis",
    },
  ].filter(Boolean));
}

function dockMarkers(t) {
  if (!dock.chart) return;
  const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const ts = (iso) => Math.floor(Date.parse(iso) / 1000);
  const ms = [];
  if (t.created_at) ms.push({ time: ts(t.created_at), position: "belowBar",
    shape: "circle", color: cssv("--dim"), text: "quoted" });
  if (t.entry_at) ms.push({ time: ts(t.entry_at), position: "belowBar",
    shape: "arrowUp", color: cssv("--green"), text: "in" });
  if (t.trim_at) ms.push({ time: ts(t.trim_at), position: "aboveBar",
    shape: "circle", color: cssv("--accent"), text: "½ off" });
  if (t.close_at) ms.push({ time: ts(t.close_at), position: "aboveBar",
    shape: "arrowDown", color: cssv("--red"), text: "out" });
  dock.chart.setMarkers(ms);
}

async function dockChartBoot(underlying) {
  if (dock.chart || !window.DeskChart) return;
  try {
    const data = await fetch(`/api/bars/${underlying || "SPY"}?interval=5m&limit=400`)
      .then((r) => (r.ok ? r.json() : null));
    if (!data || !(data.bars || []).length) return;   // text-only dock still works
    dock.chart = DeskChart.create($("dock-chart"), { intervalSec: DOCK_SECS, mode: "mini" });
    dock.chart.setData(data.bars, DOCK_SECS);
    if (dock.active) { dockLevels(dock.active); dockMarkers(dock.active); }
  } catch { /* chart is a bonus; the pill always works */ }
}

function dockPnl(positions) {
  const t = dock.active;
  if (!t) return;
  const row = (positions || []).find((p) => p.id === t.id);
  if (!row) return;
  dock.active = { ...t, ...row };
  const pnl = $("dock-pnl");
  if (row.unreal_usd !== null && row.unreal_usd !== undefined) {
    pnl.textContent = `${fmtUsd(row.unreal_usd)} · ${row.unreal_pct >= 0 ? "+" : ""}${row.unreal_pct}%`;
    pnl.className = row.unreal_usd >= 0 ? "pos" : "neg";
    if (row.tp_hit) pnl.textContent += " · TRIM ZONE";
  }
}

function handleDeskEvent(d) {
  if (d.type === "boot") {
    const open = (d.positions || [])[0];
    const latest = open || (d.recent || []).find((t) => t.status === "quoted");
    if (latest) {
      dock.active = latest;
      dockRender();
      dockChartBoot(latest.underlying);
      if (open) dockPnl(d.positions);
    }
    return;
  }
  if (d.type === "trade") {
    dock.active = d.trade;
    dockRender();
    dockChartBoot(d.trade.underlying);
    const lines = {
      quoted: `Marcus pinned ${dockContract(d.trade)} ~${d.trade.quoted_px} — on the chart ↑`,
      opened: `you're IN ${dockContract(d.trade)} @ ${d.trade.entry_px} — monitoring P&L ↑`,
      trimmed: `half off @ ${d.trade.trim_px} — runner rides`,
      closed: `flat — ${fmtUsd(d.trade.realized_usd)} on the trade`,
    };
    if (lines[d.event]) divider(lines[d.event]);
    return;
  }
  if (d.type === "pnl") dockPnl(d.positions);
}

async function refreshScore() {
  try {
    const s = await fetch("/api/score").then((r) => r.json());
    const el = $("score-chip");
    if (!el) return;
    const v = s.score ?? 0;
    el.textContent = `P&L ${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(0)}` +
      (s.open_positions ? ` · ${s.open_positions} open` : "");
    el.className = v >= 0 ? "pos" : "neg";
  } catch { /* scoreboard is a bonus */ }
}

function dockStreams() {
  // Embedded inside the landing dashboard (?embed=1) the PARENT page owns the
  // chart, the score and the trade lines — a second chart in the iframe was
  // just noise. The embed keeps only the conversation (+ live price chip).
  const embedded = document.body.classList.contains("embed");
  if (!embedded) {
    dock.eventsES = new EventSource("/api/stream/events");
    dock.eventsES.onmessage = (e) => {
      let d; try { d = JSON.parse(e.data); } catch { return; }
      handleDeskEvent(d);
      if (d.type === "trade" || d.type === "pnl") refreshScore();
    };
    refreshScore();
  }
  dock.quotesES = new EventSource("/api/stream/quotes?symbols=SPY");
  dock.quotesES.onmessage = (e) => {
    let d; try { d = JSON.parse(e.data); } catch { return; }
    if (d.type !== "quote" || d.ticker !== "SPY") return;
    spyLive = true;
    paintSpy(d.price, [d.session, d.source].filter(Boolean).join(" · ") || "live");
    if (dock.chart) {
      const t = d.ts ? Math.floor(Date.parse(d.ts) / 1000) : Math.floor(Date.now() / 1000);
      dock.chart.applyTick(d.price, t);
    }
  };
}

async function dockPopOut() {
  if (dock.pip) { dock.pip.close(); return; }
  try {
    const pip = await documentPictureInPicture.requestWindow({ width: 480, height: 320 });
    for (const ss of document.styleSheets) {
      try {
        const style = pip.document.createElement("style");
        style.textContent = [...ss.cssRules].map((r) => r.cssText).join("\n");
        pip.document.head.appendChild(style);
      } catch {
        if (ss.href) {
          const link = pip.document.createElement("link");
          link.rel = "stylesheet"; link.href = ss.href;
          pip.document.head.appendChild(link);
        }
      }
    }
    // carry the live theme vars (themes.js sets them inline on <html>)
    pip.document.documentElement.style.cssText = document.documentElement.style.cssText;
    pip.document.body.className = "dock-pip";
    pip.document.body.appendChild($("dock"));
    dock.pip = pip;
    $("dock-pop").textContent = "⧈";
    pip.addEventListener("pagehide", () => {
      $("chat-header").after($("dock"));
      dock.pip = null;
      $("dock-pop").textContent = "⧉";
    });
  } catch { /* PiP denied/unsupported: dock stays in the shell */ }
}

function dockInit() {
  $("dock-min").onclick = () => {
    const min = $("dock").classList.toggle("min");
    $("dock-min").textContent = min ? "▸" : "▾";
    localStorage.setItem("dock-min", min ? "1" : "0");
  };
  if (localStorage.getItem("dock-min") === "1") {
    $("dock").classList.add("min");
    $("dock-min").textContent = "▸";
  }
  if ("documentPictureInPicture" in window) $("dock-pop").onclick = dockPopOut;
  else $("dock-pop").style.display = "none";
  dockStreams();
}
dockInit();
