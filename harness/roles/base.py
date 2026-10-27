"""The turn loop every role shares, and the bundle of things a stage needs.

Nothing here is an exercise. It is here so the role modules can be short
enough to read during a 25-minute lab.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

from ..budget import Budget, BudgetExceeded
from ..config import HarnessConfig, RoleConfig
from ..gates.scope import ScopeGate
from ..llm import ChatClient
from ..sidechannel import SideChannels
from ..status import Outcome, StageStatus, StatusStore
from ..tools.registry import ToolContext, ToolRegistry
from ..trace import redact_text

# Allow this many model turns that produce no tool call before giving up. One
# nudge is fair; a second silence is an answer.
MAX_SILENT_TURNS = 1


@dataclasses.dataclass
class Pipeline:
    """Everything a stage is handed. One instance per run."""

    config: HarnessConfig
    llm: ChatClient
    trace: Any
    budget: Budget
    statuses: StatusStore
    run_dir: Path
    task: Any
    scope: ScopeGate
    side: SideChannels
    interactive: bool = True
    break_harness: bool = False
    # Carried between stages: observations, candidates, contract path, control
    # result, the gated verdict. Stages read each other through this, never by
    # importing each other.
    state: dict[str, Any] = dataclasses.field(default_factory=dict)

    def role_config(self, role: str) -> RoleConfig:
        return self.config.role(role)

    def context_for(self, role: str) -> ToolContext:
        return ToolContext(
            role=role,
            run_dir=self.run_dir,
            task=self.task,
            scope=self.scope,
            trace=self.trace,
            state=self.state,
        )


@dataclasses.dataclass
class RoleOutcome:
    """How one role's loop ended."""

    role: str
    outcome: Outcome
    detail: str | None = None
    finish: dict[str, Any] | None = None
    turns: int = 0
    tool_calls: int = 0
    last_stop_reason: str | None = None
    messages: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def to_status(self, stage: str | None = None, **artifacts: Any) -> StageStatus:
        return StageStatus(
            stage=stage or self.role,
            outcome=self.outcome,
            detail=self.detail,
            artifacts={
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "last_stop_reason": self.last_stop_reason,
                **({"finish": self.finish} if self.finish else {}),
                **artifacts,
            },
        )


def _split_call(call: dict[str, Any]) -> tuple[str | None, str, Any]:
    """Pull (call_id, tool name, raw arguments) out of one tool call."""
    function = call.get("function") or {}
    return call.get("id"), str(function.get("name") or ""), function.get("arguments")


def _tool_message(call_id: str | None, name: str, content: str) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "tool", "name": name, "content": content}
    if call_id is not None:
        message["tool_call_id"] = call_id
    return message


