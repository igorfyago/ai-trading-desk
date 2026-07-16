# ai-trading-desk

**Six AI agents that staff an options trading desk — a deliberate ladder from a single LLM call to multi-path LangGraph workflows and real-time voice agents.**

Built with **LangChain 1.0** and **LangGraph 1.0** (Python), the **OpenAI Realtime API** for speech-to-speech voice, and grounded in a real system: my [options-flow-analytics](https://github.com/igorfyago/options-flow-analytics) service (Rust + PostgreSQL + Node.js), which computes live dealer gamma/delta exposure (GEX/DEX) from option chains. Every agent works out of the box on a bundled, auto-seeded SQLite mirror of that production schema — clone, add one API key, run.

Each level introduces exactly one new set of concepts on top of the previous one. The point of the repo is the *ladder*: read them in order and you've walked from "prompt" to "production agent system."

| # | Agent | Framework | New concepts | |
|---|---|---|---|---|
| 1 | [Market Brief](agents/01_market_brief/) | LangChain | structured output, prompt-as-persona | one LLM call, zero tools |
| 2 | [Text-to-SQL](agents/02_text_to_sql/) | LangChain | tool loop, self-correction on SQL errors, guardrails | English → verified SQL over the GEX database |
| 3 | [GEX Repo Interpreter](agents/03_repo_interpreter/) | LangChain | embeddings, agentic RAG, citations | answers "how does the code work?" over the real repo |
| 4 | [Research Graph](agents/04_research_graph/) | LangGraph | StateGraph, conditional edges, reducers, reflection loop | plan → multi-tool research → critic → revise |
| 5 | [Desk Analyst](agents/05_desk_analyst/) | LangGraph | routing, parallel sub-agents + join, critique loop, **human-in-the-loop interrupt**, checkpointing | regime-routed signal memo, human must approve |
| 6 | [Voice Agents](agents/06_voice/) | OpenAI Realtime | speech-to-speech, WebRTC, ephemeral credentials, server-side tools | AI receptionist + AI quoting agent you can *talk to* |

**+ [a live demo site](web/)** where anyone can voice-chat with the receptionist or get option quotes read to them by the quoting agent.

## The system at a glance

```mermaid
flowchart TB
    subgraph DATA [Shared data layer — common/]
        DB[(SQLite mirror of the\ngex_dex_snapshots schema\nauto-seeded demo data)]
        BS[Black-Scholes pricer\nexpected-move math]
        LIVE[optional: live Postgres\nfrom options-flow-analytics]
    end

    subgraph TEXT [Text agents — the complexity ladder]
        A1[1 · Market Brief\nstructured output] --> A2[2 · Text-to-SQL\ntool loop]
        A2 --> A3[3 · Repo RAG\nretrieval as a tool]
        A3 --> A4[4 · Research Graph\nloops + reflection]
        A4 --> A5[5 · Desk Analyst\nfan-out + HITL]
    end

    subgraph VOICE [Voice layer]
        W[Browser · WebRTC] <--> RT[OpenAI Realtime\ngpt-realtime-2.1]
        RT -- function calls --> API[FastAPI backend]
    end

    A2 & A4 & A5 --> DB
    API --> DB
    API --> BS
    DB -.swap.- LIVE
```

## Quickstart

```bash
git clone https://github.com/igorfyago/ai-trading-desk && cd ai-trading-desk
python -m venv .venv && .venv\Scripts\activate     # Windows (source .venv/bin/activate on mac/linux)
pip install -e ".[web]"
copy .env.example .env                              # add your OPENAI_API_KEY

python -m common.db                                 # build + seed the demo database
python agents/01_market_brief/main.py "Is SPY pinned into Friday opex?"
python agents/02_text_to_sql/main.py "Top 5 put walls by strength, with spot at the time"
python agents/04_research_graph/main.py "Compare dealer positioning on SPY vs QQQ"
python agents/05_desk_analyst/main.py SPY           # pauses for your approval before publishing
uvicorn web.server:app --reload                     # http://localhost:8000 → talk to the desk
```

Agent 3 additionally needs a checkout of [options-flow-analytics](https://github.com/igorfyago/options-flow-analytics) (`GEX_REPO_PATH` in `.env`), then `--index` once.

Set `LANGSMITH_API_KEY` + `LANGSMITH_TRACING=true` and every agent run is traced in [LangSmith](https://smith.langchain.com) for free — worth it from level 4 up, where the graphs get interesting.

## Why finance, why GEX?

Dealer positioning — who is long/short gamma, where the walls are, where hedging flows flip from dampening to amplifying — is the lens this whole stack is built around, and I'd already built the analytics service. These agents are the natural next layer: natural-language access (level 2), institutional knowledge (level 3), research automation (levels 4–5), and a phone line (level 6) for a system that already existed. AI agents are most convincing when they sit on top of something real.

## Design principles

- **One new idea per level.** Each README has a diagram and a "concepts introduced" table; the code is written to be read top-to-bottom.
- **The LLM is never trusted.** Read-only SQL with keyword guardrails, row caps, recursion limits, bounded loops, a risk-critic gate, and a human interrupt before anything is published. Voice tools execute server-side only.
- **Runs for anyone.** Deterministic synthetic seed data mirrors the production schema; `DATABASE_URL` swaps in the real feed without code changes.
- **Provider-agnostic where it's free.** All text agents go through `init_chat_model` — change `DESK_MODEL` in `.env` to swap OpenAI for Anthropic/Google/local.

## Repo layout

```
common/          shared model factory, demo DB (schema + seed), market math
agents/01..06    the ladder — each folder: main.py + README with diagram
web/             FastAPI token server + WebRTC voice demo page
```

---

*Demo data and indicative Black-Scholes quotes — nothing here is financial advice or an offer to trade.*
