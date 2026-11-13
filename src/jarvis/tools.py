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


TOOLS = [calculator, current_time]
