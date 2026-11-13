"""Multi-agent graph with a supervisor, human approval and action verification.

    START -> router -> prepare -> agent <-> tools -> advance -+-> verify -> END
                         ^                                    |
                         +---------- next specialist ---------+

- router   picks the specialist(s) for the request (agents.py)
- prepare  activates the next specialist; runs RAG retrieval in code if it uses it
           (small models often skip calling a search tool, so we don't rely on it)
- agent    the active specialist's LLM call, with only that specialist's tools
- tools    runs tool calls. Actions in `approval_tools` pause the graph with
           interrupt() until the user approves or rejects (human-in-the-loop)
- advance  moves to the next specialist, or on to verify
- verify   if the reply claims an action happened but no action tool succeeded this
           turn, the reply is replaced with the truth (enforced in code, not prompt)
"""
import asyncio
import re
from datetime import date

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from .agents import DEFAULT, SPECIALISTS, Router, make_router
from .tools import TOOLS

REFUSAL_REPLY = "Sorry, I can't help with that request."
MAX_TOOL_ROUNDS = 6  # per specialist, stops tool-call loops
_CLAIM = re.compile(
    r"\b(added|created|marked|completed|saved|deleted|removed|updated|scheduled)\b", re.I)


def _fetch_policy(args: dict) -> dict:
    # The model sometimes asks fetch for max_length=100, truncating the page to nothing useful.
    if int(args.get("max_length") or 5000) < 2000:
        args = {k: v for k, v in args.items() if k != "max_length"}
    return args


# Tool-argument policies enforced in code (the model's arguments are not trusted blindly).
ARG_POLICIES = {"fetch": _fetch_policy}

# Preconditions: an action may only run after a lookup tool ran this turn. Stops the model
# from guessing an id - and the user from approving "complete task #3" blind.
PRECONDITIONS = {"complete_task": "list_tasks"}


def _task_line(listing: str, task_id) -> str | None:
    return next((ln for ln in listing.splitlines() if ln.startswith(f"#{task_id} ")), None)


class State(MessagesState):
    plan: list[str]   # specialists for this turn, in order
    step: int         # index into plan
    context: str      # retrieved passages for the active specialist ("" = none)
    rounds: int       # tool rounds used by the active specialist


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def current_turn(messages: list) -> list:
    """Messages after the latest user message."""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return messages[i + 1:]
    return list(messages)


