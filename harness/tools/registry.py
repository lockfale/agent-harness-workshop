"""Tool registration and input validation.

Every tool is a name, a description, a JSON Schema, and a handler. There are no
shorthand tool types and no vendor server-tools, because a tool declared
without a schema gets called with `{}` or with argument names the model made up,
and then nothing happens and nothing says so.

Validation runs before the handler. An invalid input returns the error and the
schema to the model, is written to the trace, and the handler never runs.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ..trace import redact_text

# Names the registry will not let you take.
RESERVED_NAMES = frozenset({"finish"})


class ToolError(Exception):
    """A tool failed in a way the model should see and can respond to."""


class ToolDefinitionError(Exception):
    """A tool was registered wrongly. A programming error, not a model error."""


@dataclasses.dataclass
class ToolContext:
    """What a handler is given besides its validated arguments.

    Attendees reach for `ctx.scope` and `ctx.task` in M5 and `ctx.state` to
    pass observations between turns.
    """

    role: str
    run_dir: Any = None
    task: Any = None
    scope: Any = None
    trace: Any = None
    state: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ToolResult:
    """What a handler returns, and what the model is shown.

    `finish` is the only flag the role runner acts on: it ends the role.
    """

    ok: bool = True
    content: Any = ""
    error: str | None = None
    finish: bool = False
    payload: dict[str, Any] | None = None

    @classmethod
    def failure(cls, error: str, **extra: Any) -> "ToolResult":
        body: dict[str, Any] = {"error": error}
        body.update(extra)
        return cls(ok=False, content=body, error=error)

    def for_model(self) -> str:
        """The string placed in the `tool` message."""
        if isinstance(self.content, str):
            return self.content
        return json.dumps(self.content, default=str, indent=2)


Handler = Callable[[dict[str, Any], ToolContext], ToolResult | str | dict[str, Any]]


@dataclasses.dataclass
class Tool:
    """One registered tool."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: Handler

    def openai_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


def normalise_schema(schema: Mapping[str, Any] | None, tool_name: str) -> dict[str, Any]:
    """Validate the schema itself, and close it unless told otherwise.

    `additionalProperties: false` is the default on purpose. An open schema
    accepts invented argument names, the handler reads the ones it knows about,
    finds nothing, and returns an empty-looking success. Closing the schema
    turns that into a validation error the model has to answer.
    """
    if schema is None:
        raise ToolDefinitionError(
            f"tool {tool_name!r} has no JSON Schema. Every tool needs one, even a "
            f'no-argument tool: use {{"type": "object", "properties": {{}}}}.'
        )
    body = dict(schema)
    if body.get("type") != "object":
        raise ToolDefinitionError(
            f"tool {tool_name!r} schema must have type 'object' at the top level, "
            f"got {body.get('type')!r}."
        )
    body.setdefault("properties", {})
    body.setdefault("additionalProperties", False)
    try:
        Draft202012Validator.check_schema(body)
    except SchemaError as exc:
        raise ToolDefinitionError(f"tool {tool_name!r} has an invalid schema: {exc.message}") from exc
    return body


