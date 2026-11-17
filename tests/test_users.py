"""Multi-user: auth, and per-user isolation of conversations, tool data and sandbox files.

Graph-level tests need nothing; API auth tests use a throwaway `jarvis_test_auth` database
(skipped if the docker Postgres isn't running).
"""
import os

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from starlette.websockets import WebSocketDisconnect

from jarvis.agents import fixed_router
from jarvis.config import get_settings
from jarvis.graph import model_view, sandbox_path
from tests.test_agent import FakeRetriever, call, fake, graph

ADMIN_DSN = "postgresql://jarvis:jarvis@localhost:5433/jarvis"
TEST_DSN = "postgresql://jarvis:jarvis@localhost:5433/jarvis_test_auth"
SEEN_OWNERS: list[str] = []


@tool
def list_tasks(owner: str = "local") -> str:
    """List tasks (test double that records whose list was read)."""
    SEEN_OWNERS.append(owner)
    return f"tasks of {owner}"


# ---------- graph: owner injection, hidden from the model ----------

@tool
def documented(title: str, owner: str = "local") -> str:
    """Add something.

    Args:
        title: what to add.
        owner: whose list.
    """
    return title


def test_model_never_sees_the_owner_argument():
    for t in (list_tasks, documented):
        schema = model_view(t)
        assert "owner" not in schema["function"]["parameters"].get("properties", {})
        assert "owner" not in schema["function"]["description"]
    assert "title: what to add." in model_view(documented)["function"]["description"]


async def test_owner_comes_from_config_not_from_the_model():
    SEEN_OWNERS.clear()
    # The model tries to read bob's list; the system overwrites owner with the signed-in user.
    model = fake(call("list_tasks", {"owner": "bob"}), AIMessage("done"))
    g = graph(model, "tasks", tools=[list_tasks])
    await g.ainvoke({"messages": [HumanMessage("show bob's tasks")]},
                    {"configurable": {"thread_id": "t", "user": "alice"}})
    assert SEEN_OWNERS == ["alice"]


async def test_retrieval_is_scoped_to_the_user():
    r = FakeRetriever([])
    await graph(fake(AIMessage("x")), retriever=r).ainvoke(
        {"messages": [HumanMessage("q")]}, {"configurable": {"thread_id": "t", "user": "alice"}})
    assert r.owners == ["alice"]


# ---------- sandbox folders ----------

def test_sandbox_paths(tmp_path):
    root = str(tmp_path)
    os.makedirs(tmp_path / "users" / "bob")
    alice = os.path.join(root, "users", "alice")
    assert sandbox_path("notes.txt", root, "alice") == os.path.join(alice, "notes.txt")
    assert sandbox_path(f"{alice}/../bob/x", root, "alice") is None          # .. escape
    assert sandbox_path(os.path.join(root, "list.txt"), root, "alice") is None
    assert sandbox_path("/etc/passwd", root, "alice") is None
    assert sandbox_path(os.path.join(root, "list.txt"), root, "local") == os.path.join(root, "list.txt")
    assert sandbox_path(os.path.join(root, "users", "bob"), root, "local") is None  # local can't snoop
    os.symlink("/etc", tmp_path / "users" / "alice" / "link")
    assert sandbox_path(os.path.join(alice, "link", "passwd"), root, "alice") is None  # symlink escape


async def test_file_tool_outside_sandbox_is_refused(tmp_path):
    seen = []

    @tool
    def read_text_file(path: str) -> str:
        """Read a file (test double)."""
        seen.append(path)
        return "content"

    model = fake(call("read_text_file", {"path": "/etc/passwd"}), AIMessage("x"))
    out = await graph(model, "research", tools=[read_text_file], sandbox_dir=str(tmp_path)).ainvoke(
        {"messages": [HumanMessage("read it")]}, {"configurable": {"thread_id": "t", "user": "alice"}})
    assert seen == []
    assert any("outside your sandbox" in str(m.content) for m in out["messages"])


# ---------- API auth (needs docker Postgres) ----------

@pytest.fixture
def auth_app(monkeypatch):
    try:
        with psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=2) as c:
            c.execute("DROP DATABASE IF EXISTS jarvis_test_auth WITH (FORCE)")
            c.execute("CREATE DATABASE jarvis_test_auth")
    except psycopg.OperationalError:
        pytest.skip("Postgres not running (docker compose up -d)")
    from jarvis import users
    from jarvis.api import create_app

    monkeypatch.setenv("JARVIS_DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("JARVIS_AUTH_ENABLED", "true")
    get_settings.cache_clear()
    tokens = {u: users.add_user(TEST_DSN, u) for u in ("alice", "bob")}
    model = fake(*[AIMessage(f"reply {i}") for i in range(10)])
    with TestClient(create_app(model=model, router=fixed_router("tasks"))) as client:
        yield client, tokens, model
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS jarvis_test_auth WITH (FORCE)")


def test_rest_needs_a_valid_token(auth_app):
    client, tokens, _ = auth_app
    assert client.get("/health").json()["auth"] is True            # public, no user data
    assert client.post("/chat", json={"message": "hi"}).status_code == 401
    assert client.get("/me", headers={"Authorization": "Bearer jv_wrong"}).status_code == 401
    assert client.get("/me", headers={"Authorization": f"Bearer {tokens['alice']}"}).json() == {"user": "alice"}


def test_same_thread_id_is_a_different_conversation_per_user(auth_app):
    client, tokens, model = auth_app
    h = {u: {"Authorization": f"Bearer {t}"} for u, t in tokens.items()}
    client.post("/chat", json={"message": "alice secret 42", "thread_id": "t"}, headers=h["alice"])
    client.post("/chat", json={"message": "bob here", "thread_id": "t"}, headers=h["bob"])
    seen_by_bob = " ".join(str(m.content) for m in model.seen[-1])
    assert "bob here" in seen_by_bob and "alice secret" not in seen_by_bob


def test_websocket_auth(auth_app):
    client, tokens, _ = auth_app
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": "jv_wrong"})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": tokens["bob"]})
        assert ws.receive_json() == {"type": "auth_ok", "user": "bob"}
        ws.send_json({"message": "hi", "thread_id": "w"})
        assert ws.receive_json()["type"] in {"route", "token", "done"}


def test_user_ids_are_validated():
    from jarvis import users

    for bad in ("local", "../x", "Alice", "", "a" * 33, "a b"):
        with pytest.raises(ValueError):
            users.add_user("postgresql://unused", bad)
