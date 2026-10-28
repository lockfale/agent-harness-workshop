"""M3: the refuter panel. Several models try to kill each candidate.

Two rules, both in code:

* **Majority refutation kills.** If most of the panel can refute a candidate,
  it is gone. One confident refutation is not enough and one confident defence
  is not enough either.
* **Uncertain counts as refuted.** A panelist that cannot decide has not
  defended the candidate. Treating "I am not sure" as a pass is how a list of
  thirty candidates stays thirty candidates.

And one rule in config: the panel should not be the model that produced the
candidate. A generator asked to judge its own output agrees with itself. If you
only have one model available, say so in your write-up rather than pretending
the panel was independent.

Abliterated models do not belong here. Removing refusals also damages
calibration: a model that will say anything will confirm a bug in a file that
has none, and the same model misses real ones. The failure is not that it is
too willing, it is that its confidence stopped meaning anything, and this role
is entirely about confidence.

What you fill in
----------------
1. `SYSTEM_PROMPT`. Ask for refutation, not review. The panelist's job is to
   find the reason this is not real.
2. Nothing else. The aggregation is plumbing, and it is the part you want to
   be boringly deterministic.
"""

from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..status import Outcome, StageStatus, is_infrastructure
from ..tools.finish import register_finish
from ..tools.registry import ToolContext, ToolRegistry, ToolResult
from .base import Pipeline, RoleOutcome, run_role

STAGE = "refute"

# Panelists judge each candidate at the same time, so a three-model panel takes
# about as long as its slowest member rather than all three in a row. Set to 1
# to run them one after another.
PANEL_WORKERS = 3

# TODO(M3): write this prompt.
#
# Ask the panelist to try to refute the candidate and to say `uncertain` when
# it cannot tell. Make clear that `uncertain` kills the candidate, so there is
# no incentive to hedge, and no incentive to bluff either.
SYSTEM_PROMPT = """TODO(M3): the refuter's system prompt goes here.
"""

JUDGEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "judgement": {
            "type": "string",
            "enum": ["refuted", "stands", "uncertain"],
            "description": (
                "refuted: you found the reason it is not real. stands: you tried "
                "and could not refute it. uncertain: you cannot tell, which "
                "counts as refuted."
            ),
        },
        "reason": {"type": "string", "minLength": 1},
    },
    "required": ["judgement", "reason"],
    "additionalProperties": False,
}


def record_judgement(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """One panelist's verdict on one candidate. Plumbing, not an exercise."""
    ctx.state.setdefault("_panel_judgement", {})[ctx.role] = dict(arguments)
    return ToolResult(content={"recorded": arguments["judgement"]})


@dataclasses.dataclass
class PanelResult:
    """How one candidate fared."""

    candidate: dict[str, Any]
    judgements: list[dict[str, Any]]
    killed: bool
    refuted_votes: int
    stands_votes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim": self.candidate.get("claim"),
            "dedup_key": self.candidate.get("dedup_key"),
            "killed": self.killed,
            "refuted_votes": self.refuted_votes,
            "stands_votes": self.stands_votes,
            "judgements": self.judgements,
        }


def aggregate(judgements: list[dict[str, Any]]) -> tuple[bool, int, int]:
    """Majority refutes, and uncertain counts as refuted.

    Returns (killed, refuted_votes, stands_votes). A panel with no usable
    judgements kills the candidate: no evidence that it stands is not the same
    as evidence that it does.
    """
    refuted = sum(1 for j in judgements if j.get("judgement") in ("refuted", "uncertain"))
    stands = sum(1 for j in judgements if j.get("judgement") == "stands")
    total = refuted + stands
    if total == 0:
        return True, refuted, stands
    return refuted * 2 > total, refuted, stands


def halts_panel(outcome: Outcome) -> bool:
    """Outcomes that stop the panel rather than counting as `uncertain`.

    `incomplete` (the panelist stopped without judging) still counts as
    uncertain: it got to look and declined to defend the candidate. These did
    not get to look.
    """
    return is_infrastructure(outcome) or outcome in (
        Outcome.BUDGET_EXCEEDED,
        Outcome.TOOL_PROTOCOL_ERROR,
    )


