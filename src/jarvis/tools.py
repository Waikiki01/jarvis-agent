"""Built-in tools. More arrive in later steps (RAG search, MCP tools)."""
import ast
import operator
from datetime import datetime

from langchain_core.tools import tool

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        if isinstance(node.op, ast.Pow) and abs(_eval(node.right)) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("only numbers and + - * / % ** are allowed")


@tool
def calculator(expression: str) -> str:
    """Evaluate an arithmetic expression, e.g. '12 * (3 + 4) / 2'."""
    try:
        return str(_eval(ast.parse(expression, mode="eval").body))
    except Exception as e:  # returned to the model so it can recover
        return f"error: {e}"


@tool
def current_time() -> str:
    """Return the current local date and time (ISO 8601)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@tool
def search_knowledge(query: str, owner: str = "local") -> str:
    """Search the user's personal knowledge base (their notes, documents and PDFs).

    Use for any question about the user's own plans, notes, policies or records.
    Returns numbered passages with sources; cite them in the answer as [1], [2], ...
    """
    from .rag import format_hits, get_retriever

    try:
        return format_hits(get_retriever().search(query, owner))
    except Exception as e:  # e.g. database down: tell the model instead of crashing
        return f"error: knowledge base unavailable ({e})"


TOOLS = [calculator, current_time]


def get_tools(settings) -> list:
    """Base tools, plus knowledge search when a database is configured."""
    return TOOLS + ([search_knowledge] if settings.database_url else [])
