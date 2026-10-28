"""MCP sessions stay open for the run. Offline: a tiny local stdio server, no Docker.

The server holds a counter. If each call started a fresh process, every call
would answer 1; a held session answers 1, 2, 3. That is the difference that
lets a browser server remember the page it loaded.
"""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from harness.tools import mcp_stdio  # noqa: E402
from harness.tools.registry import ToolContext, ToolRegistry  # noqa: E402

SERVER = textwrap.dedent(
    """
    import asyncio
    import os

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("stateful-fixture")
    count = 0

    @mcp.tool()
    def bump() -> str:
        \"\"\"Add one to the counter and return it.\"\"\"
        global count
        count += 1
        return str(count)

    @mcp.tool()
    def pid() -> str:
        \"\"\"The server's process id.\"\"\"
        return str(os.getpid())

    @mcp.tool()
    async def slow(seconds: float) -> str:
        \"\"\"Sleep, then answer. Async, so the server keeps answering meanwhile.\"\"\"
        await asyncio.sleep(seconds)
        return "done"

    if __name__ == "__main__":
        mcp.run()
    """
)


@pytest.fixture
def server(tmp_path: Path) -> Path:
    path = tmp_path / "stateful_server.py"
    path.write_text(SERVER, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _close_sessions():
    yield
    mcp_stdio.close_all()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_state_persists_across_calls(server: Path) -> None:
    registry = ToolRegistry(role="recon")
    names = registry.load_mcp_stdio(sys.executable, [str(server)], prefix="t_")
    assert set(names) == {"t_bump", "t_pid", "t_slow"}

    ctx = ToolContext(role="recon")
    answers = [registry.call("t_bump", {}, ctx).content for _ in range(3)]
    assert answers == ["1", "2", "3"]

    first = registry.call("t_pid", {}, ctx).content
    second = registry.call("t_pid", {}, ctx).content
    assert first == second


def test_schema_validation_still_applies(server: Path) -> None:
    registry = ToolRegistry(role="recon")
    registry.load_mcp_stdio(sys.executable, [str(server)], prefix="t_")
    result = registry.call("t_slow", {"seconds": "not a number"}, ToolContext(role="recon"))
    assert not result.ok


def test_close_stops_the_server(server: Path) -> None:
    session = mcp_stdio.StdioSession(sys.executable, [str(server)])
    pid = int(mcp_stdio._flatten(session.call_tool("pid", {})))
    assert _alive(pid)

    session.close()
    assert not session.running
    assert _wait_gone(pid), "server process outlived close()"
    with pytest.raises(RuntimeError):
        session.call_tool("bump", {})
    session.close()  # a second close is harmless


def test_close_all_closes_every_session(server: Path) -> None:
    registry = ToolRegistry(role="recon")
    registry.load_mcp_stdio(sys.executable, [str(server)], prefix="a_")
    registry.load_mcp_stdio(sys.executable, [str(server)], prefix="b_")
    ctx = ToolContext(role="recon")
    pids = [int(registry.call(f"{p}_pid", {}, ctx).content) for p in ("a", "b")]
    assert pids[0] != pids[1]

    mcp_stdio.close_all()
    assert not mcp_stdio._SESSIONS
    assert all(_wait_gone(pid) for pid in pids)


def test_a_timeout_does_not_kill_the_session(server: Path) -> None:
    registry = ToolRegistry(role="recon")
    registry.load_mcp_stdio(sys.executable, [str(server)], prefix="t_", timeout=0.5)
    ctx = ToolContext(role="recon")

    result = registry.call("t_slow", {"seconds": 3}, ctx)
    assert not result.ok
    assert "timed out" in (result.error or "")

    assert registry.call("t_bump", {}, ctx).content == "1"


def test_a_server_that_will_not_start_raises() -> None:
    with pytest.raises(Exception):
        mcp_stdio.StdioSession(sys.executable, ["-c", "import sys; sys.exit(3)"], open_timeout=10)
    assert not mcp_stdio._SESSIONS
