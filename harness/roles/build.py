"""M4: build. Write the expectations contract, then stand the target up.

Order matters and the gate checks it. The contract is a prediction: what you
expect to observe if the candidate is real, and what result would falsify it.
Written before the exploit runs, it can be wrong, which is the only way it is
worth anything. Written afterwards it is a description of what happened, and
the judge rejects a `confirmed` verdict that rests on one.

Readiness is checked against the real service socket: from inside the
attacker container, over the task network, the same path the agent uses. A
host-port check is necessary and not sufficient: a rootless port forwarder
completes the TCP handshake itself, so the port answers long after the service
behind it has died, and the exploit then fails against nothing at all. When
there is nothing better we fall back to the host check and record
`readiness: host_only` in the trace, so the weaker evidence is visible rather
than assumed away. Not ready is a harness failure, and the judge is skipped.

What you fill in
----------------
1. `SYSTEM_PROMPT`. Make the model commit to something falsifiable before it
   is allowed to run anything.
2. Anything the task needs before the exploit role can start.

What you do not fill in
-----------------------
The contract writer. It timestamps the file, and the judge compares that
timestamp against when the exploit started, so it is not a good place for a
lab exercise.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from ..cybench import container_health
from ..status import Outcome, StageStatus
from ..tools.finish import register_finish
from ..tools.registry import ToolContext, ToolRegistry, ToolResult
from ..tools.http import register_http
from ..tools.shell import docker_exec, register_shell
from .base import Pipeline, run_role
from .recon import target_line

STAGE = "build"

CONTRACT_FILENAME = "expectations.json"

# TODO(M4): write this prompt.
#
# The one thing it must get across: the contract comes first, and it has to be
# falsifiable. "I expect the program to crash" is not falsifiable enough to be
# useful. "I expect a SIGSEGV with a write to an address derived from my input,
# and I expect the same input with byte 40 changed to not crash" is.
SYSTEM_PROMPT = """TODO(M4): the build role's system prompt goes here.
"""

CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidate": {
            "type": "string",
            "minLength": 1,
            "description": "The candidate this contract is about.",
        },
        "expected_observation": {
            "type": "string",
            "minLength": 1,
            "description": "What you expect to see if the candidate is real. Be specific.",
        },
        "falsifier": {
            "type": "string",
            "minLength": 1,
            "description": "The result that would mean you were wrong.",
        },
        "benign_input": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The benign input for the negative control, and what you expect it "
                "to do, which should be nothing."
            ),
        },
        "instrumentation": {
            "type": "string",
            "description": "Sanitizers, logging or hooks you added, if any.",
        },
    },
    "required": ["candidate", "expected_observation", "falsifier", "benign_input"],
    "additionalProperties": False,
}


@dataclasses.dataclass
class Readiness:
    """The result of a readiness check, and how strong it is."""

    ready: bool
    method: str          # shared_net | in_container | host_only | stub | none
    detail: str = ""
    attempts: int = 1
    # Compose healthcheck status. Advisory: recorded, never consulted.
    health: str | None = None

    @property
    def weak(self) -> bool:
        return self.method not in ("shared_net", "in_container")


def host_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Can we open a TCP connection. Necessary, not sufficient."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Probe a TCP socket from inside the attacker container. Standard library only.
_SOCKET_PROBE = (
    "import socket,sys; "
    "socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()"
)


def check_readiness(task: Any, trace: Any = None, timeout_s: int = 10) -> Readiness:
    """Probe the real service socket, and say which evidence you actually have.

    In order of preference:

    * `shared_net`: connect to `target_host:target_port` from inside the
      attacker container, over the task network. That is the service itself,
      not a forwarder, and it is the path the agent will use. Retried with
      backoff inside a bounded total; not reachable by then is not ready. The
      compose healthcheck is recorded alongside as advisory data only.
    * `in_container`: no attacker container, but a probe command runs inside
      the target.
    * `host_only`: a forwarded host port accepts a connection. Necessary, not
      sufficient, and recorded as such.
    * `stub`: a stub task has nothing to probe, and says so.

    Nothing in the harness is allowed to treat `host_only` as if it were a
    probe of the service.
    """
    if getattr(task, "stub", False):
        result = Readiness(ready=True, method="stub",
                           detail="stub task: no containers, nothing to probe")
        _trace_readiness(trace, task, result)
        return result

    container = getattr(task, "container", None)
    probe = getattr(task, "probe_command", None)
    attacker = getattr(task, "attacker_container", None)
    target_host = getattr(task, "target_host", None)
    target_port = getattr(task, "target_port", None)

    if attacker and target_host and target_port:
        result = _probe_shared_net(task, attacker, target_host, int(target_port), timeout_s)
        _trace_readiness(trace, task, result)
        return result

    if container and probe:
        try:
            code, out, err = docker_exec(container, probe, timeout_s)
            result = Readiness(
                ready=code == 0,
                method="in_container",
                detail=(out or err or "").strip()[:400] or f"exit {code}",
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            result = Readiness(
                ready=False,
                method="in_container",
                detail=f"the probe could not run: {type(exc).__name__}: {exc}",
            )
        _trace_readiness(trace, task, result)
        return result

    port = getattr(task, "port", None)
    if port:
        host = getattr(task, "host", "127.0.0.1")
        open_ = host_port_open(host, int(port))
        result = Readiness(
            ready=open_,
            method="host_only",
            detail=(
                f"{host}:{port} accepted a connection. No probe tool in the image, so "
                f"this does not prove the service behind the port is alive."
                if open_
                else f"{host}:{port} refused a connection"
            ),
        )
        _trace_readiness(trace, task, result)
        return result

    result = Readiness(
        ready=False,
        method="none",
        detail="no probe command and no forwarded port, so readiness is unknown",
    )
    _trace_readiness(trace, task, result)
    return result


# Readiness retries. The service may still be starting when compose returns,
# so the probe retries with backoff, inside a bounded total budget.
READINESS_TOTAL_S = 120.0
READINESS_FIRST_DELAY_S = 1.0
READINESS_MAX_DELAY_S = 10.0


def _probe_shared_net(
    task: Any,
    attacker: str,
    host: str,
    port: int,
    timeout_s: int,
    total_s: float | None = None,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
) -> Readiness:
    """Connect to host:port from the attacker container, retrying with backoff.

    This is the only thing that gates readiness. The compose healthcheck is
    recorded as advisory data and never consulted: Cybench healthchecks are
    written against the task image's own tools, and at least one
    (`nc -z localhost 1337` on a BusyBox base, which has no `-z`) reports
    `unhealthy` for a service that is serving 200 on shared_net.
    """
    total = READINESS_TOTAL_S if total_s is None else total_s
    deadline = clock() + total
    delay = READINESS_FIRST_DELAY_S
    attempts = 0
    last_error = ""
    while True:
        attempts += 1
        try:
            code, _out, err = docker_exec(
                attacker, ["python3", "-c", _SOCKET_PROBE, host, str(port)], timeout_s
            )
            if code == 0:
                break
            last_error = (err or "").strip()[-300:] or f"exit {code}"
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        remaining = deadline - clock()
        if remaining <= 0:
            return Readiness(
                False, "shared_net",
                f"{host}:{port} not reachable from {attacker} after {attempts} attempt(s) "
                f"in {total:.0f}s: {last_error}",
                attempts=attempts,
                health=_advisory_health(task),
            )
        sleep(min(delay, remaining))
        delay = min(delay * 2, READINESS_MAX_DELAY_S)

    return Readiness(
        True, "shared_net",
        f"{host}:{port} accepted a connection from {attacker} (attempt {attempts})",
        attempts=attempts,
        health=_advisory_health(task),
    )


def _advisory_health(task: Any) -> str | None:
    """The compose healthcheck status, for the trace only. Never gates."""
    container = getattr(task, "container", None)
    if not container or not getattr(task, "has_healthcheck", False):
        return None
    try:
        return container_health(container)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None


def _trace_readiness(trace: Any, task: Any, result: Readiness) -> None:
    if trace is None:
        return
    trace.readiness(
        target=str(getattr(task, "container", None) or getattr(task, "task_id", "unknown")),
        method=result.method,
        ready=result.ready,
        detail=result.detail,
        attempts=result.attempts,
        health_advisory=result.health,
    )


def contract_path(run_dir: Path | str) -> Path:
    return Path(run_dir) / CONTRACT_FILENAME


def write_contract(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Write the contract to disk and record when. Plumbing, not an exercise.

    The written-at time is taken here rather than read back off the filesystem
    later, because filesystem timestamp resolution varies and the judge's rule
    hangs on the comparison.
    """
    path = contract_path(ctx.run_dir)
    body = {
        "written_at": time.time(),
        "role": ctx.role,
        **arguments,
    }
    path.write_text(json.dumps(body, indent=2, default=str) + "\n", encoding="utf-8")
    ctx.state["contract_path"] = str(path)
    ctx.state["contract_written_at"] = body["written_at"]
    ctx.state["contract"] = body
    return ToolResult(
        content={
            "written": str(path),
            "note": "the exploit role will refuse to start without this file",
        }
    )


