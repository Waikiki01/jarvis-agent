"""Chat model factory: one switch between the Claude API and local Ollama."""
from langchain_core.language_models import BaseChatModel

from .config import Settings, get_settings


def get_chat_model(settings: Settings | None = None) -> BaseChatModel:
    s = settings or get_settings()

    if s.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # Notes for claude-opus-5-5: thinking is always on (adaptive), effort is the
        # depth control, no temperature, no forced tool_choice. `fallbacks: "default"`
        # lets the API retry a safety refusal on a suitable model server-side.
        return ChatAnthropic(
            model=s.anthropic_model,
            max_tokens=16000,
            output_config={"effort": s.anthropic_effort},
            betas=["server-side-fallback-2026-07-01"],
            model_kwargs={"fallbacks": "default"},
        )

    if s.llm_provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=s.ollama_model, base_url=s.ollama_base_url)

    raise ValueError(f"unknown llm_provider: {s.llm_provider}")
