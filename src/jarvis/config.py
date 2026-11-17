"""Settings, read from environment variables or a .env file."""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    # .env is found from the repo root, whatever directory the app is started from.
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_prefix="JARVIS_", extra="ignore")

    # Which LLM to use: "ollama" (local, free, offline - default) or "anthropic" (Claude API, paid).
    llm_provider: Literal["anthropic", "ollama"] = "ollama"

    anthropic_model: str = "claude-opus-5-5"
    # low | medium | high | xhigh | max. Opus 5.5 defaults to medium if unset.
    anthropic_effort: str = "medium"

    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_base_url: str = "http://localhost:11434"
    # 0 = deterministic: reproducible evals and steadier tool calls (Ollama default is 0.8).
    ollama_temperature: float = 0.0

    # Postgres (with pgvector). Empty = memory in RAM only and no knowledge base.
    database_url: str = ""

    # MCP servers to connect (JSON file, relative to the repo root). Empty = none.
    mcp_config: str = "mcp_servers.json"

    # Tools that pause for the user's approval before running (human-in-the-loop).
    approval_tools: list[str] = ["add_task", "complete_task"]

    # Tracing: "phoenix" sends OpenTelemetry traces to a local Arize Phoenix
    # (docker compose --profile tracing up -d; UI at http://localhost:6006). "" = off.
    tracing: str = ""
    phoenix_endpoint: str = "http://localhost:6006/v1/traces"
    tracing_project: str = "jarvis"

    # RAG
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    rerank_model: str = "ms-marco-MiniLM-L-12-v2"  # FlashRank; empty = no reranking
    rag_candidates: int = 20   # hybrid-search candidates sent to the reranker
    rag_top_n: int = 5         # passages given to the LLM
    # Drop reranked passages below this relevance. Calibrated on the eval set: answerable
    # questions scored >= 0.03, unanswerable ones 0.0000, so 0.01 separates them.
    rag_min_score: float = 0.01

    system_prompt: str = (
        "You are Jarvis, a concise personal assistant. Use tools when they help. "
        "When passages from the user's knowledge base are provided, answer questions about "
        "the user's notes, documents or plans only from those passages, quote the exact "
        "facts (dates, numbers, names) and cite them like [1]. Do not add details that "
        "are not in the passages. If no passages are provided or they don't contain the "
        "answer, you may call search_knowledge with different words; if that also finds "
        "nothing, say you couldn't find it in the knowledge base. Never guess. "
        "Use the task tools for the user's to-do list, the file tools for files in their "
        "sandbox folder, and fetch for web pages they give you. If a tool returns an error, "
        "tell the user exactly that it failed; never claim an action happened unless a "
        "tool result confirms it."
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
