"""Offline tests: a fake chat model stands in for Claude / Ollama."""
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from jarvis.agents import fixed_router, keyword_route
from jarvis.api import create_app
from jarvis.graph import REFUSAL_REPLY, build_graph
from jarvis.rag.store import Hit
from jarvis.tools import TOOLS, calculator


class FakeModel(GenericFakeChatModel):
    """GenericFakeChatModel that accepts bind_tools and records what it was sent."""
    seen: list = []
    bound_tools: list = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tools.append(sorted(getattr(t, "name", getattr(t, "__name__", "?")) for t in tools))
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def fake(*replies) -> FakeModel:
    return FakeModel(messages=iter(replies), seen=[], bound_tools=[])


ADDED: list[str] = []


@tool
def add_task(title: str) -> str:
    """Add a task (test double)."""
    ADDED.append(title)
    return f"added #{len(ADDED)} {title}"


@tool
def list_tasks() -> str:
    """List tasks (test double)."""
    return "\n".join(ADDED) or "No tasks."


ALL_TOOLS = [*TOOLS, add_task, list_tasks]
cfg = {"configurable": {"thread_id": "t1"}}


def graph(model, *agents, **kw):
    kw.setdefault("tools", ALL_TOOLS)
    return build_graph(model, InMemorySaver(), router=fixed_router(*(agents or ["assistant"])), **kw)


def call(name, args, id="c1"):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": id}])


@pytest.fixture(autouse=True)
def reset_added():
    ADDED.clear()


# ---------- basics ----------

async def test_plain_answer():
    out = await graph(fake(AIMessage("hello there"))).ainvoke({"messages": [HumanMessage("hi")]}, cfg)
    assert out["messages"][-1].content == "hello there"


async def test_tool_loop_runs_calculator():
    model = fake(call("calculator", {"expression": "6*7"}), AIMessage("It is 42."))
    out = await graph(model).ainvoke({"messages": [HumanMessage("what is 6*7?")]}, cfg)
    assert out["messages"][-2].content == "42"
    assert out["messages"][-1].content == "It is 42."


async def test_memory_and_system_prompt():
    model = fake(AIMessage("noted"), AIMessage("your name is Danial"))
    g = graph(model, system_prompt="be brief")
    await g.ainvoke({"messages": [HumanMessage("my name is Danial")]}, cfg)
    await g.ainvoke({"messages": [HumanMessage("what is my name?")]}, cfg)
    second = model.seen[1]
    assert second[0].content.startswith("be brief\nToday is ")
    assert any("my name is Danial" in str(m.content) for m in second)


async def test_refusal_is_replaced():
    refusal = AIMessage("", response_metadata={"stop_reason": "refusal"})
    out = await graph(fake(refusal)).ainvoke({"messages": [HumanMessage("x")]}, cfg)
    assert out["messages"][-1].content == REFUSAL_REPLY


@pytest.mark.parametrize("expr,want", [("2+3*4", "14"), ("10/4", "2.5"), ("__import__('os')", "error")])
def test_calculator_is_safe(expr, want):
    assert calculator.invoke({"expression": expr}).startswith(want)


# ---------- specialists + routing ----------

def test_keyword_fast_path():
    assert keyword_route("add a task to buy milk") == ["tasks"]
    assert keyword_route("fetch https://example.com") == ["research"]
    assert keyword_route("what is my printer IP?") == []
    assert set(keyword_route("add the shopping list items as tasks")) == {"tasks", "research"}


async def test_each_specialist_sees_only_its_tools():
    model = fake(AIMessage("ok"))
    graph(model)
    assert ["calculator", "current_time"] in model.bound_tools  # +search_knowledge with a DB
    assert ["add_task", "list_tasks"] in model.bound_tools


async def test_specialist_cannot_call_other_agents_tools():
    model = fake(call("add_task", {"title": "x"}), AIMessage("done?"))
    out = await graph(model, "assistant").ainvoke({"messages": [HumanMessage("hi")]}, cfg)
    assert "not available to this agent" in out["messages"][-2].content
    assert ADDED == []