class ToolRegistry:
    """The tools one role can call."""

    def __init__(self, role: str = "", trace: Any = None) -> None:
        self.role = role
        self.trace = trace
        self._tools: dict[str, Tool] = {}

    # -- registration --------------------------------------------------------

    def register(
        self,
        name: str,
        description: str,
        schema: Mapping[str, Any] | None,
        handler: Handler,
        allow_reserved: bool = False,
    ) -> Tool:
        if name in self._tools:
            raise ToolDefinitionError(f"tool {name!r} is already registered")
        if name in RESERVED_NAMES and not allow_reserved:
            raise ToolDefinitionError(
                f"tool name {name!r} is reserved by the harness. Use "
                f"harness.tools.finish.register_finish() instead."
            )
        tool = Tool(
            name=name,
            description=description,
            schema=normalise_schema(schema, name),
            handler=handler,
        )
        self._tools[name] = tool
        return tool

    def tool(self, name: str, description: str, schema: Mapping[str, Any] | None) -> Callable[[Handler], Handler]:
        """Decorator form of `register`."""

        def decorate(handler: Handler) -> Handler:
            self.register(name, description, schema, handler)
            return handler

        return decorate

    # -- inspection ----------------------------------------------------------

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[dict[str, Any]]:
        """Tool definitions in the shape the chat-completions API wants."""
        return [tool.openai_spec() for tool in self._tools.values()]

    def require_finish(self) -> None:
        """Every role needs a way to say it is done. Called by the role runner."""
        if "finish" not in self._tools:
            raise ToolDefinitionError(
                f"role {self.role!r} has no `finish` tool. Without one, a role that "
                f"stops cannot be distinguished from a role that completed. "
                f"Call harness.tools.finish.register_finish(registry)."
            )

    # -- calling -------------------------------------------------------------

    def validate(self, name: str, arguments: Any) -> tuple[Tool | None, ToolResult | None]:
        """Check the name and the arguments. Returns (tool, rejection)."""
        tool = self._tools.get(name)
        if tool is None:
            return None, ToolResult.failure(
                f"no such tool: {name!r}",
                available_tools=sorted(self._tools),
            )
        if not isinstance(arguments, dict):
            return tool, ToolResult.failure(
                f"tool {name!r} expects a JSON object of arguments, got "
                f"{type(arguments).__name__}",
                schema=tool.schema,
            )
        errors = sorted(
            Draft202012Validator(tool.schema).iter_errors(arguments),
            key=lambda err: list(err.path),
        )
        if errors:
            return tool, ToolResult.failure(
                "; ".join(_describe(err) for err in errors),
                schema=tool.schema,
            )
        return tool, None

    def call(self, name: str, arguments: Any, ctx: ToolContext) -> ToolResult:
        """Validate, then run. An invalid call never reaches the handler."""
        tool, rejection = self.validate(name, arguments)
        if rejection is not None:
            if self.trace is not None:
                self.trace.gate_reject(
                    gate="tool_input",
                    rule="schema_validation" if tool is not None else "unknown_tool",
                    message=rejection.error or "rejected",
                    role=ctx.role,
                    tool=name,
                )
            return rejection

        assert tool is not None
        try:
            result = tool.handler(arguments, ctx)
        except ToolError as exc:
            return ToolResult.failure(redact_text(str(exc)) or "tool error")
        except Exception as exc:  # noqa: BLE001 - a tool bug is the model's problem to route around
            return ToolResult.failure(redact_text(f"{type(exc).__name__}: {exc}") or "tool error")

        if isinstance(result, ToolResult):
            return result
        if isinstance(result, str):
            return ToolResult(content=result)
        return ToolResult(content=result)

    # -- M2: tools from an MCP server ---------------------------------------

    def load_mcp_stdio(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        prefix: str = "",
        timeout: float = 30.0,
        only: list[str] | None = None,
    ) -> list[str]:
        """Register the tools an MCP server offers over stdio.

        The point of this hook is that MCP tools come through the same door as
        local ones: the server's declared `inputSchema` becomes the registry's
        schema, so a remote tool with a sloppy schema is rejected by the same
        validator, before the call leaves this process.

        `only` is an allowlist of the server's tool names, without the prefix.
        A role should get the tools its job needs, not everything a server
        happens to offer. None registers them all.

        Needs the optional `mcp` dependency. Pair it with `mcp-skeleton/`.
        """
        from .mcp_stdio import register_stdio_tools

        return register_stdio_tools(
            self, command=command, args=args or [], env=env, prefix=prefix,
            timeout=timeout, only=only,
        )


def _describe(error: Any) -> str:
    """A validation error the model can act on."""
    where = "/".join(str(part) for part in error.path)
    return f"{where or '<root>'}: {error.message}"
