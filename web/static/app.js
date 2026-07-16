/* AI Trading Desk — one conversation, six agents, text + voice. */

const $ = (id) => document.getElementById(id);
const sessionId = crypto.randomUUID();
let catalog = null;
let current = null;            // {kind: 'text'|'persona', id, name, desc, hint}
let busy = false;
let voice = { live: false, pc: null, dc: null, mic: null, agentLine: null };

const md = (text) => DOMPurify.sanitize(marked.parse(text ?? ""));

/* ------------------------------------------------------------ sidebar ---- */

async function boot() {
  catalog = await fetch("/agents").then((r) => r.json());
  const ta = $("text-agents");
  for (const a of catalog.text_agents) {
    ta.appendChild(agentButton({ kind: "text", id: a.id, name: a.name, desc: a.desc, hint: a.hint },
      `L${a.level}`));
  }
  const vp = $("voice-personas");
  for (const p of catalog.voice_personas) {
    vp.appendChild(agentButton({ kind: "persona", id: p.id, name: p.label, desc: p.tagline }, "🎙"));
  }
  select({ kind: "text", ...catalog.text_agents[0] });
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

    const offer = await voice.pc.createOffer();
    await voice.pc.setLocalDescription(offer);
    const sdp = await fetch("https://api.openai.com/v1/realtime/calls", {
      method: "POST", body: offer.sdp,
      headers: { Authorization: `Bearer ${sess.client_secret}`, "Content-Type": "application/sdp" },
    }).then((r) => r.text());
    await voice.pc.setRemoteDescription({ type: "answer", sdp });

    voice.live = true;
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
      b.querySelector(".md").textContent = ev.transcript || "(audio)";
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

$("send").onclick = send;
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === "Return" || e.keyCode === 13) send();
});
$("mic").onclick = toggleVoice;
boot();