async def test_two_specialists_run_in_order():
    model = fake(AIMessage("Shopping list: milk"), call("add_task", {"title": "milk"}), AIMessage("Added milk."))
    g = graph(model, "assistant", "tasks", approval_tools=set())
    out = await g.ainvoke({"messages": [HumanMessage("add my shopping list as tasks")]}, cfg)
    assert out["plan"] == ["assistant", "tasks"] and ADDED == ["milk"]
    assert "Shopping list: milk" in str(model.seen[1])   # 2nd agent saw the 1st agent's answer


# ---------- human approval ----------

async def test_action_pauses_until_approved():
    model = fake(call("add_task", {"title": "renew insurance"}), AIMessage("Added the task."))
    g = graph(model, "tasks", approval_tools={"add_task"})
    out = await g.ainvoke({"messages": [HumanMessage("add a task")]}, cfg)
    assert out["__interrupt__"][0].value == {"tool": "add_task", "args": {"title": "renew insurance"}}
    assert ADDED == []                                   # nothing ran yet

    out = await g.ainvoke(Command(resume={"approved": True}), cfg)
    assert ADDED == ["renew insurance"]
    assert out["messages"][-1].content == "Added the task."


async def test_rejected_action_does_not_run_and_reply_is_honest():
    model = fake(call("add_task", {"title": "x"}), AIMessage("I have added the task."))
    g = graph(model, "tasks", approval_tools={"add_task"})
    await g.ainvoke({"messages": [HumanMessage("add a task")]}, cfg)
    out = await g.ainvoke(Command(resume={"approved": False}), cfg)
    assert ADDED == []
    assert out["messages"][-1].content == ("I couldn't complete that: the user rejected this "
                                           "action. Do not retry it.")


async def test_two_approvals_never_double_run():
    """Resume re-runs the tools node; asking all approvals first prevents duplicates."""
    two = AIMessage("", tool_calls=[{"name": "add_task", "args": {"title": "a"}, "id": "1"},
                                    {"name": "add_task", "args": {"title": "b"}, "id": "2"}])
    g = graph(fake(two, AIMessage("Added both.")), "tasks", approval_tools={"add_task"})
    await g.ainvoke({"messages": [HumanMessage("add a and b")]}, cfg)
    await g.ainvoke(Command(resume={"approved": True}), cfg)
    out = await g.ainvoke(Command(resume={"approved": True}), cfg)
    assert ADDED == ["a", "b"]
    assert out["messages"][-1].content == "Added both."


# ---------- action verification ----------

async def test_claimed_action_without_tool_is_corrected():
    g = graph(fake(AIMessage("I have added 'buy milk' to your tasks.")), "tasks",
              approval_tools={"add_task"})
    out = await g.ainvoke({"messages": [HumanMessage("add buy milk")]}, cfg)
    assert out["messages"][-1].content == "I didn't actually perform that action - no change was made."
    assert sum(isinstance(m, AIMessage) for m in out["messages"]) == 1   # replaced, not appended


async def test_listing_is_not_flagged():
    g = graph(fake(call("list_tasks", {}), AIMessage("You have no tasks.")), "tasks",
              approval_tools={"add_task"})
    out = await g.ainvoke({"messages": [HumanMessage("my tasks?")]}, cfg)
    assert out["messages"][-1].content == "You have no tasks."


# ---------- retrieval ----------

class FakeRetriever:
    def __init__(self, hits):
        self.hits, self.queries = hits, []

    def search(self, q):
        self.queries.append(q)
        return self.hits


async def test_assistant_gets_passages_tasks_agent_does_not():
    hit = Hit(id=1, content="[notes]\nPrinter IP is 192.168.0.20", source="notes.md",
              title="notes", chunk_index=0, metadata={}, score=0.9)
    r, model = FakeRetriever([hit]), fake(AIMessage("192.168.0.20 [1]"))
    await graph(model, retriever=r).ainvoke({"messages": [HumanMessage("printer ip?")]}, cfg)
    assert r.queries == ["printer ip?"] and "192.168.0.20" in model.seen[0][0].content

    r2, model2 = FakeRetriever([hit]), fake(AIMessage("ok"))
    await graph(model2, "tasks", retriever=r2).ainvoke({"messages": [HumanMessage("x")]}, cfg)
    assert r2.queries == [] and "192.168.0.20" not in model2.seen[0][0].content


# ---------- API ----------

