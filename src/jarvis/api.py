"""FastAPI gateway: REST for simple calls, WebSocket for streamed tokens and agent events.

Run:  uvicorn jarvis.api:app --reload
"""
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from .config import get_settings
from .graph import build_graph
from .llm import get_chat_model
from .tools import get_tools


class ChatIn(BaseModel):
    message: str
    thread_id: str = "default"


class ChatOut(BaseModel):
    reply: str
    thread_id: str


def text_of(content) -> str:
    """Text from a message's content (a string, or a list of content blocks)."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _retriever():
    from .rag import get_retriever

    return get_retriever()


def create_app(model=None) -> FastAPI:
    """`model` is injectable for tests; by default it comes from settings."""
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            if settings.database_url:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                saver = await stack.enter_async_context(
                    AsyncPostgresSaver.from_conn_string(settings.database_url))
                await saver.setup()
                from .rag.store import VectorStore

                VectorStore(settings.database_url, settings.embed_dim).ensure_schema()
            else:
                saver = InMemorySaver()
            tools = get_tools(settings)
            if settings.mcp_config:
                from .mcp_client import open_mcp_tools

                tools += await open_mcp_tools(stack, settings.mcp_config, {t.name for t in tools},
                                              env={"database_url": settings.database_url})
            app.state.tools = [t.name for t in tools]
            app.state.graph = build_graph(
                model or get_chat_model(settings), saver, settings.system_prompt,
                tools=tools,
                retriever=_retriever() if settings.database_url else None)
            app.state.memory = "postgres" if settings.database_url else "in-memory"
            yield

    app = FastAPI(title="Jarvis Agent", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"ok": True, "provider": settings.llm_provider, "memory": app.state.memory,
                "tools": app.state.tools}

    @app.get("/search")
    def search(q: str):
        """Debug view of retrieval: final ranking plus each hit's vector/text rank."""
        from .rag import get_retriever

        if not settings.database_url:
            return {"error": "no database configured"}
        return [{"citation": h.citation, "score": round(h.score, 4), "vec_rank": h.vec_rank,
                 "txt_rank": h.txt_rank, "text": h.content[:300]}
                for h in get_retriever().search(q)]

    @app.post("/chat", response_model=ChatOut)
    async def chat(body: ChatIn):
        cfg = {"configurable": {"thread_id": body.thread_id}}
        out = await app.state.graph.ainvoke({"messages": [HumanMessage(body.message)]}, cfg)
        return ChatOut(reply=text_of(out["messages"][-1].content), thread_id=body.thread_id)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        """Client sends {"message", "thread_id"}; server streams events:
        token | tool_call | tool_result | done | error."""
        await socket.accept()
        try:
            while True:
                body = ChatIn(**await socket.receive_json())
                cfg = {"configurable": {"thread_id": body.thread_id}}
                try:
                    async for msg, _meta in app.state.graph.astream(
                        {"messages": [HumanMessage(body.message)]}, cfg, stream_mode="messages"
                    ):
                        if isinstance(msg, AIMessageChunk):
                            if t := text_of(msg.content):
                                await socket.send_json({"type": "token", "text": t})
                            for tc in msg.tool_call_chunks or []:
                                if tc.get("name"):
                                    await socket.send_json({"type": "tool_call", "name": tc["name"]})
                        elif isinstance(msg, ToolMessage):
                            await socket.send_json(
                                {"type": "tool_result", "name": msg.name, "content": str(msg.content)[:500]})
                    await socket.send_json({"type": "done"})
                except Exception as e:  # keep the socket open after a failed turn
                    await socket.send_json({"type": "error", "message": str(e)})
        except WebSocketDisconnect:
            pass

    return app


app = create_app()
