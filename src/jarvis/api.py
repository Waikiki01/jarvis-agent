"""FastAPI gateway: REST for simple calls, WebSocket for streamed tokens and agent events.

Run:  uvicorn --app-dir src jarvis.api:app --reload

Human-in-the-loop: when an action needs approval the turn pauses and the reply has
status "needs_approval" plus the pending action; POST /approve resumes it.
"""
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel

from .agents import Router
from .config import get_settings
from .graph import current_turn, text_of
from .runtime import build_runtime


class ChatIn(BaseModel):
    message: str
    thread_id: str = "default"


class ApproveIn(BaseModel):
    thread_id: str = "default"
    approved: bool


class ChatOut(BaseModel):
    thread_id: str
    status: Literal["done", "needs_approval"]
    reply: str = ""
    approval: dict | None = None   # {"tool": ..., "args": {...}} when status=needs_approval
    agents: list[str] = []         # specialists that handled the turn


def _retriever():
    from .rag import get_retriever

    return get_retriever()


def create_app(model=None, router: Router | None = None) -> FastAPI:
    """`model` and `router` are injectable for tests; by default they come from settings."""
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            rt = await build_runtime(stack, settings, model=model, router=router)
            app.state.graph, app.state.tools, app.state.memory = rt.graph, rt.tools, rt.memory
            yield

    app = FastAPI(title="Jarvis Agent", lifespan=lifespan)

    def cfg(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    async def pending(thread_id: str) -> dict | None:
        snap = await app.state.graph.aget_state(cfg(thread_id))
        return next((i.value for t in snap.tasks for i in t.interrupts), None)

    async def result(thread_id: str) -> ChatOut:
        snap = await app.state.graph.aget_state(cfg(thread_id))
        if action := next((i.value for t in snap.tasks for i in t.interrupts), None):
            return ChatOut(thread_id=thread_id, status="needs_approval", approval=action,
                           agents=snap.values.get("plan", []))
        # The final answer only: earlier specialists' messages are hand-off notes.
        turn = current_turn(snap.values.get("messages", []))
        final = next((m for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), None)
        return ChatOut(thread_id=thread_id, status="done", reply=text_of(final.content) if final else "",
                       agents=snap.values.get("plan", []))

    async def start_turn(thread_id: str, message: str):
        """Input for a new user message. A still-pending approval is rejected first, so a
        half-finished action never runs later by surprise."""
        if await pending(thread_id):
            await app.state.graph.ainvoke(Command(resume={"approved": False}), cfg(thread_id))
        return {"messages": [HumanMessage(message)]}

    @app.get("/health")
    async def health():
        return {"ok": True, "provider": settings.llm_provider, "memory": app.state.memory,
                "tools": app.state.tools, "approval_tools": settings.approval_tools}

    @app.get("/search")
    def search(q: str):
        """Debug view of retrieval: final ranking plus each hit's vector/text rank."""
        if not settings.database_url:
            return {"error": "no database configured"}
        return [{"citation": h.citation, "score": round(h.score, 4), "vec_rank": h.vec_rank,
                 "txt_rank": h.txt_rank, "text": h.content[:300]}
                for h in _retriever().search(q)]

    @app.post("/chat", response_model=ChatOut)
    async def chat(body: ChatIn):
        await app.state.graph.ainvoke(await start_turn(body.thread_id, body.message), cfg(body.thread_id))
        return await result(body.thread_id)

    @app.post("/approve", response_model=ChatOut)
    async def approve(body: ApproveIn):
        if not await pending(body.thread_id):
            return await result(body.thread_id)
        await app.state.graph.ainvoke(Command(resume={"approved": body.approved}), cfg(body.thread_id))
        return await result(body.thread_id)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        """Client sends {"message", "thread_id"} or {"approved": bool, "thread_id"}.
        Server streams: route | token | tool_call | tool_result, then
        approval_required {approval} or done {reply, agents}; error on failure."""
        await socket.accept()
        try:
            while True:
                data = await socket.receive_json()
                thread_id = data.get("thread_id", "default")
                try:
                    if "approved" in data:
                        inp = Command(resume={"approved": bool(data["approved"])})
                    else:
                        inp = await start_turn(thread_id, data["message"])
                    async for mode, chunk in app.state.graph.astream(
                            inp, cfg(thread_id), stream_mode=["messages", "updates"]):
                        if mode == "updates":
                            if "router" in chunk:
                                await socket.send_json({"type": "route", "agents": chunk["router"]["plan"]})
                            continue
                        msg, meta = chunk
                        if meta.get("langgraph_node") != "agent" and not isinstance(msg, ToolMessage):
                            continue  # e.g. the router's JSON
                        if isinstance(msg, AIMessageChunk):
                            if t := text_of(msg.content):
                                await socket.send_json({"type": "token", "text": t})
                            for tc in msg.tool_call_chunks or []:
                                if tc.get("name"):
                                    await socket.send_json({"type": "tool_call", "name": tc["name"]})
                        elif isinstance(msg, ToolMessage):
                            await socket.send_json(
                                {"type": "tool_result", "name": msg.name, "content": str(msg.content)[:500]})
                    out = await result(thread_id)
                    if out.status == "needs_approval":
                        await socket.send_json({"type": "approval_required", "approval": out.approval})
                    else:  # final, verified text (may differ from the streamed tokens)
                        await socket.send_json({"type": "done", "reply": out.reply, "agents": out.agents})
                except Exception as e:  # keep the socket open after a failed turn
                    await socket.send_json({"type": "error", "message": str(e)})
        except WebSocketDisconnect:
            pass

    return app


app = create_app()