def test_api_rest_approval_and_websocket(monkeypatch):
    monkeypatch.setenv("JARVIS_APPROVAL_TOOLS", '["add_task"]')
    from jarvis.config import get_settings
    get_settings.cache_clear()

    model = fake(AIMessage("pong"),
                 call("add_task", {"title": "milk"}), AIMessage("Added milk."),
                 AIMessage("streamed reply"))
    import jarvis.runtime as runtime
    monkeypatch.setattr(runtime, "get_tools", lambda s: ALL_TOOLS)
    app = create_app(model=model, router=fixed_router("tasks"))
    with TestClient(app) as client:
        assert client.get("/health").json()["ok"] is True

        r = client.post("/chat", json={"message": "ping", "thread_id": "a"}).json()
        assert r == {"thread_id": "a", "status": "done", "reply": "pong", "approval": None, "agents": ["tasks"]}

        r = client.post("/chat", json={"message": "add milk", "thread_id": "b"}).json()
        assert r["status"] == "needs_approval" and r["approval"]["tool"] == "add_task"
        r = client.post("/approve", json={"thread_id": "b", "approved": True}).json()
        assert r["status"] == "done" and r["reply"] == "Added milk." and ADDED == ["milk"]

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"message": "stream please", "thread_id": "c"})
            events = []
            while (e := ws.receive_json())["type"] != "done":
                events.append(e)
        assert events[0] == {"type": "route", "agents": ["tasks"]}
        assert "".join(e["text"] for e in events if e["type"] == "token") == "streamed reply"
        assert e["reply"] == "streamed reply"


async def test_router_defaults_to_assistant_without_llm_call():
    from jarvis.agents import make_router

    model = fake()  # no replies queued: any LLM call would raise
    route = make_router(model)
    assert await route("what is my printer IP?") == ["assistant"]
    assert await route("add a task to buy milk") == ["tasks"]


async def test_research_prompt_gets_sandbox_path():
    model = fake(AIMessage("ok"))
    await graph(model, "research", sandbox_dir="/abs/sandbox").ainvoke(
        {"messages": [HumanMessage("read my file")]}, cfg)
    assert "/abs/sandbox" in model.seen[0][0].content


async def test_handoff_and_final_instructions():
    model = fake(AIMessage("File says: plug adapter"), AIMessage("Added the plug adapter task."))
    await graph(model, "research", "tasks").ainvoke({"messages": [HumanMessage("x")]}, cfg)
    assert "You are step 1 of 2. The 'tasks' agent acts next." in model.seen[0][0].content
    assert "ONE short final answer" in model.seen[1][0].content


async def test_fetch_policy_drops_tiny_max_length():
    from jarvis.graph import ARG_POLICIES

    assert ARG_POLICIES["fetch"]({"url": "u", "max_length": 100}) == {"url": "u"}
    assert ARG_POLICIES["fetch"]({"url": "u", "max_length": 8000}) == {"url": "u", "max_length": 8000}


async def test_empty_handoff_is_replaced_with_tool_findings():
    model = fake(call("calculator", {"expression": "2+2"}), AIMessage(""), AIMessage("final"))
    out = await graph(model, "assistant", "tasks").ainvoke({"messages": [HumanMessage("x")]}, cfg)
    notes = [m.content for m in out["messages"] if isinstance(m, AIMessage) and "Findings" in str(m.content)]
    assert notes == ["Findings from the assistant agent:\n4"]


async def test_complete_requires_lookup_and_approval_shows_title():
    @tool
    def complete_task(task_id: int) -> str:
        """Complete a task (test double)."""
        return f"completed #{task_id}"

    ADDED.extend(["#1 Renew passport | high priority", "#3 Buy milk"])
    model = fake(call("complete_task", {"task_id": 3}, id="a"),      # guesses without looking
                 call("complete_task", {"task_id": 1}, id="b"),      # retries after the listing
                 AIMessage("Completed the passport task."))
    g = graph(model, "tasks", tools=[*ALL_TOOLS, complete_task], approval_tools={"complete_task"})
    out = await g.ainvoke({"messages": [HumanMessage("mark the passport task done")]}, cfg)
    first = [m for m in out["messages"] if getattr(m, "tool_call_id", None) == "a"][0]
    assert first.content.startswith("error: look up the right number first")
    assert "#1 Renew passport" in first.content
    assert out["__interrupt__"][0].value["target"] == "#1 Renew passport | high priority"