def run_role(
    *,
    role: str,
    pipeline: Pipeline,
    registry: ToolRegistry,
    messages: list[dict[str, Any]],
    model: str | None = None,
    max_turns: int | None = None,
) -> RoleOutcome:
    """Drive one role until it calls `finish`, runs out, or breaks.

    Returns a RoleOutcome rather than raising, because every way this can end
    is a status the orchestrator has to branch on, including the ugly ones.
    """
    registry.require_finish()
    config = pipeline.role_config(role)
    model = model or config.model
    limit = max_turns if max_turns is not None else config.max_turns
    ctx = pipeline.context_for(role)
    trace = pipeline.trace
    specs = registry.specs()

    if config.system_prompt_suffix and messages and messages[0].get("role") == "system":
        # Prompt A/B: append, never replace, so both arms share the base prompt.
        messages[0] = {
            **messages[0],
            "content": f"{messages[0].get('content') or ''}\n\n{config.system_prompt_suffix}",
        }

    trace.role_start(role=role, model=model, prompt_suffix=config.suffix_for_trace())
    started = time.time()

    turns = 0
    tool_calls = 0
    silent_turns = 0
    malformed_calls = 0
    last_stop_reason: str | None = None

    def done(outcome: Outcome, detail: str | None = None, finish: dict[str, Any] | None = None) -> RoleOutcome:
        trace.role_end(role=role, status=str(outcome), detail=detail)
        pipeline.budget.snapshot(role)
        return RoleOutcome(
            role=role,
            outcome=outcome,
            detail=detail,
            finish=finish,
            turns=turns,
            tool_calls=tool_calls,
            last_stop_reason=last_stop_reason,
            messages=messages,
        )

    while turns < limit:
        try:
            response = pipeline.llm.complete(
                model=model,
                messages=messages,
                tools=specs,
                max_output_tokens=config.max_output_tokens,
                reasoning_effort=config.reasoning_effort,
                role=role,
                turn=turns,
            )
        except (KeyboardInterrupt, SystemExit):
            # The operator asked us to stop. Do that.
            raise
        except BaseException as exc:  # noqa: BLE001 - a provider error is not a model verdict
            return done(
                Outcome.ENVIRONMENT_FAILURE,
                redact_text(f"the model call failed: {type(exc).__name__}: {exc}"),
            )

        last_stop_reason = response.stop_reason
        charge = pipeline.budget.charge(
            role=role, model=model, usage=response.usage, stop_reason=response.stop_reason
        )
        trace.model_response(
            role=role,
            turn=turns,
            model=model,
            stop_reason=response.stop_reason,
            raw_stop_reason=getattr(response, "raw_stop_reason", None),
            usage=response.usage,
            cost_usd=charge.cost_usd,
            cumulative_usd=charge.cumulative_usd,
        )
        try:
            pipeline.budget.enforce(role)
        except BudgetExceeded as exc:
            return done(Outcome.BUDGET_EXCEEDED, str(exc))

        messages.append(response.as_message(echo_reasoning=config.echo_reasoning))
        turns += 1

        if not response.tool_calls:
            silent_turns += 1
            if silent_turns > MAX_SILENT_TURNS:
                return done(
                    Outcome.INCOMPLETE,
                    f"the role stopped without calling `finish` "
                    f"(last stop_reason={response.stop_reason!r})",
                )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You made no tool call. Either call a tool, or call `finish` "
                        "with what you have. Nothing is recorded until you do."
                    ),
                }
            )
            continue

        silent_turns = 0
        for call in response.tool_calls:
            call_id, name, raw_arguments = _split_call(call)

            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments or "{}")
                except json.JSONDecodeError as exc:
                    malformed_calls += 1
                    trace.gate_reject(
                        gate="tool_input",
                        rule="malformed_tool_json",
                        message=str(exc),
                        role=role,
                        tool=name,
                        attempt=malformed_calls,
                    )
                    if malformed_calls > 1:
                        # Twice is not a blip. This is a harness status, not a
                        # finding: the model never got to say anything.
                        return done(
                            Outcome.TOOL_PROTOCOL_ERROR,
                            f"the model produced unparseable tool-call JSON twice "
                            f"(last error: {exc})",
                        )
                    messages.append(
                        _tool_message(
                            call_id,
                            name,
                            json.dumps(
                                {
                                    "error": f"your tool-call arguments were not valid "
                                             f"JSON: {exc}",
                                    "hint": "send one JSON object matching the tool schema",
                                }
                            ),
                        )
                    )
                    continue
            else:
                arguments = raw_arguments

            trace.tool_call(
                role=role, turn=turns - 1, tool=name, call_id=call_id, arguments=arguments
            )
            result = registry.call(name, arguments, ctx)
            tool_calls += 1
            trace.tool_result(
                role=role,
                turn=turns - 1,
                tool=name,
                call_id=call_id,
                ok=result.ok,
                content=result.content,
                error=result.error,
            )
            messages.append(_tool_message(call_id, name, result.for_model()))

            if result.finish:
                outcome = Outcome.COMPLETED
                payload = result.payload or {}
                if payload.get("outcome") == "blocked":
                    # The role says something outside its control stopped it.
                    # Believe it, and do not let it count as a clean stage.
                    outcome = Outcome.INCOMPLETE
                return done(
                    outcome,
                    payload.get("summary"),
                    finish=payload,
                )

    return done(
        Outcome.INCOMPLETE,
        f"the role hit its turn limit of {limit} without calling `finish` "
        f"(last stop_reason={last_stop_reason!r}, {time.time() - started:.0f}s)",
    )
