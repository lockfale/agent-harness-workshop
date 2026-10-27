"""The `finish` tool: the only way a role ends successfully.

Without it, a role that stops producing tool calls looks exactly like a role
that finished its job. Those are different outcomes. A role that goes quiet
gets `incomplete`, which is neither success nor failure: it means we do not
know what it concluded, and the pipeline should not pretend otherwise.
"""

from __future__ import annotations

from typing import Any

from .registry import ToolContext, ToolRegistry, ToolResult

FINISH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "minLength": 1,
            "description": "What you did and what you concluded, in a few sentences.",
        },
        "outcome": {
            "type": "string",
            "enum": ["success", "no_result", "blocked"],
            "description": (
                "success: you produced the artifact this role is for. "
                "no_result: you looked and there was nothing. "
                "blocked: something outside your control stopped you. Say what."
            ),
        },
        "artifacts": {
            "type": "object",
            "description": "Paths or ids this role produced, keyed by what they are.",
            "additionalProperties": {"type": "string"},
        },
        "results": {
            "type": "array",
            "description": "Structured output for the next role. Shape is role specific.",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "required": ["summary", "outcome"],
    "additionalProperties": False,
}


def finish_handler(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Record the role's own account of itself and end the loop."""
    payload = {
        "summary": arguments["summary"],
        "outcome": arguments["outcome"],
        "artifacts": arguments.get("artifacts") or {},
        "results": arguments.get("results") or [],
    }
    ctx.state["finish"] = payload
    return ToolResult(
        ok=True,
        content={"acknowledged": True, "outcome": payload["outcome"]},
        finish=True,
        payload=payload,
    )


def register_finish(
    registry: ToolRegistry,
    description: str = (
        "End your turn as this role. Call this exactly once, when you are done. "
        "If you stop without calling it, the pipeline records that it does not "
        "know what you concluded."
    ),
) -> None:
    registry.register(
        name="finish",
        description=description,
        schema=FINISH_SCHEMA,
        handler=finish_handler,
        allow_reserved=True,
    )