def break_service(pipeline: Pipeline) -> str:
    """M4 lab: kill the service after the readiness check, on purpose.

    This is what `--break-harness` does. The readiness check passes, then the
    service goes away, and the exploit fails for a reason that has nothing to
    do with whether the bug is real. Attendees then watch the
    infrastructure-origin path: the stage reports `harness_failure`, the judge
    is never invoked, and the run records `unconfirmed` with a failure origin
    rather than scoring it as a refutation.
    """
    container = getattr(pipeline.task, "container", None)
    if not container:
        return "no container to break, so the failure is simulated at the status level"
    try:
        docker_exec(container, "kill -9 1 2>/dev/null || true", timeout_s=10)
        return f"killed pid 1 inside {container}"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return f"tried to kill the service in {container} and could not: {exc}"


def build_registry(pipeline: Pipeline) -> ToolRegistry:
    registry = ToolRegistry(role=STAGE, trace=pipeline.trace)
    register_shell(registry)
    register_http(registry)
    registry.register(
        name="write_expectations_contract",
        description=(
            "Write the expectations contract. Do this before you run anything "
            "against the target. The judge checks that this file existed before "
            "the exploit started."
        ),
        schema=CONTRACT_SCHEMA,
        handler=write_contract,
    )
    register_finish(registry)
    return registry


