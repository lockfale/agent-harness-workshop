"""A shell tool that only runs inside the attacker container.

Three constraints, all deliberate:

* Never the target. The agent's shell runs in a separate attacker container on
  the task network and reaches the target by hostname, the way Cybench runs
  its own agents. A shell inside the target could read the flag off its disk.
* No attacker container, no execution. The tool returns an error. It does not
  quietly fall back to running on your laptop or in the target.
* Every command passes the scope gate first, as `kind=shell` with the
  container it would run in.

What the gate does not do is read the command. `curl http://out-of-scope/` in
a shell is checked as "a shell command in the attacker container", not as a
network request to `out-of-scope`. That is on purpose and it is the M5 lab:
the structured `http_request` tool puts its host and port in front of the
gate, and the shell does not. Watch which one the gate catches. Parsing shell
commands to recover their network targets is a losing game (variables,
base64, a script written to disk and run), and pretending it works would teach
the wrong lesson.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from ..gates.scope import Action, Decision
from .registry import ToolContext, ToolRegistry, ToolResult

SHELL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "minLength": 1,
            "description": (
                "A single shell command, run inside the attacker container. Reach "
                "the target by its hostname on the task network."
            ),
        },
        "timeout_s": {
            "type": "integer",
            "minimum": 1,
            "maximum": 600,
            "description": "Seconds to wait before giving up. Defaults to 60.",
        },
        "rationale": {
            "type": "string",
            "description": "One line on why you are running this. Shows up in the trace.",
        },
    },
    "required": ["command"],
    "additionalProperties": False,
}

DEFAULT_TIMEOUT_S = 60
MAX_OUTPUT_CHARS = 20_000


def attacker_container_for(ctx: ToolContext) -> str | None:
    """Where the agent's tools run. Never the target container."""
    task = ctx.task
    return getattr(task, "attacker_container", None) if task is not None else None


def docker_exec(
    container: str,
    command: str | list[str],
    timeout_s: int = DEFAULT_TIMEOUT_S,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    """Run one command in a container. Returns (exit_code, stdout, stderr).

    A string goes to the container's `sh -lc`; a list is run as argv.
    """
    docker = shutil.which("docker")
    if docker is None:
        raise FileNotFoundError("docker is not on PATH")
    argv = ["sh", "-lc", command] if isinstance(command, str) else list(command)
    proc = subprocess.run(  # noqa: S603 - fixed argv, command goes to the container
        [docker, "exec", *(["-i"] if stdin is not None else []), container, *argv],
        input=stdin,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def shell_handler(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    container = attacker_container_for(ctx)
    if not container:
        return ToolResult.failure(
            "no attacker container is configured, so there is nowhere sandboxed to "
            "run this. The shell tool does not run commands on the host or in the "
            "target."
        )

    command = arguments["command"]
    timeout_s = int(arguments.get("timeout_s") or DEFAULT_TIMEOUT_S)

    if ctx.scope is not None:
        action = Action(
            kind="shell",
            container=container,
            command=command,
            rationale=arguments.get("rationale"),
        )
        decision = ctx.scope.check(action)
        if decision is not Decision.ALLOW:
            return ToolResult.failure(
                f"the scope gate returned {decision.value} for this command. "
                f"It was not run. Stay inside the declared scope, or say why the "
                f"task needs it.",
                decision=decision.value,
            )

    try:
        code, out, err = docker_exec(container, command, timeout_s)
    except subprocess.TimeoutExpired:
        return ToolResult.failure(f"command timed out after {timeout_s}s", timed_out=True)
    except FileNotFoundError as exc:
        return ToolResult.failure(str(exc))

    return ToolResult(
        ok=code == 0,
        content={
            "exit_code": code,
            "stdout": out[:MAX_OUTPUT_CHARS],
            "stderr": err[:MAX_OUTPUT_CHARS],
        },
    )


def register_shell(registry: ToolRegistry, name: str = "shell") -> None:
    registry.register(
        name=name,
        description=(
            "Run one shell command inside the attacker container, which shares a "
            "network with the target. Not on the host, not in the target. Reach the "
            "target by hostname. Commands are checked against the task's scope "
            "rules before they run."
        ),
        schema=SHELL_SCHEMA,
        handler=shell_handler,
    )
