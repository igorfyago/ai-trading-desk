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
let voice = { live: false, textOnly: false, responseActive: false, pendingResponse: false,
            pc: null, dc: null, mic: null, agentLine: null,
              audioEl: null, muteGuard: false, scrubNext: false, lastActivity: 0 };

/* ------------------------------------------------- the desk heartbeat ----
   A long call with a trader friend is mostly silence. He is not waiting to
   be asked - he is watching, and when something CHANGES he says so: a fresh
   setup fires, the trim prints on the trade he pitched, the runner
   round-trips to entry, the +400% ride completes, the add level trades, or
   the tape arms the OTHER way and the pitch is dead.

   The seen-set is what keeps him from being a nuisance: every event carries
   a fingerprint and is spoken exactly once. The pitch (what he last read
   out) is captured from the trade_recommendation payload and passed back on
   every beat, so the desk can mark HIS trade, not a hypothetical one. */
const BEAT_MS = 20_000;
let beat = { timer: null, seen: new Set(), ticker: "SPY", pitch: null };

// what breaks silence first when several things are true at once: a dead
// premise outranks profit-taking, and both outrank a brand-new idea
const BEAT_RANK = ["reversal", "runner_target", "runner_stop", "trim", "add", "new_setup"];

const BEAT_SAY = {
  new_setup: (e) => `DESK ALERT - ${e.signal} just fired on ${beat.ticker}: ${e.why}. `
    + `Break in now, the way you would on a long call: one short human opener, `
    + `then the trade. Call trade_recommendation for the exact contract, and `
    + `draw_levels in the same turn.`,
  reversal: (e) => `DESK ALERT - the premise of the trade you pitched is GONE: ${e.why}. `
    + `Break in now and say so plainly - if they're in it, this is where it comes `
    + `off. Then call trade_recommendation and pitch the new side.`,
  trim: (e) => `DESK ALERT - trim level: ${e.why}. Break in: half comes off here, `
    + `that's the rule. Then the runner is free - stopped at entry or it rides `
    + `to +400%. Say the two numbers, then stop.`,
  runner_stop: (e) => `DESK ALERT - runner round-trip: ${e.why}. Break in: the rest `
    + `comes off at entry, flat on the runner, the trim already paid. One line.`,
  runner_target: (e) => `DESK ALERT - the ride is done: ${e.why}. Break in: take the `
    + `rest off, that's the whole trade, congratulate them like a human would.`,
  add: (e) => `DESK ALERT - the add level traded: ${e.why}. Break in: the second `
    + `half of the clip goes on here if they took the first. Say the level and `
    + `the size, then stop.`,
};

function beatQuery() {
  const p = beat.pitch;
  if (!p) return "";
  const q = new URLSearchParams({ kind: p.kind, strike: p.strike, entry: p.entry,
                                  expiry: p.expiry || "" });
  if (p.add_level) q.set("add", p.add_level);
  return "?" + q.toString();
}

function startHeartbeat(ticker) {
  stopHeartbeat();
  beat.ticker = (ticker || "SPY").toUpperCase();
  beat.seen = new Set();
  beat.pitch = null;
  // whatever is already true at pickup is old news, not an opening ambush
  fetch(`/api/watch/${beat.ticker}`).then(r => r.json())
    .then(d => (d.events || []).forEach(e => beat.seen.add(e.fingerprint)))
    .catch(() => {});
  beat.timer = setInterval(checkTape, BEAT_MS);
}

function stopHeartbeat() {
  if (beat.timer) clearInterval(beat.timer);
  beat.timer = null;
}

async function checkTape() {
  if (!voice.live || !voice.dc || voice.dc.readyState !== "open") return;
  if (voice.responseActive) return;       // never cut across him mid-sentence
  if (voice.userTalking) return;          // and never across the CALLER either
  let d;
  try {
    d = await fetch(`/api/watch/${beat.ticker}${beatQuery()}`).then(r => r.json());
  } catch { return; }
  const fresh = (d && d.events || []).filter(e => e.fingerprint && !beat.seen.has(e.fingerprint))
    // a round-trip to entry only MEANS anything after the trim: before it the
    // doctrine is size-for-zero, sit still - so the stop stays unspoken until
    // the trim for the same contract has been called out
    .filter(e => e.event !== "runner_stop"
      || [...beat.seen].some(f => f.includes(":trim:") && f.endsWith(e.fingerprint.split(":runner_stop:")[1])));
  if (!fresh.length) return;
  fresh.sort((a, b) => BEAT_RANK.indexOf(a.event) - BEAT_RANK.indexOf(b.event));
  const e = fresh[0];                     // ONE interruption per beat, the loudest
  beat.seen.add(e.fingerprint);

  // Framed as the desk speaking to him, not as the caller asking. A user-role
  // item would read as though the caller said it, and he would answer a
  // question nobody asked.
  try {
    voice.dc.send(JSON.stringify({
      type: "conversation.item.create",
      item: { type: "message", role: "system", content: [{ type: "input_text",
        text: (BEAT_SAY[e.event] || BEAT_SAY.new_setup)(e) }] },
    }));
    requestResponse();
    divider(`desk alert · ${e.event.replace("_", " ")} on ${beat.ticker}`);
  } catch { /* channel closing */ }
}

