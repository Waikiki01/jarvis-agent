"""The LangGraph agent: model node <-> tool node loop, with a checkpointer for memory."""
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from .tools import TOOLS

REFUSAL_REPLY = "Sorry, I can't help with that request."


def build_graph(model: BaseChatModel, checkpointer=None, system_prompt: str = "", tools=TOOLS):
    """Build and compile the agent graph.

    The model and checkpointer are passed in so tests can use fakes and the API can
    choose between in-memory and Postgres memory.
    """
    bound = model.bind_tools(tools) if tools else model

    async def agent(state: MessagesState):
        msgs = state["messages"]
        if system_prompt:
            msgs = [SystemMessage(system_prompt), *msgs]
        reply = await bound.ainvoke(msgs)
        # Claude reports safety refusals as stop_reason="refusal" (HTTP 200).
        if (reply.response_metadata or {}).get("stop_reason") == "refusal":
            reply = AIMessage(REFUSAL_REPLY)
        return {"messages": [reply]}

    g = StateGraph(MessagesState)
    g.add_node("agent", agent)
    g.add_edge(START, "agent")
    if tools:
        g.add_node("tools", ToolNode(tools))
        g.add_conditional_edges("agent", tools_condition)  # -> "tools" or END
        g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)
