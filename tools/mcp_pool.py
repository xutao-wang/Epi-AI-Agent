import asyncio
from contextlib import AsyncExitStack
from importlib import import_module
from pathlib import Path
from typing import Any

from mcp.client.stdio import stdio_client

from tools.mcp_tools import get_server_config


def _load_client_session_class() -> type[Any] | None:
    candidates = (
        ("mcp", "ClientSession"),
        ("mcp.client.session", "ClientSession"),
    )
    for module_name, attr in candidates:
        try:
            module = import_module(module_name)
            return getattr(module, attr)
        except (ImportError, AttributeError):
            continue
    return None


async def _build_stdio_client(command: str, args: list[str], env: dict[str, str] | None):
    # mcp-python changed stdio_client from accepting a plain command list
    # to requiring a server parameters object. Support both call styles.
    try:
        from mcp.client.stdio import StdioServerParameters

        return stdio_client(StdioServerParameters(command=command, args=args, env=env))
    except ImportError:
        return stdio_client([command, *args])


def _resolve_command_args(args: list[str]) -> list[str]:
    """Resolve local script paths robustly when launched from arbitrary CWDs."""
    project_root = Path(__file__).resolve().parent.parent
    resolved: list[str] = []
    for arg in args:
        if not isinstance(arg, str):
            resolved.append(arg)
            continue
        if arg.startswith("-"):
            resolved.append(arg)
            continue
        candidate = Path(arg)
        if candidate.is_absolute() or candidate.exists():
            resolved.append(str(candidate))
            continue
        repo_candidate = project_root / arg
        if repo_candidate.exists():
            resolved.append(str(repo_candidate))
            continue
        resolved.append(arg)
    return resolved


async def _create_mcp_client(cfg: dict[str, Any], stack: AsyncExitStack):
    command = cfg["command"]
    args = _resolve_command_args(cfg.get("args", []))
    env = cfg.get("env")

    stdio = await _build_stdio_client(command=command, args=args, env=env)
    stdio_result = await stack.enter_async_context(stdio)

    if hasattr(stdio_result, "call_tool"):
        client = stdio_result
        if hasattr(client, "initialize"):
            await client.initialize()
        return client

    if isinstance(stdio_result, tuple) and len(stdio_result) >= 2:
        client_session_cls = _load_client_session_class()
        if client_session_cls is None:
            raise RuntimeError("Could not import MCP ClientSession class")

        read_stream, write_stream = stdio_result[0], stdio_result[1]
        session = await stack.enter_async_context(client_session_cls(read_stream, write_stream))
        if hasattr(session, "initialize"):
            await session.initialize()
        return session

    raise RuntimeError(f"Unsupported stdio_client result type: {type(stdio_result).__name__}")


async def call_mcp_tool(server_name: str, tool_name: str, payload: dict):
    """Call an MCP tool using a one-shot session.

    This avoids cross-event-loop pooled-session issues and keeps the API simple
    and robust for repeated calls.
    """
    cfg = get_server_config(server_name)
    if not cfg:
        raise ValueError(f"Unknown MCP server: {server_name}")

    command = cfg.get("command")
    if not command:
        raise ValueError(f"Invalid MCP server config for '{server_name}': missing 'command'")

    async with AsyncExitStack() as stack:
        client = await _create_mcp_client(cfg, stack)
        return await client.call_tool(tool_name, payload)


def call_mcp_tool_sync(server_name: str, tool_name: str, payload: dict):
    """Simple sync wrapper for callers in non-async node code."""
    return asyncio.run(call_mcp_tool(server_name, tool_name, payload))


def call_server_tool(server: str, tool_name: str, **payload):
    """Very simple helper: call a tool with keyword args as payload."""
    payload.setdefault("server", server)
    return call_mcp_tool_sync(server, tool_name, payload)
