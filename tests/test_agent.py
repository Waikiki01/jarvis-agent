"""Offline tests: a fake chat model stands in for Claude / Ollama."""
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from jarvis.api import create_app
from jarvis.graph import REFUSAL_REPLY, build_graph
from jarvis.tools import calculator


class FakeModel(GenericFakeChatModel):
    """GenericFakeChatModel that accepts bind_tools and records what it was sent."""
    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def fake(*replies) -> FakeModel:
    return FakeModel(messages=iter(replies), seen=[])


cfg = {"configurable": {"thread_id": "t1"}}


async def test_plain_answer():
    g = build_graph(fake(AIMessage("hello there")), InMemorySaver())
    out = await g.ainvoke({"messages": [HumanMessage("hi")]}, cfg)
    assert out["messages"][-1].content == "hello there"


async def test_tool_loop_runs_calculator():
    model = fake(
        AIMessage("", tool_calls=[{"name": "calculator", "args": {"expression": "6*7"}, "id": "c1"}]),
        AIMessage("It is 42."),
    )
    g = build_graph(model, InMemorySaver())
    out = await g.ainvoke({"messages": [HumanMessage("what is 6*7?")]}, cfg)
    tool_msg = out["messages"][-2]
    assert tool_msg.content == "42"
    assert out["messages"][-1].content == "It is 42."


async def test_memory_persists_per_thread():
    model = fake(AIMessage("noted"), AIMessage("your name is Danial"))
    g = build_graph(model, InMemorySaver(), system_prompt="be brief")
    await g.ainvoke({"messages": [HumanMessage("my name is Danial")]}, cfg)
    await g.ainvoke({"messages": [HumanMessage("what is my name?")]}, cfg)
    second_call = model.seen[1]
    assert second_call[0].content == "be brief"           # system prompt first
    assert any("my name is Danial" in str(m.content) for m in second_call)


async def test_refusal_is_replaced():
    refusal = AIMessage("", response_metadata={"stop_reason": "refusal"})
    g = build_graph(fake(refusal), InMemorySaver())
    out = await g.ainvoke({"messages": [HumanMessage("x")]}, cfg)
    assert out["messages"][-1].content == REFUSAL_REPLY


@pytest.mark.parametrize("expr,want", [("2+3*4", "14"), ("10/4", "2.5"), ("__import__('os')", "error")])
def test_calculator_is_safe(expr, want):
    assert calculator.invoke({"expression": expr}).startswith(want)


def test_api_rest_and_websocket():
    app = create_app(model=fake(AIMessage("pong"), AIMessage("streamed reply")))
    with TestClient(app) as client:
        assert client.get("/health").json()["ok"] is True
        r = client.post("/chat", json={"message": "ping", "thread_id": "a"})
        assert r.json()["reply"] == "pong"

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"message": "stream please", "thread_id": "b"})
            events = []
            while (e := ws.receive_json())["type"] != "done":
                events.append(e)
        text = "".join(e["text"] for e in events if e["type"] == "token")
        assert text == "streamed reply"


class FakeRetriever:
    def __init__(self, hits):
        self.hits, self.queries = hits, []

    def search(self, q):
        self.queries.append(q)
        return self.hits


async def test_retrieve_node_injects_passages():
    from jarvis.rag.store import Hit

    hit = Hit(id=1, content="[notes]\nPrinter IP is 192.168.0.20", source="notes.md",
              title="notes", chunk_index=0, metadata={}, score=0.9)
    retriever, model = FakeRetriever([hit]), fake(AIMessage("It is 192.168.0.20 [1]."))
    g = build_graph(model, InMemorySaver(), system_prompt="sys", retriever=retriever)
    await g.ainvoke({"messages": [HumanMessage("printer ip?")]}, cfg)
    assert retriever.queries == ["printer ip?"]
    system = model.seen[0][0].content
    assert "[1] (source: notes.md #0)" in system and "192.168.0.20" in system


async def test_no_relevant_passages_means_no_context():
    model = fake(AIMessage("I couldn't find it."))
    g = build_graph(model, InMemorySaver(), system_prompt="sys", retriever=FakeRetriever([]))
    await g.ainvoke({"messages": [HumanMessage("wife's birthday?")]}, cfg)
    assert model.seen[0][0].content == "sys"
