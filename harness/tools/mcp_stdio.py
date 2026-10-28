"""M2 hook: load tools from an MCP server over stdio.

An MCP server's tools arrive with their own `inputSchema`. Those schemas go
straight into the registry, which means a remote tool is validated by the same
validator as a local one and a remote tool with a bad schema is rejected at
registration rather than at 3am.

One server process per run, not per call
----------------------------------------
The server is started once, when its tools are registered, and every call
goes to that same process until the run ends. Starting a fresh process per
call is simpler, but it throws away whatever the server was holding: a
browser server forgets the page it just loaded, so `navigate` followed by
`click` lands in two different browsers. Tools that are stateless do not
notice either way; tools that hold state only work like this.

The session lives on a background thread with its own asyncio loop, because
the harness's tool handlers are plain synchronous functions. Sessions close
at interpreter exit, or earlier with `close_all()`. Closing matters for a
server started as `docker run --rm -i ...`: the container exits when its
stdin closes, and not before.

Note what does not happen here: no scope check. The gate check lives inside
`http.py` and `shell.py`. A tool loaded from an MCP server goes through the
schema validator and nothing else, so it can reach whatever the server
process can reach. That is the M5 lab's point, not an oversight.

This module needs the optional `mcp` dependency. Nothing else in the harness
imports it, so the rest of the labs run without it.

    pip install mcp
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import threading
from typing import Any

from .registry import ToolRegistry, ToolResult

# How long to wait for the server to start and answer `initialize`. A
# `docker run` server may also be starting a container, so this is longer than
# a single call's timeout.
OPEN_TIMEOUT = 60.0

# How long `close()` waits for the server to exit before giving up on it.
CLOSE_TIMEOUT = 10.0


def _require_mcp() -> tuple[Any, Any, Any]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "loading MCP tools needs the optional `mcp` package: pip install mcp"
        ) from exc
    return ClientSession, StdioServerParameters, stdio_client


class StdioSession:
    """One MCP server process, held open until `close()`.

    A single task on the background loop owns the transport and the session,
    from `initialize` until shutdown. The stdio transport uses task groups
    that must be entered and exited by the same task, so the owner opens
    them, waits for the close signal, and exits them itself. Tool calls are
    separate tasks on the same loop, which the session supports.
    """

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        open_timeout: float = OPEN_TIMEOUT,
    ) -> None:
        self.command = command
        self.args = list(args)
        self._session: Any = None
        self._loop = asyncio.new_event_loop()
        self._closing = asyncio.Event()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name=f"mcp-stdio:{command}", daemon=True
        )
        self._thread.start()

        ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._owner = asyncio.run_coroutine_threadsafe(self._own(env, ready), self._loop)
        try:
            ready.result(open_timeout)
        except concurrent.futures.TimeoutError:
            self.close()
            raise RuntimeError(
                f"MCP server {command!r} did not finish starting within {open_timeout}s"
            ) from None
        except BaseException:
            self.close()
            raise

    async def _own(self, env: dict[str, str] | None, ready: concurrent.futures.Future) -> None:
        ClientSession, StdioServerParameters, stdio_client = _require_mcp()
        params = StdioServerParameters(command=self.command, args=self.args, env=env)
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    ready.set_result(None)
                    await self._closing.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise
        finally:
            self._session = None

    def _live_session(self) -> Any:
        session = self._session
        if session is None:
            raise RuntimeError(f"MCP server {self.command!r} is not running")
        return session

    def _run(self, coro: Any, timeout: float) -> Any:
        future = asyncio.run_coroutine_threadsafe(asyncio.wait_for(coro, timeout), self._loop)
        try:
            # A little slack so the loop's own timeout fires first and cancels
            # the call there, rather than leaving it running on the loop.
            return future.result(timeout + 5)
        except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
            future.cancel()
            raise TimeoutError(f"MCP call timed out after {timeout}s") from None

    def list_tools(self, timeout: float = 30.0) -> list[Any]:
        listed = self._run(self._live_session().list_tools(), timeout)
        return list(listed.tools)

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 30.0) -> Any:
        return self._run(self._live_session().call_tool(name, arguments), timeout)

    @property
    def running(self) -> bool:
        return self._session is not None

    def close(self) -> None:
        """Stop the server and the loop. Safe to call more than once."""
        if self._loop.is_closed():
            return
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._closing.set)
            try:
                self._owner.result(CLOSE_TIMEOUT)
            except BaseException:  # noqa: BLE001 - shutting down; nothing to report to
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(CLOSE_TIMEOUT)
        if not self._thread.is_alive():
            self._loop.close()
        _SESSIONS.discard(self)


# Every open session, so nothing outlives the process that started it.
_SESSIONS: set[StdioSession] = set()


def close_all() -> None:
    """Close every MCP session this process opened."""
    for session in list(_SESSIONS):
        session.close()


atexit.register(close_all)


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
    """Start the server, register every tool it advertises, return their names.

    The server keeps running after this returns. Each registered tool sends
    its calls to it, with `timeout` seconds per call.
    """
    session = StdioSession(command, args, env)
    _SESSIONS.add(session)
    try:
        tools = session.list_tools(timeout)
    except BaseException:
        session.close()
        raise
    registered: list[str] = []

    for tool in tools:
        remote_name = tool.name
        local_name = f"{prefix}{remote_name}"
        schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}

        def handler(arguments: dict[str, Any], ctx: Any, _remote: str = remote_name) -> ToolResult:
            try:
                result = session.call_tool(_remote, arguments, timeout)
            except TimeoutError:
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
