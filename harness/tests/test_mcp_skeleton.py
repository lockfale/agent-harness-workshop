"""M2: the MCP skeleton loads through the registry and its worked tool answers.

Starts the real server over stdio, so it needs the `mcp` package. It skips
cleanly without it. `read_note` is the attendee's exercise, so nothing here
asserts what it returns.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from harness.tools.registry import ToolContext, ToolRegistry  # noqa: E402

SERVER = Path(__file__).resolve().parents[2] / "mcp-skeleton" / "server.py"


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    registry = ToolRegistry(role="recon")
    registry.load_mcp_stdio(sys.executable, [str(SERVER)], prefix="mcp_")
    return registry


def test_both_tools_register_with_the_servers_schema(registry):
    for name, param in (("mcp_search_notes", "term"), ("mcp_read_note", "name")):
        tool, rejection = registry.validate(name, {param: "x"})
        assert rejection is None, rejection
        assert param in tool.schema["required"]


def test_worked_tool_finds_a_sample_note(registry):
    result = registry.call("mcp_search_notes", {"term": "scope"}, ToolContext(role="recon"))
    assert result.ok
    assert "rules-of-engagement.md:" in result.content


def test_no_match_is_said_plainly(registry):
    result = registry.call("mcp_search_notes", {"term": "zzqx-no-such"}, ToolContext(role="recon"))
    assert result.ok
    assert "No note mentions" in result.content


def test_missing_argument_never_reaches_the_server(registry):
    result = registry.call("mcp_search_notes", {}, ToolContext(role="recon"))
    assert not result.ok
    assert "term" in result.error


def test_an_allowlist_registers_only_the_tools_named():
    registry = ToolRegistry(role="recon")
    names = registry.load_mcp_stdio(
        sys.executable, [str(SERVER)], prefix="mcp_", only=["search_notes"]
    )
    assert names == ["mcp_search_notes"]
    assert registry.names() == ["mcp_search_notes"]


def test_an_allowlist_name_the_server_lacks_is_an_error():
    registry = ToolRegistry(role="recon")
    with pytest.raises(ValueError, match="offers no tool named serch_notes"):
        registry.load_mcp_stdio(sys.executable, [str(SERVER)], only=["serch_notes"])
    assert registry.names() == []