/* ------------------------------------------------------ the call log ----
   One record per logical turn (caller spoke -> tools ran -> Marcus answered),
   POSTed for offline review by `python -m common.calllog review`. The clock
   is the point: dead air is what makes a turn feel wrong, and you cannot fix
   what you never measured. sendBeacon so a logging hiccup can never cost the
   caller a word, and so a turn still lands if the tab closes mid-call. */
let turn = null;

function turnStart() {
  turn = { user: "", agent: "", userAt: 0, userStoppedAt: 0, firstWordAt: 0,
           tools: [], covered: null, bargeIn: false };
}

function turnFlush(ghost) {
  const t = turn;
  turn = null;
  if (!t || ghost) return;              // a scrubbed reply is noise, not evidence
  if (!t.user && !t.agent) return;
  try {
    navigator.sendBeacon("/calllog", new Blob([JSON.stringify({
      session: sessionId, persona: current?.id || "marcus",
      user: t.user, agent: t.agent,
      // from the caller falling silent, NOT from the transcript landing
      ms_dead_air: t.userStoppedAt && t.firstWordAt
        ? t.firstWordAt - t.userStoppedAt : null,
      tools: t.tools, covered: t.covered, barge_in: t.bargeIn,
    })], { type: "application/json" }));
  } catch { /* logging is never worth an exception on a live call */ }
}

const VOICE_IDLE_MS = 3 * 60 * 1000;   // auto-hangup: an open mic left alone is
                                       // background-noise triggers + token burn
