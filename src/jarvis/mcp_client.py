"""Connect Jarvis to MCP servers listed in mcp_servers.json and expose their tools.

Production concerns handled here:
  - persistent sessions: each server is started once at app start-up, not per tool call
  - allow-list: only the tools named in "allow_tools" are given to the model
    (fewer, safer tools; small models choose better from a short list)
  - failure isolation: a server that fails to start is logged and skipped; Jarvis runs on
  - name clashes: a tool whose name is already taken is skipped, never silently replaced

Config placeholders: {python} = this interpreter, {root} = repo root, {home} = $HOME.
"""
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def _expand(value, env: dict):
    if isinstance(value, str):
        return value.format(python=sys.executable, root=ROOT, home=Path.home(), **env)
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    return value


def load_config(path: str | Path, env: dict | None = None) -> tuple[dict, dict]:
    """Return (connections for MultiServerMCPClient, allow-lists per server)."""
    raw = json.loads(Path(path).read_text())
    env = {"database_url": os.environ.get("JARVIS_DATABASE_URL", ""), **(env or {})}
    connections, allow = {}, {}
    for name, cfg in raw.items():
        if cfg.pop("enabled", True) is False:
            continue
        allow[name] = cfg.pop("allow_tools", None)
        connections[name] = _expand(cfg, env)
    return connections, allow


async def open_mcp_tools(stack: AsyncExitStack, config_path: str | Path,
                         taken: set[str] = frozenset(), env: dict | None = None) -> list[BaseTool]:
    """Start each configured server inside `stack` and return their allowed tools."""
    path = Path(config_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        log.info("no MCP config at %s", path)
        return []
    connections, allow = load_config(path, env)
    client = MultiServerMCPClient(connections)
    names, tools = set(taken), []
    for server in connections:
        try:
            session = await stack.enter_async_context(client.session(server))
            loaded = await load_mcp_tools(session, server_name=server)
        except Exception as e:
            log.warning("MCP server %r failed to start, skipping: %s", server, e)
            continue
        for t in loaded:
            if allow[server] is not None and t.name not in allow[server]:
                continue
            if t.name in names:
                log.warning("MCP tool %r from %r clashes with an existing tool, skipping", t.name, server)
                continue
            names.add(t.name)
            tools.append(t)
        log.info("MCP %s: %s", server, [t.name for t in loaded if t in tools])
    return tools
