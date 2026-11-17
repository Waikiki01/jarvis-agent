# Jarvis Agent

[![CI](https://github.com/Waikiki01/jarvis-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Waikiki01/jarvis-agent/actions/workflows/ci.yml)

A personal AI assistant built as a production-style agentic system:
**LangGraph** multi-agent orchestration, **RAG** on **Postgres + pgvector**, **MCP** tools,
streaming **FastAPI**, with evals and tracing. Runs **fully local and free** by default (Ollama),
with the **Claude API** as an optional, switchable provider.

## Demo

![Jarvis demo: two agents, human approval, action complete](docs/demo.gif)

Full 45-second run (real local model, nothing staged): [`docs/demo.mp4`](docs/demo.mp4): a cited answer
from notes, an honest "couldn't find it", a research → tasks hand-off with approval, and a rejected action.

| Approval card | Action complete | Phone |
|---|---|---|
| ![approval](docs/img/approval.png) | ![done](docs/img/action_complete.png) | ![mobile](docs/img/mobile.png) |

## Architecture

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
Phoenix tracing (OpenTelemetry) · golden-set evals with a CI-style gate · Docker Compose · GitHub Actions
```

## Roadmap

| Step | Scope | Status |
|---|---|---|
| 1 | FastAPI + LangGraph agent, tools, streaming, memory, Claude/Ollama switch | ✅ |
| 2 | RAG: ingestion, chunking, hybrid search, reranking, citations | ✅ |
| 3 | MCP: own server + client for external servers | ✅ |
| 4 | Multi-agent supervisor + human-in-the-loop approval | ✅ |
| 5 | Evals (golden set, retrieval metrics, gate) + Phoenix tracing + CI | ✅ |
| 6 | Orb/voice UI, demo video | ✅ |

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
ollama pull qwen2.5:7b-instruct  # local LLM (free)
cp .env.example .env            # optional: switch provider / enable Postgres
docker compose up -d            # optional: Postgres + pgvector for persistent memory
.venv/bin/uvicorn jarvis.api:app --app-dir src --reload --reload-dir src
# Windows: tools/start-jarvis.sh is a one-click launcher (see "Desktop launcher" below)
# open http://localhost:8000  (the orb UI)
```

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message":"What is 123 * 47?","thread_id":"demo"}'
```

WebSocket `/ws`: send `{"message": "...", "thread_id": "..."}`; receive
`token`, `tool_call`, `tool_result`, `done` or `error` events.

### Desktop launcher (Windows + WSL)

`tools/start-jarvis.sh` starts Postgres, then the server, and opens the UI in the Windows
browser once `/health` answers (if Jarvis is already running it just opens the browser).
Make a desktop shortcut whose target is:

```
C:\Windows\System32\wsl.exe -d Ubuntu-24.04 --cd /home/<you>/work/jarvis-agent -- bash -l tools/start-jarvis.sh
```

Closing the window stops the server.

## Knowledge base (RAG)

```bash
.venv/bin/python -m jarvis.rag.ingest samples/      # md / txt / pdf; unchanged files are skipped
curl -s "localhost:8000/search?q=printer%20ip"      # debug view: score, vector rank, keyword rank
```

```
question ─▶ retrieve node (every turn, in code)
             ├─ embed (nomic-embed-text, "search_query:" prefix)
             ├─ hybrid search in Postgres: pgvector HNSW (cosine)  +  full-text (tsvector, GIN)
             │     merged with Reciprocal Rank Fusion: 1/(60+rank_vec) + 1/(60+rank_text)
             ├─ rerank top 20 with a local cross-encoder (FlashRank MiniLM, CPU)
             └─ drop passages below a relevance score ─▶ numbered passages [1] [2] ...
          ─▶ agent (LLM answers only from passages, cites [n]; may call search_knowledge again)
```

What made the difference (measured on the sample docs):

| Problem found | Fix | Effect |
|---|---|---|
| 7B model often **didn't call** the search tool | Retrieval runs in code before the LLM; the tool stays for follow-ups | Unanswered questions about the docs → answered |
| Keyword search ANDed all words ("time" not in text → no match) | OR the words, rank by `ts_rank_cd` | Keyword side matches again |
| Whole note in one 600-char chunk → reranker score 0.009 | Section-aware chunks with heading path `[Doc > Section]` | Same question scored 0.374 |
| Irrelevant passages invite made-up answers | Cross-encoder score cut-off | "What is my wife's birthday?" → "couldn't find it" |

## MCP (Model Context Protocol)

Jarvis is an **MCP client**, and the repo ships its own **MCP server**.

| Server | Source | Tools given to Jarvis |
|---|---|---|
| `tasks` | **this repo** (`jarvis.mcp_server.tasks`, Postgres) | `add_task`, `list_tasks`, `complete_task` + resource `tasks://open` + prompt `plan_my_day` |
| `filesystem` | `@modelcontextprotocol/server-filesystem` (pinned) | `read_text_file`, `list_directory`, `search_files`, **only inside `sandbox/`** |
| `fetch` | `mcp-server-fetch` | `fetch` (read a web page) |

Configured in [`mcp_servers.json`](mcp_servers.json). The client (`jarvis/mcp_client.py`):
- starts each server **once** and keeps the session open (not one process per tool call)
- gives the model only **allow-listed** tools (write/move/delete file tools are left out)
- **isolates failures**: a server that won't start is logged and skipped
- refuses **tool-name clashes** instead of silently replacing a tool
- `GET /health` lists the tools that actually loaded

Use the tasks server from any MCP client, e.g. Claude Desktop on Windows with the server in WSL
(`claude_desktop_config.json`):

```json
{"mcpServers": {"jarvis-tasks": {
  "command": "wsl",
  "args": ["-d", "Ubuntu-24.04", "--", "env", "-u", "PYTHONPATH",
           "JARVIS_DATABASE_URL=postgresql://jarvis:jarvis@localhost:5433/jarvis",
           "/home/<you>/work/jarvis-agent/.venv/bin/python", "-m", "jarvis.mcp_server.tasks"]
}}}
```

Or over HTTP: `python -m jarvis.mcp_server.tasks --http` → `http://127.0.0.1:8765/mcp`.

## Multi-agent supervisor + human approval

```
START → router → prepare → agent ⇄ tools → advance ─┬─▶ verify → END
                   ▲                                 │
                   └────────── next specialist ──────┘
```

| Specialist | Tools (only these) | Handles |
|---|---|---|
| `assistant` | `search_knowledge`, `calculator`, `current_time` + RAG passages | notes, documents, maths, chat |
| `tasks` | `add_task`, `list_tasks`, `complete_task` | to-do list |
| `research` | `read_text_file`, `list_directory`, `search_files`, `fetch` | sandbox files, web pages |

- **Router**: keyword fast path (no LLM, ~0 ms; no match → `assistant`). Only requests that
  span two specialists go to an LLM router with a JSON-schema answer, which orders them
  (e.g. `research` → `tasks` for "add the item on my shopping list as a task").
- **Hand-off**: a non-final specialist reports only the facts the next one needs; the last one
  writes the single answer the user sees.
- **Human-in-the-loop**: tools in `approval_tools` (`add_task`, `complete_task`) call LangGraph
  `interrupt()`. `/chat` returns `status: "needs_approval"` with the pending action;
  `POST /approve {"thread_id", "approved"}` resumes. All approvals are collected *before*
  any tool runs, because resuming re-executes the node (prevents double actions).
  A new message while an approval is pending rejects it first.
- **Verification in code**: if the final reply claims an action ("added", "marked"…) but no
  action tool succeeded this turn, the reply is replaced with the truth.

Found in live testing (7B local model) and fixed:

| Problem | Fix |
|---|---|
| After a tool error the model said *"I have manually marked the task as done"* | `verify` node replaces unconfirmed action claims |
| LLM router sent "what is my printer IP?" to `research` (15 s, wrong) | No keyword → `assistant` directly (3 s, correct) |
| Research agent used relative paths; filesystem server needs absolute | Sandbox path injected into its prompt |
| Two-step reply presented the shopping list as the to-do list | Hand-off notes hidden; final agent answers only from tool results |

## Evals

```bash
python evals/run.py            # 32 golden cases (incl. multi-turn) against the full stack, fresh jarvis_eval DB
python evals/run.py --only rag # one category or case id;  --offline skips web cases
```

[`evals/golden.yaml`](evals/golden.yaml) covers RAG answers + citations, refusing facts that
aren't in the notes, tools, tasks with approve/reject, files, web fetch, a two-agent request,
and a **prompt injection** hidden in a sandbox file. Checks are deterministic (expected facts,
citations, routing, which action was submitted for approval and with which arguments), plus
retrieval **hit@5** and **MRR** measured on the retriever alone. Thresholds in the YAML make it
a gate (exit code 1 on a miss). Report: [`evals/results/latest.md`](evals/results/latest.md).

**Result (local `qwen2.5:7b-instruct`, free): 85% → 100%** on the 27 single-turn cases; **97% (31/32)**
after adding 5 multi-turn cases (see UI section). Retrieval hit@5 100%, MRR 1.00, median latency
~1.1 s, identical on repeat runs. Every fix came from a failing case:

| Failing case | Root cause | Fix |
|---|---|---|
| `rag-tyres` | right passage scored 0.03, cut-off was a guessed 0.05 | **calibrated** on the eval: answerable ≥ 0.03, unanswerable 0.0000 → 0.01 |
| `research-fetch` | model asked `fetch(max_length=100)` | tool-argument policy in code |
| `multi-shopping-to-task` | empty hand-off note; tasks agent asked "proceed?" | pass tool findings on; tell the agent approval is automatic |
| `task-complete` | model **guessed** task #3 (passport was #1); approval showed only an id → user could approve the wrong task blind | precondition in code: `list_tasks` before `complete_task`; approval shows the task's title |
| flaky results between runs | Ollama default temperature 0.8 | temperature 0 → identical scores on repeat runs |
| `refuse-bank` | grader didn't accept "I don't have access…" | grader fixed (an eval bug, not a model bug) |

Baseline report kept in [`evals/results/baseline_2026-11-13.md`](evals/results/baseline_2026-11-13.md).

## Tracing

```bash
docker compose --profile tracing up -d          # Arize Phoenix, http://localhost:6006
JARVIS_TRACING=phoenix uvicorn --app-dir src jarvis.api:app
```

Every turn becomes an OpenTelemetry trace: router → prepare → agent → tools → verify, each LLM
call with its prompt, output and latency. Phoenix was chosen over self-hosted Langfuse because
it is one container and fully local; both speak OpenTelemetry.

## CI

GitHub Actions runs `ruff` and the test suite on every push, against a real pgvector service
container and real MCP stdio servers (LLM and embeddings are faked, so CI needs no GPU).
The LLM evals need Ollama and run locally (`python evals/run.py`).

## UI

`http://localhost:8000` serves the orb UI (plain HTML/JS/canvas, no build step, works offline):
- streams tokens and live agent activity chips (`research → tasks → ⚙ add_task → approved → ✓`)
- **approval card** with the exact action (task title, due date, or the task being completed);
  keys `Y` / `N`; orb turns gold and forms a "?"
- orb states follow the backend: thinking, speaking, action complete (green ✓), error
- the final bubble is the server's **verified** reply, which may replace streamed text
- **free voice**: push-to-talk (hold Space) via the browser's Web Speech API, spoken replies via
  `speechSynthesis`. No keys. Chrome sends speech audio to its own recognition service; for
  fully offline speech, swap in faster-whisper later.

### What the demo recording exposed

Single-turn evals were at 100%, but a scripted **multi-turn** demo failed: with the full history,
the 7B model **invented** "your wife's birthday is 25 February 1980 [1]" with fake sources, and a
two-agent request was lost. Fixes, all in code:

| Problem | Fix |
|---|---|
| Copied citation style from earlier turns with no passages | **Citation grounding check**: `[n]` must point at a passage retrieved this turn, else "couldn't find it" |
| Long history confused the current request | **History trimming**: last 3 exchanges, no old tool chatter or `[n]` markers |
| Hand-off note was a question back to the user | Hand-off = the previous agent's **raw tool results** |
| Next agent saw tools it doesn't have and faked success | Each agent sees only **its own** tool calls |
| Evals only tested fresh conversations | New **multi-turn** cases replay the demo session |

**Known limitation (left failing on purpose):** deep into a conversation, when blocked for guessing a
task number, the 7B model doesn't retry with the correct number. The user gets a safe answer
("I didn't change anything yet. Your open tasks are: ... Which one do you mean?"), but the eval
expects an approval request, so `conv-5` stays red rather than loosening the test.

## Tests

```bash
.venv/bin/python -m pytest -q     # fake LLM + fake embedder; RAG store tests use a throwaway
                                  # jarvis_test DB (skipped if docker Postgres isn't running)
```

## Design notes

- **Local-first, provider-agnostic**: one factory (`llm.py`) returns a local Ollama model (default) or Claude (`claude-opus-5-5`, adaptive
  thinking with configurable effort, server-side refusal fallback).
- **Dependency injection**: the graph takes its model and checkpointer as arguments, so tests run
  without network and the API picks in-memory or Postgres memory from config.
- **Safe tools**: the calculator parses an AST whitelist instead of calling `eval`.
- **Refusals** (`stop_reason == "refusal"`) are turned into a clear reply instead of an empty one.