setInterval(() => {
  if (voice.live && Date.now() - voice.lastActivity > VOICE_IDLE_MS) {
    hangUp();
    divider("call ended · idle for 3 minutes");
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
  // This host is the trading app now: Marcus is the only agent here. The rest
  // of the roster and the agent builder moved to the observatory, so the
  // catalog is just the desk's voice personas (Marcus leads).
  catalog = await fetch("/agents").then((r) => r.json());
  catalog.text_agents = catalog.text_agents || [];
  const nav = $("persona-list");
  nav.innerHTML = "";
  for (const p of catalog.voice_personas) {
    nav.appendChild(agentButton({ kind: "persona", id: p.id, name: p.label, desc: p.tagline }, "🎙"));
  }
  await loadHistory();
  if (!current) {
    const want = urlParams.get("agent");
    const persona = catalog.voice_personas.find((p) => p.id === want)
      || catalog.voice_personas.find((p) => p.id === "marcus")
      || catalog.voice_personas[0];
    if (persona) select({ kind: "persona", id: persona.id, name: persona.label, desc: persona.tagline });
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
    for (const p of catalog.voice_personas) names[p.id] = p.label;
    for (const m of h.messages) {
      const who = m.role === "user" ? "You" : (names[m.agent] || m.agent);
      const b = bubble(who, m.role === "user" ? "user" : "agent");
      if (m.role === "user") b.querySelector(".md").textContent = m.content;
      else b.querySelector(".md").innerHTML = md(m.content);
    }
    divider("live · same conversation on every screen");
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
  $("input").placeholder = voiceOnly
    ? "Type here, or tap the glowing mic and talk"
    : (sel.hint ? `e.g. ${sel.hint}` : "Ask the agent…");
}

/* --------------------------------------------------------------- log ---- */

function divider(text) {
  const d = document.createElement("div");
  d.className = "divider";
  d.textContent = `· ${text} ·`;
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
  if (!text || busy || !current) return;
  if (current.kind === "persona") {
    // typed turn to a voice agent: rides the live call, or quietly opens a
    // no-mic session whose replies still SPEAK
    $("input").value = "";
    const u = bubble("You", "user");
    u.querySelector(".md").textContent = text;
    if (voice.live) sendTextTurn(text);
    else await startVoice({ withMic: false, queueText: text });
    $("input").focus();
    return;
  }
  if (current.kind !== "text") return;
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
  box.innerHTML = `<h4>Human in the loop: your call</h4>
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
  if (voice.live && !voice.textOnly) { hangUp(); return; }
  if (voice.live && voice.textOnly) hangUp(true);   // typed session upgrades to a real call
  await startVoice({ withMic: true });
}

async function startVoice({ withMic, queueText } = { withMic: true }) {
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

    voice.pc = new RTCPeerConnection();
    voice.pc.ontrack = (e) => {
      voice.audioEl = new Audio();            // kept: the ghost guard mutes it
      voice.audioEl.srcObject = e.streams[0];
      voice.audioEl.play();
    };
    if (withMic) {
      voice.mic = await navigator.mediaDevices.getUserMedia({ audio: true });
      voice.mic.getTracks().forEach((t) => voice.pc.addTrack(t, voice.mic));
    } else {
      // typed session: no microphone, the agent still SPEAKS its replies
      voice.pc.addTransceiver("audio", { direction: "recvonly" });
    }
    voice.dc = voice.pc.createDataChannel("oai-events");
    voice.dc.onmessage = (e) => handleVoiceEvent(JSON.parse(e.data));
    // Mic call: the agent answers the phone and speaks FIRST. Typed session:
    // the queued message IS the opener, no greeting over it.
    voice.dc.onopen = () => {
      if (queueText) sendTextTurn(queueText);
      else requestResponse();
    };

    const offer = await voice.pc.createOffer();
    await voice.pc.setLocalDescription(offer);
    const sdp = await fetch("https://api.openai.com/v1/realtime/calls", {
      method: "POST", body: offer.sdp,
      headers: { Authorization: `Bearer ${sess.client_secret}`, "Content-Type": "application/sdp" },
    }).then((r) => r.text());
    await voice.pc.setRemoteDescription({ type: "answer", sdp });

    voice.live = true;
    startHeartbeat(current?.id === 'marcus' ? 'SPY' : null);
    voice.textOnly = !withMic;
    voice.lastActivity = Date.now();
    if (withMic) $("mic").classList.add("live");
    $("voice-state").textContent = withMic
      ? `voice live · ${sess.label}` : `chat live · replies speak · ${sess.label}`;
    $("voice-state").classList.add("live");
    divider(withMic ? `voice call started · ${sess.label}`
                    : `chat started · ${sess.label} answers out loud`);
  } catch (err) {
    $("voice-state").textContent = "voice error: " + err.message;
    hangUp(true);
  }
  $("mic").disabled = false;
}

/* THE ONE DOOR to response.create. The Realtime session can run exactly ONE
   response at a time: a second response.create while one is speaking makes
   the two compete and the live one dies MID-WORD. That is the cutoff. Every
   path (greeting, typed turn, tool results, noise-repair) goes through here,
   and anything asked for while a response is live is QUEUED, never raced. */
function requestResponse() {
  if (!voice.dc || voice.dc.readyState !== "open") return;
  if (voice.responseActive) { voice.pendingResponse = true; return; }
  voice.expectReply = true;
  voice.lastActivity = Date.now();
  try { voice.dc.send(JSON.stringify({ type: "response.create" })); }
  catch { /* channel closing */ }
}

function sendTextTurn(text) {
  voice.dc.send(JSON.stringify({
    type: "conversation.item.create",
    item: { type: "message", role: "user",
            content: [{ type: "input_text", text }] },
  }));
  requestResponse();
}

function hangUp(silent) {
  stopHeartbeat();
  voice.dc?.close(); voice.pc?.close();
  voice.mic?.getTracks().forEach((t) => t.stop());
  voice.audioEl?.pause();
  const wasLive = voice.live;
  voice = { live: false, textOnly: false, responseActive: false, pendingResponse: false,
            pc: null, dc: null, mic: null, agentLine: null,
            audioEl: null, muteGuard: false, scrubNext: false, lastActivity: 0 };
  $("mic").classList.remove("live");
  if (!silent) {
    $("voice-state").textContent = "";
    $("voice-state").classList.remove("live");
    if (wasLive) divider("voice call ended");
  }
}

/* Ghost-speech kill switch. Server VAD sometimes commits background noise as
   a "turn" minutes into silence; the model then answers nobody and the noise
   items pollute the context ("repeats whatever we were talking about").
   Prompt rules can't stop it — the model never sees the VAD decision — so the
   CLIENT arbitrates: a turn whose transcription has no real words gets its
   response cancelled, its audio flushed, and both its items scrubbed from the
   conversation. After long idle we additionally hold playback muted until the
   transcript proves a human actually spoke. */

const REAL_WORDS = /[\p{L}\p{N}]{2,}/u;
const IDLE_GUARD_MS = 30_000;

function killGhostTurn(userItemId, wasSpeaking) {
  // Only the GHOST may be cancelled. A reply that began BEFORE this phantom
  // turn is a real sentence the caller is listening to · cancelling that was
  // the desk cutting itself off while nobody spoke. Compare the clocks: the
  // active response is the ghost only if it was created after the noise.
  // NEVER kill an answer to a question that was actually asked. expectReply is
  // set the moment a transcription passes REAL_WORDS (and by every deliberate
  // requestResponse), and cleared at response.done, so it marks exactly the
  // window where the live response is owed to somebody.
  //
  // Without it the clock comparison below false-positives, because
  // speechStartedAt is ONE global that every speech_started overwrites while
  // transcriptions land late and out of order: noise at T1, a real question at
  // T2 that moves the clock, the answer created at T3, then T1's slow
  // transcription arrives, fails REAL_WORDS, and T3 > T2 declares the real
  // answer a ghost. That is the mid-word cutoff, and the resume below then
  // continues from the severed token.
  const isGhost = voice.responseActive
    && !voice.expectReply
    && (voice.responseStartedAt || 0) > (voice.speechStartedAt || 0);
  try {
    if (isGhost) {
      voice.dc.send(JSON.stringify({ type: "response.cancel" }));
      voice.dc.send(JSON.stringify({ type: "output_audio_buffer.clear" }));
    }
    if (userItemId) {           // the phantom turn always leaves the context
      voice.dc.send(JSON.stringify({ type: "conversation.item.delete", item_id: userItemId }));
    }
  } catch { /* channel closing: nothing to kill */ }
  if (isGhost) {
    voice.scrubNext = true;                   // response.done will delete its items
    const ghost = voice.agentLine && voice.agentLine.closest(".bubble");
    if (ghost) ghost.remove();                // never happened, don't show it
    voice.agentLine = null;
  }
  liftMuteGuard();
  // if a barge-in DID clip him mid-word, tell him to finish the thought
  // instead of leaving the sentence dead in the air
  if (isGhost && wasSpeaking && Date.now() - (voice.resumeAt || 0) > 2500) {
    voice.resumeAt = Date.now();
    requestResponse();          // gated: never races the response being cancelled
  }
}

function liftMuteGuard() {
  if (voice.muteGuard) {
    voice.muteGuard = false;
    if (voice.audioEl) voice.audioEl.muted = false;
  }
}

async function handleVoiceEvent(ev) {
  switch (ev.type) {
    case "error": {
      // the server tells us when we raced it; make that visible instead of
      // letting it show up only as Marcus dying mid-sentence
      const msg = ev.error?.message || ev.error?.code || "realtime error";
      console.warn("[realtime]", ev.error?.code, msg);
      voice.lastError = { code: ev.error?.code, msg, at: Date.now() };
      break;
    }
    case "input_audio_buffer.speech_started": {
      voice.speechStartedAt = Date.now();     // a turn began (real or the room)
      voice.userTalking = true;               // the heartbeat holds its tongue
      // talking over Marcus mid-sentence is the single loudest signal that a
      // turn was too long or too slow, so it rides along with that turn
      if (turn && voice.responseActive) turn.bargeIn = true;
      break;
    }
    case "input_audio_buffer.speech_stopped": {
      voice.userTalking = false;
      // THE moment the caller starts waiting, and the only honest start for
      // dead air. Measuring from transcription.completed logged NEGATIVE waits
      // (-2441ms), because transcription lands well after Marcus has already
      // begun answering: that clock times the transcriber, not the silence.
      if (!turn) turnStart();
      turn.userStoppedAt = Date.now();
      break;
    }
    case "response.created": {
      voice.responseActive = true;
      voice.responseStartedAt = Date.now();   // whose sentence is on the air
      // reply starting after a long quiet spell: hold the speaker until the
      // transcript proves it was a person, not the room (fail-open in 4s).
      // NEVER guard a reply the caller asked for (real turn / tool follow-up) —
      // muting those made Marcus "say nothing" after slow tool calls.
      if (!voice.expectReply && Date.now() - voice.lastActivity > IDLE_GUARD_MS && voice.audioEl) {
        voice.audioEl.muted = true;
        voice.muteGuard = true;
        setTimeout(liftMuteGuard, 4000);
      }
      break;
    }
    case "response.output_audio_transcript.delta": {
      voice.lastActivity = Date.now();        // agent mid-speech keeps the line open
      voice.lastAgentDelta = Date.now();      // barge-in repair: was he talking?
      if (!turn) turnStart();
      if (!turn.firstWordAt) turn.firstWordAt = Date.now();   // silence ends HERE
      // a fresh bubble mid-turn means a SECOND response (tool follow-up, or a
      // resume after a cutoff). Keep them separable instead of gluing the log
      // into "...if it's there.SPY is in a momentum tape..."
      if (!voice.agentLine && turn.agent) turn.agent += "\n";
      turn.agent += ev.delta;
      if (!voice.agentLine) {
        const b = bubble(`${current.name} (voice)`, "agent");
        voice.agentLine = b.querySelector(".md");
      }
      voice.agentLine.textContent += ev.delta;
      scroll();
      break;
    }
    case "conversation.item.input_audio_transcription.failed":
      killGhostTurn(ev.item_id, Date.now() - (voice.lastAgentDelta || 0) < 4000);
      break;
    case "conversation.item.input_audio_transcription.completed": {
      const words = (ev.transcript || "").trim();
      const speaking = Date.now() - (voice.lastAgentDelta || 0) < 4000;
      if (!REAL_WORDS.test(words)) {          // noise, breath, mic bump: not speech
        killGhostTurn(ev.item_id, speaking);
        break;
      }
      // music makes the transcriber HALLUCINATE short foreign-script frags
      // ("그게 딱" mid-Spanish-call): a short burst with zero Latin letters
      // in a Latin-script conversation is the room, not the caller
      if (!/[A-Za-z0-9À-ɏ]{2,}/.test(words) && words.length < 8) {
        killGhostTurn(ev.item_id, speaking);
        break;
      }
      voice.lastActivity = Date.now();        // ONLY real speech keeps the line open
      voice.expectReply = true;               // the caller asked — never mute the answer
      if (!turn) turnStart();
      turn.user = words;
      turn.userAt = Date.now();               // the clock on dead air starts here
      liftMuteGuard();
      const b = bubble("You (voice)", "user");
      b.querySelector(".md").textContent = words;
      // transcription lands async — keep it ABOVE the agent's in-flight reply
      const agentBubble = voice.agentLine && voice.agentLine.closest(".bubble");
      if (agentBubble) $("log").insertBefore(b, agentBubble);
      scroll();
      break;
    }
    case "response.done": {
      voice.agentLine = null;
      voice.expectReply = false;
      voice.responseActive = false;           // the floor is free again
      const items = ev.response?.output || [];
      if (voice.scrubNext || ev.response?.status === "cancelled") {
        voice.scrubNext = false;              // ghost reply: erase it from context
        turnFlush(true);                      // and from the log: it never happened
        for (const item of items) {
          if (item.id) {
            try {
              voice.dc.send(JSON.stringify({ type: "conversation.item.delete", item_id: item.id }));
            } catch { /* channel closing */ }
          }
        }
        break;                                // and never run its tool calls
      }
      // ALL tool outputs first, then exactly ONE response for the batch: a
      // turn that calls two tools (trade_recommendation + draw_levels) used
      // to fire two responses and the second cut the first off mid-word
      const calls = items.filter((i) => i.type === "function_call");
      for (const item of calls) await runVoiceTool(item);
      // a turn is not over while another response is still owed: both of these
      // branches keep talking, and splitting there would log half an answer
      if (calls.length) requestResponse();
      else if (voice.pendingResponse) { voice.pendingResponse = false; requestResponse(); }
      else turnFlush(false);                  // spoken and done: this turn is evidence
      break;
    }
  }
}

async function runVoiceTool(item) {
  const b = [...document.querySelectorAll(".bubble.agent")].pop();
  if (b) chip(b, "tool", `🔧 ${item.name}`);
  const toolStartedAt = Date.now();
  // did he talk through the wait, or leave the caller in silence? measured at
  // the FIRST tool only: that is the gap the COVER THE WAIT rule has to fill
  if (turn && turn.covered === null) turn.covered = !!turn.firstWordAt;
  let output;
  if (item.name === "draw_levels") {
    // chart-side tool: the line must appear the moment Marcus says the number
    try { output = JSON.stringify(drawCallerLevels(JSON.parse(item.arguments || "{}"))); }
    catch (e) { output = JSON.stringify({ error: String(e) }); }
  } else {
    const url = current.kind === "persona" ? `/tool/${current.id}` : `/tool/bridge/${current.id}`;
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: item.name, arguments: JSON.parse(item.arguments), session: sessionId }),
    }).then((r) => r.json());
    output = res.output;
  }
  if (turn) turn.tools.push({ name: item.name, ms: Date.now() - toolStartedAt });
  if (item.name === "trade_recommendation") tradeStickyFromPayload(output);
  // submit the result ONLY: the caller creates one response for the whole
  // batch, so two tools in a turn can never race two replies
  voice.dc.send(JSON.stringify({
    type: "conversation.item.create",
    item: { type: "function_call_output", call_id: item.call_id, output },
  }));
}

const LEVEL_COLORS = { green: "--green", red: "--red", accent: "--accent", dim: "--dim" };

/* ------------------------------------------------- the trade sticky ----
   Marcus's call, pinned in the conversation until the position is flat.
   "XSP 746p @ 2.30 · now" / "XSP 746p @ 2.30 · if a 15m thick closes 746.62" */

function renderTradeSticky(s) {
  const log = $("log");
  if (!log) return;
  let el = $("trade-sticky");
  if (!el) {
    el = document.createElement("div");
    el.id = "trade-sticky";
    // NO label here. On an options desk "the call" reads as a CALL option:
    // the contract line already says call or put, so anything else is noise.
    el.innerHTML = `<span class="tsk-dot"></span><span class="tsk-contract"></span>
      <span class="tsk-cond"></span>`;
    el.onclick = () => {
      const lv = el._levels;
      if (lv && lv.length && !el.classList.contains("done")) drawCallerLevels({ levels: lv });
    };
    // its OWN bar above the transcript, never floating inside it: as a
    // sticky child of the scroller it sat on top of Marcus's words
    log.parentNode.insertBefore(el, log);
  }
  el.className = s.kind === "call" ? "call" : s.kind === "put" ? "put" : "";
  if (s.done) el.classList.add("done");
  if (s.ready) el.classList.add("ready");     // the condition just came true
  el.querySelector(".tsk-contract").textContent = s.contract;
  el.querySelector(".tsk-cond").textContent = s.cond ? `· ${s.cond}` : "";
  el._levels = s.levels || [];
  el._watch = s.watch || null;                // ticker to re-check, if pending
}

function tradeStickyFromPayload(raw) {
  try {
    const p = typeof raw === "string" ? JSON.parse(raw) : raw;
    const x = p.execution;
    if (!x || !x.strike) return;
    const px = Number(x.entry_option_price_est);
    const cp = x.contract_plan || {};
    // THE PITCH: what the heartbeat marks from here on. Only a FILLABLE call
    // replaces it - a later "plan, nothing on yet" answer must not blind the
    // desk to a position the caller may already be holding from the last
    // fillable one. Analysis units (SPY strike), same as the engine prices in.
    if (cp.contracts_now > 0 && isFinite(px) && px > 0) {
      beat.pitch = { kind: x.kind, strike: Number(x.strike), entry: px,
                     expiry: x.expiry || "", add_level: cp.add_level || null };
    }
    const name = cp.contract_ticker && cp.contract
      ? `${cp.contract_ticker} ${cp.contract}`          // the desk notation: "XSP 748p"
      : `${p.ticker} ${Number(x.strike)}${x.kind[0]}`;
    // the expiry belongs IN the notation: an option without its date is not
    // a tradeable instruction ("XSP 608p 19/07 @ 2.30")
    const contract = [name, ddmm(x.expiry), isFinite(px) ? `@ ${px.toFixed(2)}` : ""]
      .filter(Boolean).join(" ");
    // The condition must always name a PRICE and say plainly whether this is
    // live or pending · "on confirmation only" told him nothing actionable.
    const tp = p.tape || {};
    const a = tp.action || {};
    // the CONTRACT side picks the line, and the line must say it serves that
    // side ("for") and sit ahead of the trade - the prose regex this replaces
    // grabbed whichever line mentioned CONFIRM, including the other trade's
    const want = x.kind === "call" ? "long" : "short";
    const lineFor = [a.up, a.down].find((l) => l && l.for === want
      && (x.kind === "call" ? l.dist > 0 : l.dist < 0));
    let cond = "take it now", ready = true, watch = null;
    if (a.stance === "conditional") {
      cond = lineFor
        ? `wait · 15m close ${x.kind === "call" ? "over" : "under"} ${lineFor.level}`
        : "wait · no trigger in reach yet";
      ready = false; watch = p.ticker;
    } else if (a.stance === "wait_pullback") {
      const vw = Number(tp.vwap);
      cond = `wait · pullback holding VWAP${isFinite(vw) ? " " + vw.toFixed(2) : ""}`;
      ready = false; watch = p.ticker;
    } else if (a.stance === "wait") {
      cond = "no setup yet · not a trade";
      ready = false; watch = p.ticker;
    }
    const levels = [
      x.entry_underlying && { price: x.entry_underlying, label: "entry", color: "accent" },
      x.tp50_underlying_est && { price: x.tp50_underlying_est, label: "trim +50%", color: "green" },
      x.thesis_reference && { price: x.thesis_reference, label: x.thesis_label || "thesis", color: "dim" },
      x.target && { price: x.target, label: "target", color: x.kind === "call" ? "green" : "red" },
    ].filter(Boolean);
    renderTradeSticky({ contract, cond, kind: x.kind, levels, ready, watch });
    const st = $("trade-sticky");
    if (st) st._contractKey = `${x.kind}:${x.strike}:${x.expiry}`;
    watchPendingTrade();
  } catch { /* the sticky is a bonus; the voice already said it */ }
}

/* A pending call is a PROMISE: the desk re-checks the tape and flips the bar
   to "take it now" the moment the trigger prints, instead of leaving a stale
   condition on screen that the caller has to re-ask about. */
let stickyTimer = null;
function watchPendingTrade() {
  clearInterval(stickyTimer);
  stickyTimer = setInterval(async () => {
    const el = $("trade-sticky");
    if (!el || !el._watch || el.classList.contains("done") || document.hidden) return;
    try {
      const d = await fetch(`/api/summary/${encodeURIComponent(el._watch)}`).then((r) => r.json());
      if (!d || !d.trade) return;
      const fx = (d.trade.execution || {});
      const key = `${fx.kind}:${fx.strike}:${fx.expiry}`;
      // same contract: full refresh (its condition may have armed). A
      // DIFFERENT contract stays out - the heartbeat announces new trades,
      // the sticky guards the one already pinned.
      if (!el._contractKey || el._contractKey === key) {
        el._contractKey = key;
        tradeStickyFromPayload(JSON.stringify(d.trade));
      }
    } catch { /* provider hiccup: next tick */ }
  }, 30000);
}

function tradeStickyFromTrade(t) {
  if (!t || !t.strike) return;
  const cond = t.status === "quoted" ? (t.quoted_px ? `@ ${t.quoted_px} · as quoted` : "as quoted")
    : t.status === "opened" ? `IN @ ${t.entry_px}`
    : t.status === "trimmed" ? "half off · runner rides"
    : t.status === "closed" ? `flat · ${fmtUsd(t.realized_usd)}` : t.status;
  renderTradeSticky({
    contract: [dockContract(t), ddmm(t.expiry)].filter(Boolean).join(" "),
    cond, kind: t.kind, done: t.status === "closed",
    // a real position outranks any pending watch: stop re-checking the tape
    ready: t.status === "opened" || t.status === "trimmed", watch: null,
    levels: [
      t.entry_underlying && { price: t.entry_underlying, label: "entry", color: "accent" },
      t.tp50_underlying && { price: t.tp50_underlying, label: "trim +50%", color: "green" },
      t.thesis_reference && { price: t.thesis_reference, label: "thesis", color: "dim" },
    ].filter(Boolean),
  });
}

function drawCallerLevels(args) {
  const levels = (args.levels || [])
    .filter((l) => l && typeof l.price === "number" && isFinite(l.price) && l.price > 0)
    .slice(0, 8)
    .map((l) => ({ price: l.price, label: String(l.label || "").slice(0, 28),
                   color: LEVEL_COLORS[l.color] ? l.color : "accent" }));
  const msg = { deskDrawLevels: { levels, clear: !!args.clear } };
  if (window.parent !== window) window.parent.postMessage(msg, "*");  // embed: the parent owns the chart
  dock.extraLevels = levels;
  dock.extraLevelsSym = dock.symbol;
  if (dock.chart) {
    if (dock.active) dockLevels(dock.active);
    else {
      const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
      dock.chart.setLevels(levels.map((l) => ({
        price: l.price, color: cssv(LEVEL_COLORS[l.color]), style: 2, title: l.label })));
    }
  }
  return { drawn: levels.length, cleared: !!args.clear };
}

/* --------------------------------------------------------------- wire ---- */

/* ------------------------------------------------------------ builder ---- */

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

const dock = { chart: null, symbol: null, chartSeq: 0, active: null, pip: null,
  quotesES: null, quotesSyms: "", eventsES: null };
const DOCK_SECS = 900;   // 15m: the SAME tape the dashboard chart shows AND
                         // the same bars the ENGINE reads (get_tape_read is
                         // 15m) — one chart identity everywhere
const DOCK_INTERVAL = `${DOCK_SECS / 60}m`;  // derived: fetch and identity
                                             // CANNOT diverge again (the dock
                                             // once fetched 5m under a 15m
                                             // identity; studies ran on a
                                             // mixed series)

function fmtUsd(x) {
  if (x === null || x === undefined) return "";
  return (x >= 0 ? "+$" : "−$") + Math.abs(x).toFixed(0);
}

/* "2026-07-19" -> "19/07": the expiry as a trader writes it */
function ddmm(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
  return m ? `${m[3]}/${m[2]}` : "";
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

/* ONE line per price. The trade pins its own entry/trim/thesis and Marcus's
   draw_levels re-sends the same numbers, so the chart grew "thesis 743.78"
   twice. First writer wins: the trade's own label is the canonical one. */
function dedupeLevels(levels) {
  const seen = new Set();
  return levels.filter((l) => {
    if (!l || !isFinite(l.price)) return false;
    const key = Number(l.price).toFixed(2);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function dockLevels(t) {
  if (!dock.chart) return;
  if (t.underlying && dock.symbol && t.underlying !== dock.symbol) return;  // chart not re-seeded yet
  const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  dock.chart.setLevels(dedupeLevels([
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
    ...(dock.extraLevels || []).map((l) => ({
      price: l.price, color: cssv(LEVEL_COLORS[l.color]) || "#7c8aff",
      style: 2, title: l.label,
    })),
  ].filter(Boolean)));
}

function dockMarkers(t) {
  if (!dock.chart) return;
  if (t.underlying && dock.symbol && t.underlying !== dock.symbol) return;  // chart not re-seeded yet
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
  const sym = underlying || "SPY";
  // a QQQ trade after a SPY one must re-seed the candles, or its levels and
  // markers land on the wrong instrument (and the tick guard starves it)
  if (!window.DeskChart || (dock.chart && dock.symbol === sym)) return;
  const seq = ++dock.chartSeq;
  try {
    const data = await fetch(`/api/bars/${sym}?interval=${DOCK_INTERVAL}&limit=400`)
      .then((r) => (r.ok ? r.json() : null));
    if (seq !== dock.chartSeq) return;                // a newer boot superseded this one
    if (!data || !(data.bars || []).length) return;   // text-only dock still works
    if (!dock.chart) dock.chart = DeskChart.create($("dock-chart"), { intervalSec: DOCK_SECS });
    if (dock.extraLevelsSym && dock.extraLevelsSym !== sym) dock.extraLevels = [];
    dock.chart.setData(data.bars, DOCK_SECS, { symbol: sym });
    dock.symbol = sym;
    dockQuotes();                                     // the stream follows the chart's symbol
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
      tradeStickyFromTrade(latest);           // the call survives a reload
      if (open) dockPnl(d.positions);
    }
    return;
  }
  if (d.type === "trade") {
    dock.active = d.trade;
    dockRender();
    dockChartBoot(d.trade.underlying);
    tradeStickyFromTrade(d.trade);
    const lines = {
      quoted: `Marcus pinned ${dockContract(d.trade)} ~${d.trade.quoted_px} · on the chart ↑`,
      opened: `you're IN ${dockContract(d.trade)} @ ${d.trade.entry_px} · monitoring P&L ↑`,
      trimmed: `half off @ ${d.trade.trim_px} · runner rides`,
      closed: `flat · ${fmtUsd(d.trade.realized_usd)} on the trade`,
    };
    // announce each trade lifecycle step ONCE: replays and reconnects
    // re-emit events, and the same quote must not stack up in the log
    const annKey = `${d.trade.id}:${d.event}`;
    if (lines[d.event] && dock.lastAnn !== annKey) {
      dock.lastAnn = annKey;
      divider(lines[d.event]);
    }
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
  dockQuotes();
}

function dockQuotes() {
  // SPY always rides along for the price chip; the dock chart's own symbol
  // joins so a QQQ/IWM trade chart still ticks live
  const syms = [...new Set(["SPY", dock.symbol].filter(Boolean))].join(",");
  if (dock.quotesES) {
    if (dock.quotesSyms === syms) return;
    dock.quotesES.close();
  }
  dock.quotesSyms = syms;
  dock.quotesES = new EventSource(`/api/stream/quotes?symbols=${syms}`);
  dock.quotesES.onmessage = (e) => {
    let d; try { d = JSON.parse(e.data); } catch { return; }
    if (d.type !== "quote") return;
    if (d.ticker === "SPY") {
      spyLive = true;
      paintSpy(d.price, [d.session, d.source].filter(Boolean).join(" · ") || "live");
    }
    if (dock.chart) {
      const t = d.ts ? Math.floor(Date.parse(d.ts) / 1000) : 0;
      dock.chart.applyTick(d.price, t, d.ticker);   // the chart's guard filters foreign ticks
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
