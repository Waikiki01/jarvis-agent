"""The LangGraph agent.

    START -> retrieve -> agent <-> tools -> END

`retrieve` runs knowledge-base search in code on every user turn. Small local models
often fail to call a search tool when they should, so retrieval is deterministic and the
cross-encoder score (inside the retriever) decides whether anything is relevant enough to
show the model. The search tool stays available for follow-up searches.
"""
import asyncio

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from .tools import TOOLS

REFUSAL_REPLY = "Sorry, I can't help with that request."


class State(MessagesState):
    context: str  # passages retrieved for the current turn ("" = none relevant)


def build_graph(model: BaseChatModel, checkpointer=None, system_prompt: str = "",
                tools=TOOLS, retriever=None):
    """Build and compile the agent graph.

    model, checkpointer and retriever are injected so tests can use fakes and the API
    can choose in-memory or Postgres memory.
    """
    from .rag import format_hits

    bound = model.bind_tools(tools) if tools else model

    async def retrieve(state: State):
        last = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
        if retriever is None or last is None:
            return {"context": ""}
        try:
            hits = await asyncio.to_thread(retriever.search, str(last.content))
        except Exception:  # knowledge base down: answer without it
            hits = []
        return {"context": format_hits(hits) if hits else ""}

    async def agent(state: State):
        system = system_prompt
        if state.get("context"):
            system += ("\n\nPassages from the user's knowledge base that may answer the "
                       "latest question (cite as [n]):\n\n" + state["context"])
        msgs = [SystemMessage(system), *state["messages"]] if system else state["messages"]
        reply = await bound.ainvoke(msgs)
        # Claude reports safety refusals as stop_reason="refusal" (HTTP 200).
        if (reply.response_metadata or {}).get("stop_reason") == "refusal":
            reply = AIMessage(REFUSAL_REPLY)
        return {"messages": [reply]}

    g = StateGraph(State)
    g.add_node("retrieve", retrieve)
    g.add_node("agent", agent)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "agent")
    if tools:
        g.add_node("tools", ToolNode(tools))
        g.add_conditional_edges("agent", tools_condition)  # -> "tools" or END
        g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)
