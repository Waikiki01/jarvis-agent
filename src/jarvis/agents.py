"""Specialist agents and the supervisor's router.

Why specialists: each one sees only 3-4 tools and a focused prompt. Small local models
choose tools far more reliably from a short list than from all ten.

Routing is two-tier:
  1. keyword fast path (no LLM call, ~0 ms):
       one match  -> that specialist
       no match   -> the assistant (it has the knowledge base); in live tests the 7B
                     LLM router sent "what is my printer IP?" to research, so the LLM
                     is not trusted with the common single-domain case
  2. two or more matches -> LLM router with a JSON-schema constrained answer, which
     decides the order for multi-step requests
"""
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Specialist:
    name: str
    description: str
    prompt: str
    tools: tuple[str, ...]
    uses_rag: bool = False


SPECIALISTS: dict[str, Specialist] = {s.name: s for s in [
    Specialist(
        "assistant",
        "questions about the user's own notes, documents and plans (knowledge base), "
        "maths, the date or time, and general chat",
        "You answer questions. When knowledge-base passages are provided, answer only from "
        "them, quote exact facts and cite them like [1]. Never guess personal facts.",
        ("search_knowledge", "calculator", "current_time"),
        uses_rag=True,
    ),
    Specialist(
        "tasks",
        "the user's to-do list: add, list or complete tasks and reminders",
        "You manage the user's to-do list with the task tools. Convert relative dates "
        "(tomorrow, next Friday) to YYYY-MM-DD using today's date. To complete a task by "
        "name, call list_tasks first to find its number. Report exactly what the tools "
        "returned.",
        ("add_task", "list_tasks", "complete_task"),
    ),
    Specialist(
        "research",
        "reading files in the user's sandbox folder (e.g. the shopping list) and fetching "
        "web pages from a URL",
        "You read files and web pages with your tools and report what they say. The user's "
        "files are in the sandbox folder {sandbox} - always use absolute paths inside it. "
        "If you don't know the file name, call list_directory on that folder first.",
        ("read_text_file", "list_directory", "search_files", "fetch"),
    ),
]}

DEFAULT = "assistant"

_RULES = {
    "tasks": re.compile(r"\b(tasks?|to-?dos?|todo list|remind me|reminders?)\b", re.I),
    "research": re.compile(
        r"https?://|\b(fetch|website|web ?page|url|files?|folders?|sandbox|shopping list)\b", re.I),
}


def keyword_route(text: str) -> list[str]:
    return [name for name, rx in _RULES.items() if rx.search(text)]


class Route(BaseModel):
    """Specialists needed for the request, in the order they should run."""
    agents: list[Literal["assistant", "tasks", "research"]] = Field(min_length=1, max_length=2)


Router = Callable[[str], Awaitable[list[str]]]


def make_router(model: BaseChatModel) -> Router:
    # json_schema = native constrained output (Ollama format / Claude structured outputs);
    # no forced tool_choice, which newer Claude models reject.
    structured = model.with_structured_output(Route, method="json_schema")
    menu = "\n".join(f"- {s.name}: {s.description}" for s in SPECIALISTS.values())

    async def route(text: str) -> list[str]:
        hits = keyword_route(text)
        if len(hits) <= 1:
            return hits or [DEFAULT]
        try:
            r = await structured.ainvoke(
                f"Pick the specialist(s) for this request. If it needs two (e.g. read a file, "
                f"then add a task from it), list both in order.\n\nSpecialists:\n{menu}\n\n"
                f"Request: {text}")
            agents = list(dict.fromkeys(r.agents))  # dedupe, keep order
            return agents or [DEFAULT]
        except Exception as e:
            log.warning("router failed (%s); using %s", e, DEFAULT)
            return hits or [DEFAULT]

    return route


def fixed_router(*agents: str) -> Router:
    async def route(_text: str) -> list[str]:
        return list(agents)
    return route
