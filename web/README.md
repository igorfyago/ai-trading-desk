# The web app — one conversation, six agents, text + voice

Single-page app (no build step) + FastAPI backend. Users pick an agent from the ladder, chat with streaming answers and live tool/node chips, approve the Desk Analyst's memos with buttons, and press the mic to *talk* to any agent — voice-native personas directly, text agents through the Realtime **voice bridge**.

## Run

```bash
pip install -e ".[web]"
uvicorn web.server:app --reload      # http://localhost:8000
```

## Endpoints

| Route | What |
|---|---|
| `GET /` | the app |
| `GET /agents` | agent + persona catalog for the sidebar |
| `POST /chat/{agent_id}` | NDJSON stream: `node`, `tool`, `token`, `interrupt`, `final`, `error` events |
| `POST /chat/analyst/resume` | continue the Desk Analyst after approve / revise / reject |
| `POST /session/{persona}` | ephemeral Realtime secret for `riley` / `quinn` / `marcus` |
| `POST /session/bridge/{agent_id}` | ephemeral secret for the voice bridge onto a text agent |
| `POST /tool/{persona}` · `POST /tool/bridge/{agent_id}` | server-side execution of Realtime function calls |

Security posture: the OpenAI key never reaches the browser (per-session ephemeral secrets, persona instructions and tool schemas baked in at mint time); all tool code and the database stay server-side; SQL access is read-only with keyword guardrails.

## Observability

With `LANGSMITH_TRACING=true`, every chat run is traced under `LANGSMITH_PROJECT` with tags (`web-ui`, agent id) and session metadata; voice tool calls are wrapped with `@traceable`. Open [smith.langchain.com](https://smith.langchain.com) → project → filter by tag.

## Hosting plan (AWS)

Phase 1 (same pattern as options-flow-analytics): one small EC2 instance, `uvicorn` behind Caddy/nginx for TLS (WebRTC mic access requires HTTPS), `OPENAI_API_KEY` in SSM Parameter Store, SQLite on the instance. Phase 2 (managed): ECS Fargate + ALB + ACM certificate, secrets from Secrets Manager, RDS Postgres via `DATABASE_URL`. Before sharing publicly: add a rate limit (`slowapi`) on `/session/*` and a spending cap on the OpenAI project key.
