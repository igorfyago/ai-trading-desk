# Recruiter Demo — talk to the desk from a browser

A single-page WebRTC client + FastAPI backend that lets anyone (yes, recruiters) have a live voice conversation with the desk's AI receptionist or quoting agent. See [agents/06_voice](../agents/06_voice/README.md) for the architecture.

## Local

```bash
pip install -e ".[web]"
uvicorn web.server:app --reload
# http://localhost:8000
```

## Deploy (so people can try it from a link)

Any host that runs a Python process works — the app is one uvicorn process, SQLite included:

- **Render / Railway / Fly.io**: start command `uvicorn web.server:app --host 0.0.0.0 --port $PORT`, set `OPENAI_API_KEY` as a secret. WebRTC needs HTTPS for mic access — all three give you TLS by default.
- Cost control: sessions use ephemeral secrets minted per click; add a simple rate limit (e.g. `slowapi`) before sharing the link widely, and set a spending cap on the OpenAI project key.

## Endpoints

| Route | What |
|---|---|
| `GET /` | the demo page |
| `POST /session/{persona}` | mints a one-session Realtime client secret (`receptionist` or `quoting`) |
| `POST /tool/{persona}` | executes the model's function calls server-side |
