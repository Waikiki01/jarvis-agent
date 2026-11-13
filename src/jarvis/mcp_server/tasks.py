"""Jarvis Tasks: a standalone MCP server for a personal to-do list (Postgres).

It shows the three MCP building blocks:
  - tools      add_task, list_tasks, complete_task
  - resource   tasks://open  (read-only view of open tasks)
  - prompt     plan_my_day   (reusable prompt template)

Any MCP client can use it (Jarvis, Claude Desktop, Cursor, ...):
    python -m jarvis.mcp_server.tasks            # stdio (default)
    python -m jarvis.mcp_server.tasks --http     # streamable HTTP on :8765/mcp

Needs JARVIS_DATABASE_URL. Deleting is deliberately not offered: destructive actions get
a human-approval step in the agent first (step 4).
"""
import argparse
import os
from datetime import date
from typing import Literal

import psycopg
from mcp.server.fastmcp import FastMCP

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         bigserial PRIMARY KEY,
    title      text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    due        date,
    priority   text NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high')),
    done       boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    done_at    timestamptz
);
"""

mcp = FastMCP("jarvis-tasks", host="127.0.0.1", port=8765)


def _conn() -> psycopg.Connection:
    dsn = os.environ.get("JARVIS_DATABASE_URL")
    if not dsn:
        raise RuntimeError("JARVIS_DATABASE_URL is not set")
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(SCHEMA)
    return conn


def _fmt(row) -> str:
    tid, title, due, prio, done = row
    parts = [f"#{tid} {title}"]
    if due:
        parts.append(f"due {due.isoformat()}")
    if prio != "normal":
        parts.append(f"{prio} priority")
    if done:
        parts.append("done")
    return " | ".join(parts)


def _open_tasks() -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, title, due, priority, done FROM tasks WHERE NOT done "
            "ORDER BY due NULLS LAST, CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id"
        ).fetchall()
    return [_fmt(r) for r in rows]


@mcp.tool()
def add_task(title: str, due: str | None = None,
             priority: Literal["low", "normal", "high"] = "normal") -> str:
    """Add a task to the user's to-do list.

    Args:
        title: what needs doing, e.g. "Renew car insurance".
        due: optional due date as YYYY-MM-DD.
        priority: low, normal or high.
    """
    title = title.strip()
    if not 1 <= len(title) <= 200:
        return "error: title must be 1-200 characters"
    try:
        due_date = date.fromisoformat(due) if due else None
    except ValueError:
        return f"error: due must be YYYY-MM-DD, got {due!r}"
    with _conn() as c:
        row = c.execute(
            "INSERT INTO tasks (title, due, priority) VALUES (%s, %s, %s) "
            "RETURNING id, title, due, priority, done", (title, due_date, priority)).fetchone()
    return f"added {_fmt(row)}"


@mcp.tool()
def list_tasks(include_done: bool = False) -> str:
    """List the user's tasks, soonest due first. Set include_done to also show finished ones."""
    if not include_done:
        rows = _open_tasks()
    else:
        with _conn() as c:
            rows = [_fmt(r) for r in c.execute(
                "SELECT id, title, due, priority, done FROM tasks ORDER BY done, due NULLS LAST, id")]
    return "\n".join(rows) if rows else "No tasks."


@mcp.tool()
def complete_task(task_id: int) -> str:
    """Mark a task as done, by its number (e.g. 3 for #3). Call list_tasks first if unsure."""
    with _conn() as c:
        row = c.execute(
            "UPDATE tasks SET done = true, done_at = now() WHERE id = %s AND NOT done "
            "RETURNING id, title, due, priority, done", (task_id,)).fetchone()
    return f"completed {_fmt(row)}" if row else f"error: no open task #{task_id}"


@mcp.resource("tasks://open")
def open_tasks_resource() -> str:
    """All open tasks, one per line."""
    return "\n".join(_open_tasks()) or "No open tasks."


@mcp.prompt()
def plan_my_day() -> str:
    """Ask the assistant to turn today's open tasks into a short plan."""
    tasks = "\n".join(_open_tasks()) or "(no open tasks)"
    return (f"Today is {date.today().isoformat()}. Here are my open tasks:\n{tasks}\n\n"
            "Make a short, realistic plan for today: overdue and high-priority first, "
            "at most 5 items, one line each.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP on :8765/mcp")
    mcp.run(transport="streamable-http" if ap.parse_args().http else "stdio")


if __name__ == "__main__":
    main()
