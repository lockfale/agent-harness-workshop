"""M2 hook: load tools from an MCP server over stdio.

An MCP server's tools arrive with their own `inputSchema`. Those schemas go
straight into the registry, which means a remote tool is validated by the same
validator as a local one and a remote tool with a bad schema is rejected at
registration rather than at 3am.

This module needs the optional `mcp` dependency. Nothing else in the harness
imports it, so the rest of the labs run without it.

    pip install mcp
"""

from __future__ import annotations

import asyncio
from typing import Any

from .registry import ToolRegistry, ToolResult


def _require_mcp() -> tuple[Any, Any, Any]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "loading MCP tools needs the optional `mcp` package: pip install mcp"
        ) from exc
    return ClientSession, StdioServerParameters, stdio_client


async def _list_tools(command: str, args: list[str], env: dict[str, str] | None) -> list[Any]:
    ClientSession, StdioServerParameters, stdio_client = _require_mcp()
    params = StdioServerParameters(command=command, args=args, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            return list(listed.tools)


async def _call_tool(
    command: str, args: list[str], env: dict[str, str] | None, name: str, arguments: dict[str, Any]
) -> Any:
    ClientSession, StdioServerParameters, stdio_client = _require_mcp()
    params = StdioServerParameters(command=command, args=args, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, arguments)


def _flatten(result: Any) -> str:
    """Turn an MCP tool result into text the model can read."""
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else repr(block))
    return "\n".join(parts) if parts else repr(result)


def register_stdio_tools(
    registry: ToolRegistry,
    *,
    command: str,
    args: list[str],
    env: dict[str, str] | None = None,
    prefix: str = "",
    timeout: float = 30.0,
) -> list[str]:
    """Register every tool the server advertises. Returns the names registered.

    One subprocess per call keeps this short enough to read in a workshop. A
    real harness holds the session open; if you want that, it is a good
    follow-on exercise and the validation path does not change.
    """
    tools = asyncio.run(_list_tools(command, args, env))
    registered: list[str] = []

    for tool in tools:
        remote_name = tool.name
        local_name = f"{prefix}{remote_name}"
        schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}

        def handler(arguments: dict[str, Any], ctx: Any, _remote: str = remote_name) -> ToolResult:
            try:
                result = asyncio.run(
                    asyncio.wait_for(
                        _call_tool(command, args, env, _remote, arguments), timeout
                    )
                )
            except asyncio.TimeoutError:
                return ToolResult.failure(f"MCP tool {_remote!r} timed out after {timeout}s")
            is_error = bool(getattr(result, "isError", False))
            text = _flatten(result)
            return ToolResult(ok=not is_error, content=text, error=text if is_error else None)

        registry.register(
            name=local_name,
            description=(getattr(tool, "description", None) or f"MCP tool {remote_name}"),
            schema=schema,
            handler=handler,
        )
        registered.append(local_name)

    return registered
