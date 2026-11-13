"""Settings, read from environment variables or a .env file."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="JARVIS_", extra="ignore")

    # Which LLM to use: "ollama" (local, free, offline - default) or "anthropic" (Claude API, paid).
    llm_provider: Literal["anthropic", "ollama"] = "ollama"

    anthropic_model: str = "claude-opus-5-5"
    # low | medium | high | xhigh | max. Opus 5.5 defaults to medium if unset.
    anthropic_effort: str = "medium"

    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_base_url: str = "http://localhost:11434"

    # Postgres (with pgvector). Empty = keep conversation memory in RAM only.
    database_url: str = ""

    system_prompt: str = (
        "You are Jarvis, a concise personal assistant. Use tools when they help. "
        "If you are not sure, say so instead of guessing."
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