def build_registry(pipeline: Pipeline) -> ToolRegistry:
    registry = ToolRegistry(role=STAGE, trace=pipeline.trace)
    registry.register(
        name="record_judgement",
        description="Record your judgement on this candidate, then call finish.",
        schema=JUDGEMENT_SCHEMA,
        handler=record_judgement,
    )
    register_finish(registry)
    return registry


def opening_messages(pipeline: Pipeline, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    import json

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Try to refute this candidate. Record one judgement, then finish.\n\n"
                f"{json.dumps(candidate, indent=2, default=str)}"
            ),
        },
    ]


def judge_once(
    pipeline: Pipeline, candidate: dict[str, Any], model: str, max_turns: int
) -> tuple[RoleOutcome, dict[str, Any] | None]:
    """One panelist on one candidate, on its own copy of the stage state.

    Panelists run at the same time, so each records its judgement into a
    private copy; one panelist's vote can never land in another's slot.
    """
    private = dataclasses.replace(pipeline, state=dict(pipeline.state))
    private.state.pop("_panel_judgement", None)
    outcome = run_role(
        role=STAGE,
        pipeline=private,
        registry=build_registry(private),
        messages=opening_messages(private, candidate),
        model=model,
        max_turns=max_turns,
    )
    return outcome, (private.state.get("_panel_judgement") or {}).get(STAGE)


def run(pipeline: Pipeline) -> StageStatus:
    config = pipeline.role_config(STAGE)
    panel = config.panel or [config.model]
    candidates = pipeline.state.get("candidates") or []
    workers = max(1, min(PANEL_WORKERS, len(panel)))

    if len(panel) == 1:
        pipeline.trace.note(
            "the refuter panel has one model, so it is not independent of the "
            "generator. Say so when you report the result.",
            stage=STAGE,
            panel=panel,
        )

    results: list[PanelResult] = []
    halted: tuple[str, Any] | None = None
    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    for candidate in candidates:
        judgements: list[dict[str, Any]] = []
        if pool is not None:
            # The panelists are independent, so they judge at the same time.
            # Results are read back in panel order, so the record does not
            # depend on which one finished first.
            futures = [
                pool.submit(judge_once, pipeline, candidate, model, config.max_turns)
                for model in panel
            ]
            answers = (future.result() for future in futures)
        else:
            answers = (
                judge_once(pipeline, candidate, model, config.max_turns) for model in panel
            )
        for model, (outcome, recorded) in zip(panel, answers):
            if halts_panel(outcome.outcome):
                # A panelist that never got to judge is not a vote. A provider
                # error, a crash, a protocol error or a budget breach stops the
                # panel, and the stage carries that outcome instead of
                # pretending the candidates were weighed.
                halted = (model, outcome)
                break
            if recorded is None:
                # A panelist that produced nothing usable is not a vote for
                # the candidate. Record why and move on.
                recorded = {
                    "judgement": "uncertain",
                    "reason": f"no judgement recorded ({outcome.outcome}: {outcome.detail})",
                }
            judgements.append({"model": model, **recorded})

        if halted is not None:
            break
        killed, refuted, stands = aggregate(judgements)
        results.append(
            PanelResult(
                candidate=candidate,
                judgements=judgements,
                killed=killed,
                refuted_votes=refuted,
                stands_votes=stands,
            )
        )
    if pool is not None:
        pool.shutdown(wait=True)

    if halted is not None:
        model, outcome = halted
        pipeline.state["panel_results"] = [r.as_dict() for r in results]
        pipeline.state["survivors"] = []
        pipeline.state.pop("_panel_judgement", None)
        return pipeline.statuses.write(
            outcome.to_status(
                STAGE,
                panel=panel,
                halted_by=model,
                candidates=len(candidates),
                judged=len(results),
                independent_panel=len(panel) > 1,
            )
        )

    survivors = [r.candidate for r in results if not r.killed]
    pipeline.state["panel_results"] = [r.as_dict() for r in results]
    pipeline.state["survivors"] = survivors
    pipeline.state.pop("_panel_judgement", None)

    return pipeline.statuses.write(
        StageStatus(
            stage=STAGE,
            outcome=Outcome.COMPLETED,
            detail=f"{len(survivors)} of {len(candidates)} candidates survived the panel",
            artifacts={
                "panel": panel,
                "candidates": len(candidates),
                "survivors": len(survivors),
                "independent_panel": len(panel) > 1,
            },
        )
    )
