"""MCP tests: the tasks server over the real MCP protocol (stdio), and the client loader.

Server tests need the docker Postgres (a throwaway jarvis_test_mcp database); skipped if down.
"""
import json
import sys
from contextlib import AsyncExitStack

import psycopg
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jarvis.mcp_client import load_config, open_mcp_tools

ADMIN_DSN = "postgresql://jarvis:jarvis@localhost:5433/jarvis"
TEST_DSN = "postgresql://jarvis:jarvis@localhost:5433/jarvis_test_mcp"


@pytest.fixture(scope="module")
def test_db():
    try:
        with psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=2) as c:
            c.execute("DROP DATABASE IF EXISTS jarvis_test_mcp")
            c.execute("CREATE DATABASE jarvis_test_mcp")
    except psycopg.OperationalError:
        pytest.skip("Postgres not running (docker compose up -d)")
    yield TEST_DSN
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS jarvis_test_mcp WITH (FORCE)")


def text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def test_tasks_server_over_mcp(test_db):
    params = StdioServerParameters(command=sys.executable, args=["-m", "jarvis.mcp_server.tasks"],
                                   env={"JARVIS_DATABASE_URL": test_db})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()

        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert set(tools) == {"add_task", "list_tasks", "complete_task"}
        assert tools["add_task"].inputSchema["properties"]["priority"]["enum"] == ["low", "normal", "high"]

        assert text(await s.call_tool("add_task", {"title": "Renew road tax", "due": "2027-06-15",
                                                   "priority": "high"})).startswith("added #1")
        await s.call_tool("add_task", {"title": "Buy plug adapter"})
        assert "error: due must be YYYY-MM-DD" in text(await s.call_tool("add_task", {"title": "x", "due": "tomorrow"}))

        listing = text(await s.call_tool("list_tasks", {}))
        assert listing.splitlines()[0] == "#1 Renew road tax | due 2027-06-15 | high priority"

        assert text(await s.call_tool("complete_task", {"task_id": 2})).startswith("completed #2")
        assert "error: no open task #2" in text(await s.call_tool("complete_task", {"task_id": 2}))
        assert "Buy plug adapter" not in text(await s.call_tool("list_tasks", {}))

        res = await s.read_resource("tasks://open")
        assert "Renew road tax" in res.contents[0].text
        prompt = await s.get_prompt("plan_my_day")
        assert "Renew road tax" in prompt.messages[0].content.text


def test_load_config_expands_and_filters(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "a": {"transport": "stdio", "command": "{python}", "args": ["{root}/x"],
              "env": {"DB": "{database_url}"}, "allow_tools": ["t1"]},
        "off": {"transport": "stdio", "command": "x", "args": [], "enabled": False},
    }))
    conns, allow = load_config(cfg, {"database_url": "postgresql://db"})
    assert set(conns) == {"a"}
    assert conns["a"]["command"] == sys.executable
    assert conns["a"]["args"][0].endswith("/x") and "{root}" not in conns["a"]["args"][0]
    assert conns["a"]["env"] == {"DB": "postgresql://db"}
    assert "allow_tools" not in conns["a"] and allow == {"a": ["t1"]}


async def test_broken_server_is_skipped_and_allowlist_applied(tmp_path, test_db):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "broken": {"transport": "stdio", "command": "/nonexistent/binary", "args": []},
        "tasks": {"transport": "stdio", "command": "{python}", "args": ["-m", "jarvis.mcp_server.tasks"],
                  "env": {"JARVIS_DATABASE_URL": test_db}, "allow_tools": ["list_tasks"]},
    }))
    async with AsyncExitStack() as stack:
        tools = await open_mcp_tools(stack, cfg, taken={"calculator"})
        assert [t.name for t in tools] == ["list_tasks"]
        out = await tools[0].ainvoke({})
        assert "No tasks" in str(out) or "#" in str(out)


def test_env_argument_overrides_os_environ(tmp_path, monkeypatch):
    """Regression: the DB URL comes from settings (.env), not only os.environ."""
    monkeypatch.delenv("JARVIS_DATABASE_URL", raising=False)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"t": {"transport": "stdio", "command": "x", "args": [],
                                     "env": {"JARVIS_DATABASE_URL": "{database_url}"}}}))
    conns, _ = load_config(cfg, {"database_url": "postgresql://from-settings"})
    assert conns["t"]["env"]["JARVIS_DATABASE_URL"] == "postgresql://from-settings"