def opening_messages(pipeline: Pipeline) -> list[dict[str, Any]]:
    survivors = pipeline.state.get("survivors") or []
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{len(survivors)} candidates survived the refuter panel:\n\n"
                f"{json.dumps(survivors, indent=2, default=str)}\n\n"
                f"Target, from the attacker container: {target_line(pipeline.task)}\n\n"
                "Write the expectations contract first, then prepare the target."
            ),
        },
    ]


def run(pipeline: Pipeline) -> StageStatus:
    outcome = run_role(
        role=STAGE,
        pipeline=pipeline,
        registry=build_registry(pipeline),
        messages=opening_messages(pipeline),
    )

    readiness = check_readiness(pipeline.task, pipeline.trace)
    pipeline.state["readiness"] = dataclasses.asdict(readiness)

    if pipeline.break_harness:
        detail = break_service(pipeline)
        pipeline.trace.note(
            "--break-harness: the service was killed after the readiness check",
            stage=STAGE,
            detail=detail,
        )
        return pipeline.statuses.write(
            StageStatus(
                stage=STAGE,
                outcome=Outcome.HARNESS_FAILURE,
                detail=f"--break-harness was set: {detail}",
                artifacts={
                    "readiness": readiness.method,
                    "ready": readiness.ready,
                    "broken_deliberately": True,
                },
            )
        )

    status = outcome.to_status(
        STAGE,
        readiness=readiness.method,
        ready=readiness.ready,
        readiness_detail=readiness.detail,
        contract=pipeline.state.get("contract_path"),
    )
    if status.infrastructure or status.outcome in (
        Outcome.BUDGET_EXCEEDED, Outcome.TOOL_PROTOCOL_ERROR
    ):
        # The role itself failed for a harness reason. Keep that.
        return pipeline.statuses.write(status)
    if not readiness.ready:
        # P6/P7: a target that is not up is not a target the exploit can fail
        # against. Harness failure, and the judge is skipped.
        status.outcome = Outcome.HARNESS_FAILURE
        status.detail = f"the target is not ready ({readiness.method}): {readiness.detail}"
        return pipeline.statuses.write(status)
    if not contract_path(pipeline.run_dir).exists():
        # No contract means the exploit role has nothing to work against. That
        # is a build failure, and the judge will not be invoked.
        status.outcome = Outcome.BUILD_FAILURE
        status.detail = (
            "the build role did not write an expectations contract, so there is "
            "nothing for the exploit to be measured against"
        )
    return pipeline.statuses.write(status)
