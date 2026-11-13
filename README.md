# Jarvis Agent

A personal AI assistant built as a production-style agentic system:
**LangGraph** multi-agent orchestration, **RAG** on **Postgres + pgvector**, **MCP** tools,
streaming **FastAPI**, with evals and tracing. Runs **fully local and free** by default (Ollama),
with the **Claude API** as an optional, switchable provider.

## Architecture (target)

```
UI (orb + voice)
   │ WebSocket: streamed tokens + agent events
FastAPI gateway
   │
LangGraph supervisor ── Postgres checkpointer (memory, human approval)
   ├─ Knowledge agent ── RAG: hybrid search (pgvector + full-text) → rerank → cited answer
   ├─ Research agent  ── MCP client → web / GitHub / filesystem MCP servers
   ├─ Action agent    ── MCP client → own MCP server (notes, tasks, calendar)
   └─ Long-term memory ── user facts in pgvector
Langfuse tracing · eval suite (golden set + RAGAS) · Docker Compose · CI
```

## Roadmap

| Step | Scope | Status |
|---|---|---|
| 1 | FastAPI + LangGraph agent, tools, streaming, memory, Claude/Ollama switch | ✅ |
| 2 | RAG: ingestion, chunking, hybrid search, reranking, citations | ⏳ |
| 3 | MCP: own server + client for external servers | ⏳ |
| 4 | Multi-agent supervisor + human-in-the-loop approval | ⏳ |
| 5 | Evals (golden set, RAGAS) + Langfuse tracing + CI gate | ⏳ |
| 6 | Orb/voice UI, demo video | ⏳ |

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
ollama pull qwen2.5:7b-instruct  # local LLM (free)
cp .env.example .env            # optional: switch provider / enable Postgres
docker compose up -d            # optional: Postgres + pgvector for persistent memory
cd src && ../.venv/bin/uvicorn jarvis.api:app --reload
```

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message":"What is 123 * 47?","thread_id":"demo"}'
```

WebSocket `/ws`: send `{"message": "...", "thread_id": "..."}`; receive
`token`, `tool_call`, `tool_result`, `done` or `error` events.

## Tests

```bash
.venv/bin/python -m pytest -q     # offline: a fake model replaces the LLM
```

## Design notes

- **Local-first, provider-agnostic**: one factory (`llm.py`) returns a local Ollama model (default) or Claude (`claude-opus-5-5`, adaptive
  thinking with configurable effort, server-side refusal fallback).
- **Dependency injection**: the graph takes its model and checkpointer as arguments, so tests run
  without network and the API picks in-memory or Postgres memory from config.
- **Safe tools**: the calculator parses an AST whitelist instead of calling `eval`.
- **Refusals** (`stop_reason == "refusal"`) are turned into a clear reply instead of an empty one.
