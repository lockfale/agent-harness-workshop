"""M4: the judge. Records a verdict through a gated tool, or not at all.

There is no other way for this role to express a verdict. It cannot write one
in prose, and prose is not read. `record_verdict` validates against the schema
and then runs all six rules in `gates/verdict.py`; a rejection comes back as a
tool error naming the rule, and the model has to answer it.

Two things this role does not do:

* It is never invoked after an infrastructure failure. `run.py` checks the
  stage statuses first. A build that did not build and a timeout are not
  evidence that the bug is not there, and sending them to a judge gets them
  scored as "exploit failed", which is how real bugs get discarded.
* It does not share a model lineage with the exploit role, where the config
  can manage it. A generator judging its own output agrees with itself.

Abliterated models do not belong here either, for the same reason as the
refuter panel: the calibration damage is the problem, not the refusals.

What you fill in
----------------
`SYSTEM_PROMPT`. Describe the proof tiers and what distinguishes them, and
make clear that a rejection is a rule it must satisfy rather than an opinion
it can argue with. Attendees usually try arguing with it once.
"""

from __future__ import annotations

import json
from typing import Any

from ..gates.verdict import (
    VERDICT_SCHEMA,
    Evidence,
    GatedVerdict,
    VerdictRejected,
    check_verdict,
)
from ..status import Outcome, StageStatus
from ..tools.finish import register_finish
from ..tools.registry import ToolContext, ToolRegistry, ToolResult
from .base import Pipeline, run_role
from .build import contract_path

STAGE = "judge"

MAX_GATE_REJECTIONS = 4

# TODO(M4): write this prompt.
#
# Worth including: the tier ladder and what each rung requires; that severity
# is derived from the tier and not something it sets; that `unconfirmed` is a
# perfectly good answer and is the right one more often than it feels like.
SYSTEM_PROMPT = """TODO(M4): the judge's system prompt goes here.
"""


def gather_evidence(pipeline: Pipeline) -> Evidence:
    """What the gate checks against. None of it comes from the model."""
    control = pipeline.state.get("control") or {}
    origin = pipeline.statuses.failure_origin()
    path = contract_path(pipeline.run_dir)
    return Evidence(
        contract_path=path,
        contract_written_at=pipeline.state.get("contract_written_at"),
        exploit_started_at=pipeline.state.get("exploit_started_at"),
        control_ran=bool(control.get("ran")),
        control_clean=bool(control.get("clean")),
        control_detail=str(control.get("detail") or ""),
        previous_tier=pipeline.state.get("previous_tier"),
        infrastructure_failure=(origin[0], str(origin[1])) if origin else None,
    )


def record_verdict(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Gate the verdict. Plumbing: the rules live in gates/verdict.py."""
    evidence: Evidence = ctx.state["_judge_evidence"]
    rejections = ctx.state.setdefault("_judge_rejections", [])

    try:
        gated: GatedVerdict = check_verdict(arguments, evidence)
    except VerdictRejected as rejection:
        rejections.append({"rule": rejection.rule, "message": rejection.message})
        if ctx.trace is not None:
            ctx.trace.gate_reject(
                gate="verdict",
                rule=rejection.rule,
                message=rejection.message,
                role=ctx.role,
                proposed_verdict=arguments.get("verdict"),
                proposed_tier=arguments.get("proof_tier"),
            )
        if len(rejections) >= MAX_GATE_REJECTIONS:
            return ToolResult.failure(
                f"{rejection.message}\n\nThat is {len(rejections)} rejections. The "
                f"gate will not change. Record a verdict that satisfies it, or "
                f"finish with outcome 'blocked' and say why you cannot.",
                rule=rejection.rule,
            )
        return ToolResult.failure(rejection.message, rule=rejection.rule)

    ctx.state["verdict"] = gated.as_dict()
    if ctx.trace is not None:
        for warning in gated.warnings:
            ctx.trace.event("gate_warning", gate="verdict", role=ctx.role, message=warning)
    return ToolResult(
        content={
            "accepted": True,
            "verdict": gated.verdict,
            "proof_tier": gated.proof_tier,
            "severity": gated.severity,
            "adjustments": gated.adjustments,
            "warnings": gated.warnings,
        }
    )


def build_registry(pipeline: Pipeline) -> ToolRegistry:
    registry = ToolRegistry(role=STAGE, trace=pipeline.trace)
    registry.register(
        name="record_verdict",
        description=(
            "Record your verdict. This is the only way to record one. The handler "
            "enforces rules in code and will reject a verdict the evidence does "
            "not support, naming the rule. Fix it and call again."
        ),
        schema=VERDICT_SCHEMA,
        handler=record_verdict,
    )
    register_finish(registry)
    return registry


def opening_messages(pipeline: Pipeline) -> list[dict[str, Any]]:
    state = pipeline.state
    brief = {
        "contract": state.get("contract"),
        "attempts": state.get("attempts") or [],
        "negative_control": state.get("control") or {},
        "readiness": state.get("readiness") or {},
        "surviving_candidates": state.get("survivors") or [],
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Judge this run. Everything below was observed by the harness, not "
                "reported by the role that produced it.\n\n"
                f"{json.dumps(brief, indent=2, default=str)}"
            ),
        },
    ]


def run(pipeline: Pipeline) -> StageStatus:
    """Run the judge. `run.py` is responsible for not calling this after an
    infrastructure failure; the gate's first rule is the backstop, not the
    policy."""
    pipeline.state["_judge_evidence"] = gather_evidence(pipeline)
    pipeline.state["_judge_rejections"] = []

    outcome = run_role(
        role=STAGE,
        pipeline=pipeline,
        registry=build_registry(pipeline),
        messages=opening_messages(pipeline),
    )

    verdict = pipeline.state.get("verdict")
    rejections = pipeline.state.get("_judge_rejections") or []
    status = outcome.to_status(
        STAGE,
        verdict=(verdict or {}).get("verdict"),
        proof_tier=(verdict or {}).get("proof_tier"),
        severity=(verdict or {}).get("severity"),
        gate_rejections=len(rejections),
        rejected_rules=[r["rule"] for r in rejections],
    )
    if verdict is None and status.outcome is Outcome.COMPLETED:
        # It called `finish` without ever getting a verdict through the gate.
        status.outcome = Outcome.INCOMPLETE
        status.detail = (
            "the judge finished without a verdict the gate would accept "
            f"({len(rejections)} rejection(s))"
        )
    pipeline.state.pop("_judge_evidence", None)
    return pipeline.statuses.write(status)
