"""FastAPI gateway: REST for simple calls, WebSocket for streamed tokens and agent events.

Run:  uvicorn --app-dir src jarvis.api:app --reload

Human-in-the-loop: when an action needs approval the turn pauses and the reply has
status "needs_approval" plus the pending action; POST /approve resumes it.

Auth (JARVIS_AUTH_ENABLED=true): REST calls send `Authorization: Bearer <token>`; the
WebSocket's first message is {"type": "auth", "token": ...} (not a URL parameter, which
would end up in access logs). Each user's conversations, tasks, notes and sandbox files are
separate. With auth off everything runs as the single user "local".
"""
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel

from . import stt, users
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

    if settings.auth_enabled and not settings.database_url:
        raise RuntimeError("JARVIS_AUTH_ENABLED needs JARVIS_DATABASE_URL (users live in Postgres)")

    app = FastAPI(title="Jarvis Agent", lifespan=lifespan)

    async def user_for(token: str | None) -> str | None:
        if not settings.auth_enabled:
            return users.LOCAL
        return await run_in_threadpool(users.authenticate, settings.database_url, token)

    async def current_user(authorization: str | None = Header(None)) -> str:
        token = authorization.removeprefix("Bearer ").strip() if authorization else None
        if user := await user_for(token):
            return user
        raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})

    def cfg(thread_id: str, user: str = users.LOCAL) -> dict:
        # Thread ids are namespaced per user: another user's id never reaches their history.
        tid = thread_id if user == users.LOCAL else f"{user}:{thread_id}"
        return {"configurable": {"thread_id": tid, "user": user}}

    async def pending(thread_id: str, user: str) -> dict | None:
        snap = await app.state.graph.aget_state(cfg(thread_id, user))
        return next((i.value for t in snap.tasks for i in t.interrupts), None)

    async def result(thread_id: str, user: str) -> ChatOut:
        snap = await app.state.graph.aget_state(cfg(thread_id, user))
        if action := next((i.value for t in snap.tasks for i in t.interrupts), None):
            return ChatOut(thread_id=thread_id, status="needs_approval", approval=action,
                           agents=snap.values.get("plan", []))
        # The final answer only: earlier specialists' messages are hand-off notes.
        turn = current_turn(snap.values.get("messages", []))
        final = next((m for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), None)
        return ChatOut(thread_id=thread_id, status="done", reply=text_of(final.content) if final else "",
                       agents=snap.values.get("plan", []))

    async def start_turn(thread_id: str, message: str, user: str):
        """Input for a new user message. A still-pending approval is rejected first, so a
        half-finished action never runs later by surprise."""
        if await pending(thread_id, user):
            await app.state.graph.ainvoke(Command(resume={"approved": False}), cfg(thread_id, user))
        return {"messages": [HumanMessage(message)]}

    @app.get("/health")
    async def health():
        return {"ok": True, "provider": settings.llm_provider, "memory": app.state.memory,
                "tools": app.state.tools, "approval_tools": settings.approval_tools,
                "stt": "whisper" if stt.available() else "browser", "auth": settings.auth_enabled}

    @app.get("/me")
    async def me(user: str = Depends(current_user)):
        return {"user": user}

    @app.post("/transcribe")
    async def transcribe(request: Request, _user: str = Depends(current_user)):
        """Raw audio body (e.g. audio/webm from MediaRecorder) -> {"text": ...}, offline."""
        if not stt.available():
            raise HTTPException(501, "offline speech-to-text not installed (pip install -e '.[voice]')")
        audio = await request.body()
        if not audio:
            raise HTTPException(400, "empty audio")
        if len(audio) > stt.MAX_AUDIO_BYTES:
            raise HTTPException(413, "audio too long")
        try:
            return {"text": await run_in_threadpool(stt.transcribe, audio)}
        except Exception as e:  # undecodable audio etc.
            raise HTTPException(422, f"could not transcribe: {e}") from e

    @app.get("/search")
    def search(q: str, user: str = Depends(current_user)):
        """Debug view of retrieval: final ranking plus each hit's vector/text rank."""
        if not settings.database_url:
            return {"error": "no database configured"}
        return [{"citation": h.citation, "score": round(h.score, 4), "vec_rank": h.vec_rank,
                 "txt_rank": h.txt_rank, "text": h.content[:300]}
                for h in _retriever().search(q, user)]

    @app.post("/chat", response_model=ChatOut)
    async def chat(body: ChatIn, user: str = Depends(current_user)):
        await app.state.graph.ainvoke(await start_turn(body.thread_id, body.message, user),
                                      cfg(body.thread_id, user))
        return await result(body.thread_id, user)

    @app.post("/approve", response_model=ChatOut)
    async def approve(body: ApproveIn, user: str = Depends(current_user)):
        if not await pending(body.thread_id, user):
            return await result(body.thread_id, user)
        await app.state.graph.ainvoke(Command(resume={"approved": body.approved}), cfg(body.thread_id, user))
        return await result(body.thread_id, user)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        """With auth on, the first client message is {"type": "auth", "token"}; the server
        answers {"type": "auth_ok", "user"} or closes with code 4401.
        Client sends {"message", "thread_id"} or {"approved": bool, "thread_id"}.
        Server streams: route | token | tool_call | tool_result, then
        approval_required {approval} or done {reply, agents}; error on failure."""
        await socket.accept()
        try:
            user = users.LOCAL
            if settings.auth_enabled:
                first = await socket.receive_json()
                user = await user_for(first.get("token") if first.get("type") == "auth" else None)
                if not user:
                    await socket.close(code=4401, reason="invalid token")
                    return
                await socket.send_json({"type": "auth_ok", "user": user})
            while True:
                data = await socket.receive_json()
                thread_id = data.get("thread_id", "default")
                try:
                    if "approved" in data:
                        inp = Command(resume={"approved": bool(data["approved"])})
                    else:
                        inp = await start_turn(thread_id, data["message"], user)
                    async for mode, chunk in app.state.graph.astream(
                            inp, cfg(thread_id, user), stream_mode=["messages", "updates"]):
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
                    out = await result(thread_id, user)
                    if out.status == "needs_approval":
                        await socket.send_json({"type": "approval_required", "approval": out.approval})
                    else:  # final, verified text (may differ from the streamed tokens)
                        await socket.send_json({"type": "done", "reply": out.reply, "agents": out.agents})
                except Exception as e:  # keep the socket open after a failed turn
                    await socket.send_json({"type": "error", "message": str(e)})
        except WebSocketDisconnect:
            pass

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():  # browsers ask for /favicon.ico even when the page names another icon
        return FileResponse(Path(__file__).parent / "ui" / "favicon.svg", media_type="image/svg+xml")

    # The orb UI (static files). Mounted last so the API routes above take precedence.
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "ui", html=True), name="ui")
    return app


app = create_app()
