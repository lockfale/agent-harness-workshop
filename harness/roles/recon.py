"""M2: recon. Explore the task and emit observations.

What you fill in
----------------
1. `SYSTEM_PROMPT`. The role's job is observations, not conclusions. An
   observation is something you can point at: a file, a port, a function, a
   response. "This is probably a buffer overflow" is a conclusion, and it
   belongs to triage, which has to defend its ranking. Write the prompt so the
   model produces the former.
2. The body of `record_observation`. The schema is already written and the
   registry already validates against it, so your handler is handed arguments
   you can trust. Decide what you store and in what shape, because triage
   reads exactly this.
3. Optional, and the better half of M2: point the registry at your own MCP
   server with `registry.load_mcp_stdio(...)` and give the agent a tool from
   `mcp-skeleton/`. MCP tools come through the same validator, so a sloppy
   `inputSchema` is rejected here rather than at runtime.

What you do not fill in
-----------------------
The turn loop, the trace, the budget, and the two target-facing tools. Both
run inside the attacker container, never the target: `shell` is checked by the
scope gate as a command in that container, and `http_request` is checked by
its host and port. Only the second one is a network check (see M5).
"""

from __future__ import annotations

from typing import Any

from ..status import StageStatus
from ..tools.finish import register_finish
from ..tools.registry import ToolContext, ToolRegistry, ToolResult
from ..tools.http import register_http
from ..tools.shell import register_shell
from .base import Pipeline, RoleOutcome, run_role

STAGE = "recon"

# TODO(M2): write this prompt.
#
# Things worth putting in it, from runs that went badly without them:
#   - say what an observation is, and that conclusions are somebody else's job
#   - say the shell runs inside the container and the host is not reachable
#   - say that `finish` is the only way to end, and that going quiet records
#     "we do not know what this role concluded"
#   - give it a budget in turns, because a role that does not know it is on a
#     clock will explore until the cap stops it
SYSTEM_PROMPT = """TODO(M2): the recon role's system prompt goes here.

Until you write it, this role will wander. That is the exercise: run it once
with this placeholder, read the trace, then write the prompt that fixes what
you saw.
"""

OBSERVATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "what": {
            "type": "string",
            "minLength": 1,
            "description": "The observation itself. Something you can point at.",
        },
        "where": {
            "type": "string",
            "minLength": 1,
            "description": "File and line, endpoint, port, or function name.",
        },
        "evidence": {
            "type": "string",
            "minLength": 1,
            "description": "The command output or file content you are reading from.",
        },
        "kind": {
            "type": "string",
            "enum": ["file", "service", "binary", "endpoint", "config", "behaviour", "other"],
        },
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "How sure you are that you read this correctly.",
        },
    },
    "required": ["what", "where", "evidence", "kind"],
    "additionalProperties": False,
}


def record_observation(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """TODO(M2): store one observation for the triage role to pick up.

    The arguments are already validated against OBSERVATION_SCHEMA, so you do
    not need to check them. What you choose to store is the contract between
    this role and triage: `roles/triage.py` dedups and ranks exactly what you
    put in `ctx.state["observations"]`.

    A minimal version that works:

        observations = ctx.state.setdefault("observations", [])
        observations.append(dict(arguments))
        return ToolResult(content={"recorded": len(observations)})

    Then decide what else is worth keeping. A turn number? The tool that
    produced the evidence? Think about what triage will wish it had.
    """
    raise NotImplementedError("TODO(M2): implement record_observation")


def build_registry(pipeline: Pipeline) -> ToolRegistry:
    """The tools recon can call. Plumbing, except for what you add."""
    registry = ToolRegistry(role=STAGE, trace=pipeline.trace)
    register_shell(registry)
    register_http(registry)
    registry.register(
        name="record_observation",
        description=(
            "Record one thing you observed. Facts, not conclusions. Call this "
            "every time you learn something, not once at the end."
        ),
        schema=OBSERVATION_SCHEMA,
        handler=record_observation,
    )
    # TODO(M2): add your own tool here, or load one from your MCP server:
    #   registry.load_mcp_stdio("python", ["mcp-skeleton/server.py"], prefix="mcp_")
    register_finish(registry)
    return registry


def opening_messages(pipeline: Pipeline) -> list[dict[str, Any]]:
    task = pipeline.task
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Task: {getattr(task, 'name', '') or getattr(task, 'task_id', 'unknown')}\n"
                f"Target: {target_line(task)}\n\n"
                f"{getattr(task, 'prompt', '')}"
            ),
        },
    ]


def target_line(task: Any) -> str:
    """How the agent should address the target. The target's container name
    is the hostname on the task network, and that is all the agent is told."""
    host = getattr(task, "target_host", None)
    port = getattr(task, "target_port", None)
    if not host:
        return "none configured"
    return f"{host}:{port}" if port else str(host)


def run(pipeline: Pipeline) -> StageStatus:
    outcome: RoleOutcome = run_role(
        role=STAGE,
        pipeline=pipeline,
        registry=build_registry(pipeline),
        messages=opening_messages(pipeline),
    )
    observations = pipeline.state.get("observations") or []
    return pipeline.statuses.write(
        outcome.to_status(STAGE, observations=len(observations))
    )
