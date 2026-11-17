"""Builds the full Jarvis graph (memory, tools, MCP servers, RAG, tracing).

Shared by the API and the eval runner, so evals test exactly what the API serves.
"""
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass

from langgraph.checkpoint.memory import InMemorySaver

from .agents import Router
from .config import Settings
from .graph import build_graph
from .llm import get_chat_model
from .mcp_client import ROOT, open_mcp_tools
from .tools import get_tools

log = logging.getLogger(__name__)
_tracing_on = False


@dataclass
class Runtime:
    graph: object
    tools: list[str]
    memory: str


def setup_tracing(settings: Settings) -> None:
    """OpenTelemetry tracing of every LangChain/LangGraph step to a local Phoenix UI."""
    global _tracing_on
    if _tracing_on or settings.tracing != "phoenix":
        return
    try:
        from phoenix.otel import register

        register(project_name=settings.tracing_project, endpoint=settings.phoenix_endpoint,
                 auto_instrument=True, batch=True, verbose=False)
        _tracing_on = True
        log.info("tracing to Phoenix at %s", settings.phoenix_endpoint)
    except Exception as e:  # tracing must never break the app
        log.warning("tracing disabled: %s", e)


async def build_runtime(stack: AsyncExitStack, settings: Settings,
                        model=None, router: Router | None = None) -> Runtime:
    setup_tracing(settings)
    if settings.database_url:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        from .rag import get_retriever
        from .rag.store import VectorStore

        saver = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(settings.database_url))
        await saver.setup()
        VectorStore(settings.database_url, settings.embed_dim).ensure_schema()
        from .users import ensure_schema as ensure_users
        ensure_users(settings.database_url)
        retriever = get_retriever()
    else:
        saver, retriever = InMemorySaver(), None

    tools = get_tools(settings)
    if settings.mcp_config:
        tools += await open_mcp_tools(stack, settings.mcp_config, {t.name for t in tools},
                                      env={"database_url": settings.database_url})

    graph = build_graph(
        model or get_chat_model(settings), saver, settings.system_prompt,
        tools=tools, router=router, retriever=retriever,
        approval_tools=set(settings.approval_tools), sandbox_dir=str(ROOT / "sandbox"))
    return Runtime(graph, [t.name for t in tools], "postgres" if settings.database_url else "in-memory")
