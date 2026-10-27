"""P3: every tool has a schema, and invalid input never reaches the handler."""

from __future__ import annotations

import pytest

from harness.tools.finish import register_finish
from harness.tools.registry import (
    ToolContext,
    ToolDefinitionError,
    ToolRegistry,
    ToolResult,
)

SCHEMA = {
    "type": "object",
    "properties": {"target": {"type": "string"}, "depth": {"type": "integer", "minimum": 1}},
    "required": ["target"],
}


def registry_with_spy(trace=None):
    """A registry whose one tool records every time it is actually executed."""
    executed: list[dict] = []
    registry = ToolRegistry(role="test", trace=trace)

    def handler(arguments, ctx):
        executed.append(arguments)
        return ToolResult(content={"ok": True})

    registry.register("scan", "scan a target", SCHEMA, handler)
    return registry, executed


def test_invalid_input_is_rejected_and_never_executed(trace):
    registry, executed = registry_with_spy(trace)
    ctx = ToolContext(role="test", trace=trace)

    result = registry.call("scan", {"depth": 0}, ctx)

    assert result.ok is False
    assert executed == [], "the handler ran despite failing validation"
    assert "target" in (result.error or "") or "depth" in (result.error or "")
    assert result.content["schema"]["properties"]["target"]["type"] == "string"

    rejects = trace.of_kind("gate_reject")
    assert len(rejects) == 1
    assert rejects[0]["rule"] == "schema_validation"
    assert rejects[0]["tool"] == "scan"


def test_valid_input_reaches_the_handler(trace):
    registry, executed = registry_with_spy(trace)
    result = registry.call("scan", {"target": "x", "depth": 2}, ToolContext(role="test"))

    assert result.ok is True
    assert executed == [{"target": "x", "depth": 2}]
    assert trace.of_kind("gate_reject") == []


def test_invented_argument_names_are_rejected(trace):
    """Schemas are closed by default, so a made-up field is an error."""
    registry, executed = registry_with_spy(trace)
    result = registry.call("scan", {"target": "x", "tarrget": "x"}, ToolContext(role="test"))

    assert result.ok is False
    assert executed == []
    assert "tarrget" in (result.error or "")


def test_empty_object_is_rejected_when_a_field_is_required(trace):
    registry, executed = registry_with_spy(trace)
    result = registry.call("scan", {}, ToolContext(role="test"))
    assert result.ok is False
    assert executed == []


def test_unknown_tool_returns_a_structured_error(trace):
    registry, _ = registry_with_spy(trace)
    result = registry.call("nmap", {"target": "x"}, ToolContext(role="test", trace=trace))

    assert result.ok is False
    assert "nmap" in (result.error or "")
    assert "scan" in result.content["available_tools"]
    assert trace.of_kind("gate_reject")[0]["rule"] == "unknown_tool"


def test_non_object_arguments_are_rejected(trace):
    registry, executed = registry_with_spy(trace)
    result = registry.call("scan", ["target"], ToolContext(role="test"))
    assert result.ok is False
    assert executed == []


def test_a_tool_without_a_schema_cannot_be_registered():
    registry = ToolRegistry(role="test")
    with pytest.raises(ToolDefinitionError, match="no JSON Schema"):
        registry.register("bad", "no schema", None, lambda a, c: ToolResult())


def test_a_non_object_schema_cannot_be_registered():
    registry = ToolRegistry(role="test")
    with pytest.raises(ToolDefinitionError, match="type 'object'"):
        registry.register("bad", "wrong", {"type": "string"}, lambda a, c: ToolResult())


def test_finish_is_reserved():
    registry = ToolRegistry(role="test")
    with pytest.raises(ToolDefinitionError, match="reserved"):
        registry.register("finish", "mine", {"type": "object"}, lambda a, c: ToolResult())


def test_require_finish_complains_when_there_is_no_finish_tool():
    registry, _ = registry_with_spy()
    with pytest.raises(ToolDefinitionError, match="no `finish` tool"):
        registry.require_finish()

    register_finish(registry)
    registry.require_finish()


def test_handler_exceptions_become_tool_errors(trace):
    registry = ToolRegistry(role="test", trace=trace)

    def handler(arguments, ctx):
        raise ValueError("the tool body is still a TODO")

    registry.register("todo", "not done", {"type": "object"}, handler)
    result = registry.call("todo", {}, ToolContext(role="test"))

    assert result.ok is False
    assert "the tool body is still a TODO" in (result.error or "")


def test_specs_are_openai_shaped():
    registry, _ = registry_with_spy()
    register_finish(registry)
    specs = registry.specs()

    assert {spec["function"]["name"] for spec in specs} == {"scan", "finish"}
    assert all(spec["type"] == "function" for spec in specs)
    assert all("parameters" in spec["function"] for spec in specs)
