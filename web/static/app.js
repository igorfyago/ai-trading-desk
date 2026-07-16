/* AI Trading Desk — one conversation, six agents, text + voice. */

const $ = (id) => document.getElementById(id);
// crypto.randomUUID only exists in secure contexts (https/localhost) — fall back
const sessionId = crypto.randomUUID
  ? crypto.randomUUID()
  : "s-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
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
  if (!current) select({ kind: "text", ...catalog.text_agents[0] });
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

/* ----------------------------------------------------------- ambience ---- */
/* Procedural office sound design (no audio files): a faint room tone while
   the call is live, and keyboard foley that plays exactly while the agent's
   tool calls run — "let me pull that up" + real typing sounds. The point:
   digital silence is the #1 bot tell; a real mic has a room behind it. */

const amb = { ctx: null, master: null, room: null, typing: false };

function ambStart() {
  // Keyboard foley only — no room-tone drone (tried it, sounded like traffic).
  if (amb.ctx) return;
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const master = ctx.createGain();
  master.gain.value = 1.0;
  master.connect(ctx.destination);
  amb.ctx = ctx; amb.master = master;
}

function ambStop() {
  amb.typing = false;
  if (amb.ctx) { amb.ctx.close(); amb.ctx = null; }
}

function keyClick() {
  if (!amb.ctx) return;
  const ctx = amb.ctx;
  const dur = 0.008 + Math.random() * 0.012;
  const buf = ctx.createBuffer(1, ctx.sampleRate * dur, ctx.sampleRate);
  const data = buf.getChannelData(0);
  for (let i = 0; i < data.length; i++) {
    data[i] = (Math.random() * 2 - 1) * Math.exp(-i / (data.length * 0.3));
  }
  const src = ctx.createBufferSource();
  src.buffer = buf;
  const bp = ctx.createBiquadFilter();
  bp.type = "bandpass";
  bp.frequency.value = 2000 + Math.random() * 2500;  // every key sounds different
  bp.Q.value = 1.2;
  const g = ctx.createGain();
  g.gain.value = 0.04 + Math.random() * 0.05;
  src.connect(bp).connect(g).connect(amb.master);
  src.start();
}

function typeBurst() {                            // one word-ish flurry of keys
  if (!amb.ctx || !amb.typing) return;
  const keys = 3 + Math.floor(Math.random() * 6);
  let t = 0;
  for (let i = 0; i < keys; i++) {
    t += 55 + Math.random() * 110;                // human inter-key jitter
    setTimeout(() => amb.typing && keyClick(), t);
  }
  setTimeout(typeBurst, t + 150 + Math.random() * 500);  // pause between words
}

function typingStart() { if (amb.ctx && !amb.typing) { amb.typing = true; typeBurst(); } }
function typingStop() { amb.typing = false; }


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
    ambStart();
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
  ambStop();
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
  typingStart();                       // she's "looking it up" — keys clatter
  const url = current.kind === "persona" ? `/tool/${current.id}` : `/tool/bridge/${current.id}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: item.name, arguments: JSON.parse(item.arguments), session: sessionId }),
  }).then((r) => r.json()).finally(typingStop);
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