def build_graph(model: BaseChatModel, checkpointer=None, system_prompt: str = "",
                tools=TOOLS, retriever=None, router: Router | None = None,
                approval_tools: frozenset[str] | set[str] = frozenset(),
                sandbox_dir: str = "sandbox"):
    from .rag import format_hits

    by_name = {t.name: t for t in tools}
    specialists = {
        name: (spec, [by_name[t] for t in spec.tools if t in by_name])
        for name, spec in SPECIALISTS.items()
    }
    bound = {name: (model.bind_tools(ts) if ts else model) for name, (_, ts) in specialists.items()}
    route = router or make_router(model)
    action_tools = set(approval_tools)

    def active(state: State) -> str:
        plan = state.get("plan") or [DEFAULT]
        return plan[min(state.get("step", 0), len(plan) - 1)]

    async def router_node(state: State):
        last = current_turn_user(state["messages"])
        plan = [p for p in await route(last) if p in specialists] or [DEFAULT]
        return {"plan": plan, "step": 0}

    async def prepare(state: State):
        spec, _ = specialists[active(state)]
        context = ""
        if spec.uses_rag and retriever is not None:
            try:
                hits = await asyncio.to_thread(retriever.search, current_turn_user(state["messages"]))
            except Exception:  # knowledge base down: answer without it
                hits = []
            context = format_hits(hits) if hits else ""
        return {"context": context, "rounds": 0}

    async def agent(state: State):
        name = active(state)
        spec, _ = specialists[name]
        system = (f"{system_prompt}\nToday is {date.today().isoformat()}.\n\n"
                  + spec.prompt.replace("{sandbox}", sandbox_dir))
        plan, step = state.get("plan") or [DEFAULT], state.get("step", 0)
        if step < len(plan) - 1:
            system += (f"\n\nYou are step {step + 1} of {len(plan)}. The '{plan[step + 1]}' agent "
                       "acts next. Only report the facts it needs, in one or two lines. Do not "
                       "promise or describe any other actions.")
        elif len(plan) > 1:
            system += ("\n\nOther agents already handled earlier steps of this request (see "
                       "above). Give the user ONE short final answer for the whole request, "
                       "using only facts from tool results. Do not list items that no tool "
                       "returned.")
        if state.get("context"):
            system += ("\n\nPassages from the user's knowledge base that may answer the "
                       "latest question (cite as [n]):\n\n" + state["context"])
        reply = await bound[name].ainvoke([SystemMessage(system), *state["messages"]])
        if (reply.response_metadata or {}).get("stop_reason") == "refusal":
            reply = AIMessage(REFUSAL_REPLY)
        return {"messages": [reply], "rounds": state.get("rounds", 0) + 1}

    def after_agent(state: State) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None) and state.get("rounds", 0) <= MAX_TOOL_ROUNDS:
            return "tools"
        return "advance"

    async def tools_node(state: State):
        calls = state["messages"][-1].tool_calls
        allowed = {t.name for t in specialists[active(state)][1]}
        # Ask for every approval BEFORE running anything: on resume LangGraph re-runs this
        # node from the top, so no tool may have run before the last interrupt().
        turn = current_turn(state["messages"])
        lookups: dict[str, str] = {}
        for m in turn:
            if isinstance(m, ToolMessage):
                lookups[m.name] = str(m.content)
                for need in PRECONDITIONS.values():  # an automatic lookup counts too
                    marker = f"Result of {need}:\n"
                    if marker in str(m.content):
                        lookups[need] = str(m.content).split(marker, 1)[1]
        blocked = {}  # tool_call id -> result text, for unmet preconditions
        for tc in calls:
            need = PRECONDITIONS.get(tc["name"])
            if need and need not in lookups and need in by_name:
                listing = text_of(await by_name[need].ainvoke({}))
                blocked[tc["id"]] = (f"error: look up the right number first - do not guess. "
                                     f"Result of {need}:\n{listing}")
        approved = {}
        for tc in calls:
            if tc["name"] in action_tools and tc["name"] in allowed and tc["id"] not in blocked:
                request = {"tool": tc["name"], "args": tc["args"]}
                if tc["name"] == "complete_task" and "list_tasks" in lookups:
                    request["target"] = _task_line(lookups["list_tasks"], tc["args"].get("task_id"))
                decision = interrupt(request)
                approved[tc["id"]] = isinstance(decision, dict) and decision.get("approved") is True
        out = []
        for tc in calls:
            if tc["name"] not in allowed:
                result = f"error: tool {tc['name']!r} is not available to this agent"
            elif tc["id"] in blocked:
                result = blocked[tc["id"]]
            elif approved.get(tc["id"]) is False:
                result = "error: the user rejected this action. Do not retry it."
            else:
                try:
                    args = ARG_POLICIES.get(tc["name"], lambda a: a)(dict(tc["args"]))
                    result = text_of(await by_name[tc["name"]].ainvoke(args))
                except Exception as e:
                    result = f"error: {e}"
            out.append(ToolMessage(content=str(result), tool_call_id=tc["id"], name=tc["name"]))
        return {"messages": out}

    def advance(state: State):
        step, plan = state.get("step", 0), state.get("plan") or []
        updates: dict = {"step": step + 1}
        last = state["messages"][-1]
        if (step < len(plan) - 1 and isinstance(last, AIMessage) and not last.tool_calls
                and not text_of(last.content).strip()):
            # Empty hand-off note: pass on what this agent's tools found instead.
            found = [str(m.content) for m in current_turn(state["messages"]) if isinstance(m, ToolMessage)]
            note = found[-1][:2000] if found else "nothing found"
            updates["messages"] = [AIMessage(f"Findings from the {plan[step]} agent:\n{note}", id=last.id)]
        if getattr(last, "tool_calls", None):  # hit MAX_TOOL_ROUNDS with calls pending
            updates["messages"] = [
                *[ToolMessage("error: stopped, too many tool calls", tool_call_id=tc["id"],
                              name=tc["name"]) for tc in last.tool_calls],
                AIMessage("I stopped because this was taking too many steps."),
            ]
        return updates

    def after_advance(state: State) -> str:
        return "prepare" if state["step"] < len(state.get("plan") or []) else "verify"

    def verify(state: State):
        turn = current_turn(state["messages"])
        final = next((m for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), None)
        if final is None or not _CLAIM.search(text_of(final.content)):
            return {}
        if not any(SPECIALISTS[p].tools and set(SPECIALISTS[p].tools) & action_tools
                   for p in state.get("plan") or []):
            return {}  # no action-capable specialist ran, nothing to verify
        results = [m for m in turn if isinstance(m, ToolMessage) and m.name in action_tools]
        if any(not str(m.content).startswith("error") for m in results):
            return {}
        errors = [str(m.content) for m in results]
        truth = ("I couldn't complete that: " + errors[-1].removeprefix("error: ")
                 if errors else "I didn't actually perform that action - no change was made.")
        return {"messages": [AIMessage(truth, id=final.id)]}  # same id = replace

    g = StateGraph(State)
    for name, fn in [("router", router_node), ("prepare", prepare), ("agent", agent),
                     ("tools", tools_node), ("advance", advance), ("verify", verify)]:
        g.add_node(name, fn)
    g.add_edge(START, "router")
    g.add_edge("router", "prepare")
    g.add_edge("prepare", "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", "advance"])
    g.add_edge("tools", "agent")
    g.add_conditional_edges("advance", after_advance, ["prepare", "verify"])
    g.add_edge("verify", END)
    return g.compile(checkpointer=checkpointer)


def current_turn_user(messages: list) -> str:
    last = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    return text_of(last.content) if last else ""
