# Agent 6 — Voice Agents (AI Receptionist + AI Quoting Agent)

**Complexity level: 6/6 — real-time speech-to-speech agents with server-side tool execution.**

Two voice agents staff the desk's phone line, built on the **OpenAI Realtime API** (`gpt-realtime-2.1`, speech-to-speech — no STT→LLM→TTS pipeline, so it handles interruptions, hesitations and tone natively):

- **Riley — AI Receptionist** (`voice: marin`): answers questions about the desk, gives the current positioning read on a ticker in plain words, and **books callbacks** (a real database write) after collecting name/contact/topic.
- **Marcus — AI Quoting Agent** (`voice: cedar`): quotes options by voice — *"SPY 620 calls, a week out"* → Black-Scholes price, delta, expected move — with a trader's cadence, confirming numbers back before quoting.

The interesting engineering is **where things run**. Audio flows browser ↔ OpenAI directly over WebRTC (lowest latency). But the model's *function calls* are forwarded by the browser to our FastAPI backend, executed against the same `common/` market library and SQLite DB the text agents use, and the result is sent back over the data channel. The API key never reaches the browser — the server mints a **one-session ephemeral client secret** with the persona's instructions and tool schemas baked in at mint time, so the client can't tamper with the prompt either.

## How it works

```mermaid
sequenceDiagram
    participant B as Browser (mic + speaker)
    participant S as FastAPI backend
    participant O as OpenAI Realtime (WebRTC)
    B->>S: POST /session/quoting
    S->>O: POST /v1/realtime/client_secrets<br/>(instructions + tools + voice, real API key)
    O-->>S: ephemeral client secret
    S-->>B: client secret (single-session)
    B->>O: WebRTC SDP offer → answer (audio up/down + data channel)
    Note over B,O: caller talks; model answers in speech,<br/>interruptible mid-sentence
    O-->>B: response.done → function_call: quote_option({...})
    B->>S: POST /tool/quoting {name, arguments}
    S->>S: Black-Scholes on the desk DB (server-side)
    S-->>B: {output}
    B->>O: function_call_output + response.create
    O-->>B: "the 620 calls are going about four-eighty, forty-two delta…"
```

## Run it

```bash
pip install -e ".[web]"
uvicorn web.server:app --reload
# open http://localhost:8000, pick an agent, allow the mic, talk
```

Try, with your voice:
- *"Hey, what do you guys actually do?"* → Riley explains the desk
- *"What's the read on QQQ today?"* → `desk_status` call → plain-English regime
- *"Book me a call with an analyst about hedging, I'm Igor, igor@example.com"* → DB write
- *"Marcus, price me SPY 620 calls, five days out"* → `market_context` → `quote_option`
- interrupt the answer mid-sentence — the model stops and adapts

## Concepts introduced (on top of level 5)

| Concept | Where |
|---|---|
| Speech-to-speech (no pipeline) | `gpt-realtime-2.1` session |
| Ephemeral credentials | `POST /v1/realtime/client_secrets` in `web/server.py` |
| WebRTC audio + data channel | `web/static/index.html` |
| Server-side tool execution for a client-side model | `POST /tool/{persona}` round-trip |
| Voice UX prompting (cadence, no-JSON-aloud, confirmation) | `personas.py` instructions |
