"""The orchestrator.

    recon -> triage -> refute -> build -> exploit -> judge

Each stage writes a status file and this module branches on the file, never on
an exit code and never on whether the stage's function returned without
raising. A stage that crashes still exits, and a stage that crashes inside a
`try` can exit zero; read that as "completed, nothing found" once and you will
report a target clean because the scanner died.

Usage:

    python -m harness.run --adapter cybench --task <task dir> --budget-usd 5
    python -m harness.run --adapter cybench --task <task dir> --pause-after triage
    python -m harness.run --adapter cybench --task <task dir> --break-harness
    python -m harness.run --task anything      # stub adapter: no containers
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import cybench
from .budget import Budget
from .config import PIPELINE, HarnessConfig, load_config
from .gates.scope import DEFAULT_RULES, ScopeGate
from .gates.verdict import PROOF_TIERS
from .llm import LLMClient
from .pricing import UnknownModelError
from .roles import build as build_role
from .roles import exploit as exploit_role
from .roles import judge as judge_role
from .roles import recon as recon_role
from .roles import refute as refute_role
from .roles import triage as triage_role
from .roles.base import Pipeline
from .sidechannel import SideChannels
from .status import Outcome, StageStatus, StatusStore
from .trace import Trace, redact_text

# Bringing the task's containers up. Not a pipeline stage, but it gets a
# status file so a failed start reads as an environment failure.
SETUP_STAGE = "setup"

# Stages before the judge, in order. The judge is not in this list because
# whether it runs at all is a decision, not a step.
STAGE_FUNCS: list[tuple[str, Callable[[Pipeline], StageStatus]]] = [
    ("recon", recon_role.run),
    ("triage", triage_role.run),
    ("refute", refute_role.run),
    ("build", build_role.run),
    ("exploit", exploit_role.run),
]

# Outcomes that stop the pipeline where they happen. An infrastructure failure
# stops it too, and is handled separately because it also sets failure_origin.
STOP_OUTCOMES = frozenset({Outcome.BUDGET_EXCEEDED, Outcome.TOOL_PROTOCOL_ERROR})


@dataclasses.dataclass
class RunResult:
    """What one run concluded, and how much of it actually ran."""

    run_id: str
    task: str
    verdict: str | None
    failure_origin: str | None
    judge_invoked: bool
    spent_usd: float
    run_dir: Path
    stages: dict[str, str] = dataclasses.field(default_factory=dict)
    paused_after: str | None = None
    # Set when the run ended cleanly before the judge, e.g. no candidate
    # survived the refuter panel. Not a failure, and not a verdict.
    ended_early: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "verdict": self.verdict,
            "failure_origin": self.failure_origin,
            "judge_invoked": self.judge_invoked,
            "spent_usd": round(self.spent_usd, 6),
            "run_dir": str(self.run_dir),
            "stages": self.stages,
            "paused_after": self.paused_after,
            "ended_early": self.ended_early,
            "detail": self.detail,
        }


def build_pipeline(
    *,
    task_id: str,
    config: HarnessConfig,
    run_dir: Path,
    adapter: Any,
    llm: Any = None,
    trace: Trace | None = None,
    scope: ScopeGate | None = None,
    interactive: bool = True,
    break_harness: bool = False,
    notifiers: list[tuple[str, Any]] | None = None,
    previous_tier: str | None = None,
) -> Pipeline:
    """Assemble everything one run needs. Separate from `main` so tests can
    drive the pipeline with a fake client and no filesystem surprises."""
    if trace is None:
        ensure_fresh_run_dir(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    trace = trace or Trace(run_dir / "trace.jsonl")
    side = SideChannels(trace=trace)
    for name, notifier in notifiers or []:
        side.add(name, notifier)

    task = adapter.load(task_id)
    task_id = task.task_id
    pipeline = Pipeline(
        config=config,
        llm=llm if llm is not None else LLMClient(base_url=config.base_url),
        trace=trace,
        budget=Budget(
            prices=config.prices,
            run_cap_usd=config.run_budget_usd,
            role_caps_usd=config.role_caps(),
            trace=trace,
        ),
        statuses=StatusStore(run_dir),
        run_dir=run_dir,
        task=task,
        scope=scope or ScopeGate.from_dict(
            DEFAULT_RULES, task_id=task_id, interactive=interactive, trace=trace
        ),
        side=side,
        interactive=interactive,
        break_harness=break_harness,
    )
    if previous_tier is not None:
        # The rerun ratchet (gate rule 4) only has something to ratchet against
        # when you say what an earlier run reached. Nothing is read from disk
        # behind your back: pass the `proof_tier` from that run's run_end.
        pipeline.state["previous_tier"] = previous_tier
    return pipeline


def run_pipeline(
    pipeline: Pipeline,
    *,
    task_id: str,
    adapter: Any,
    pause_after: str | None = None,
    run_id: str | None = None,
) -> RunResult:
    """Run the stages, branch on their status files, and decide the verdict."""
    run_id = run_id or uuid.uuid4().hex[:12]
    trace = pipeline.trace
    config = pipeline.config

    trace.run_start(
        run_id=run_id,
        task=task_id,
        config={
            **config.for_trace(),
            "adapter": getattr(adapter, "name", type(adapter).__name__),
            "task": _task_for_trace(pipeline.task),
            "break_harness": pipeline.break_harness,
            "interactive": pipeline.interactive,
            "scope_rules": len(pipeline.scope.rules),
            "pause_after": pause_after,
        },
    )
    pipeline.side.notify("run_start", run_id=run_id, task=task_id)

    result = RunResult(
        run_id=run_id,
        task=task_id,
        verdict=None,
        failure_origin=None,
        judge_invoked=False,
        spent_usd=0.0,
        run_dir=pipeline.run_dir,
    )

    stopped_at: tuple[str, Outcome] | None = None

    try:
        stopped_at = _start_task(pipeline, adapter, result)

        if stopped_at is None:
            for stage, func in STAGE_FUNCS:
                status = _run_stage(pipeline, stage, func)
                result.stages[stage] = str(status.outcome)

                if status.infrastructure or status.outcome in STOP_OUTCOMES:
                    stopped_at = (stage, status.outcome)
                    break

                if stage == "refute" and not pipeline.state.get("survivors"):
                    # Nothing to build, exploit or judge. Spending on those
                    # stages now would buy a verdict about nothing.
                    result.ended_early = "no_survivors"
                    result.detail = (
                        "no candidate survived the refuter panel, so build, exploit "
                        "and judge were skipped"
                    )
                    trace.note(result.detail, stage=stage)
                    break

                if pause_after == stage and not _continue_after(stage, pipeline):
                    result.paused_after = stage
                    result.detail = f"paused after {stage} and not resumed"
                    break

        pipeline.budget.snapshot("run")

        # --- decide whether the judge runs at all ---------------------------
        origin = pipeline.statuses.failure_origin()
        if origin is not None:
            stage, outcome = origin
            result.verdict = "unconfirmed"
            result.failure_origin = str(outcome)
            result.detail = (
                f"stage {stage!r} failed for an infrastructure reason ({outcome}), so "
                f"the judge was not invoked. Nothing here is evidence that the "
                f"candidate is not real."
            )
            trace.note(result.detail, stage=stage, failure_origin=str(outcome))
        elif stopped_at is not None:
            stage, outcome = stopped_at
            result.verdict = "unconfirmed"
            result.failure_origin = str(outcome)
            result.detail = (
                f"the run stopped at {stage!r} with {outcome}, which is a harness "
                f"status. The judge was not invoked."
            )
        elif result.paused_after is not None or result.ended_early is not None:
            trace.note(result.detail)
        else:
            status = _run_stage(pipeline, "judge", judge_role.run)
            result.judge_invoked = True
            result.stages["judge"] = str(status.outcome)
            verdict = pipeline.state.get("verdict") or {}
            if status.infrastructure or status.outcome in STOP_OUTCOMES:
                # The judge itself hit a provider error, a crash or the budget.
                # That keeps its classification; it is not "the judge said no".
                result.verdict = "unconfirmed"
                result.failure_origin = str(status.outcome)
                result.detail = (
                    f"the judge stage ended with {status.outcome}, so there is no "
                    f"verdict. That is a harness status, not a judgement."
                )
            else:
                result.verdict = verdict.get("verdict") or "unconfirmed"
                if not verdict:
                    result.failure_origin = "judge_incomplete"
                    result.detail = "the judge produced no verdict the gate would accept"
    finally:
        _stop_task(pipeline, adapter)
        pipeline.budget.snapshot("run")
        result.spent_usd = pipeline.budget.spent_usd

    trace.run_end(
        run_id=run_id,
        verdict=result.verdict,
        failure_origin=result.failure_origin,
        spent_usd=result.spent_usd,
        judge_invoked=result.judge_invoked,
        stages=result.stages,
        proof_tier=(pipeline.state.get("verdict") or {}).get("proof_tier"),
        severity=(pipeline.state.get("verdict") or {}).get("severity"),
    )
    pipeline.side.notify("run_end", **result.as_dict())
    pipeline.side.render("summary", _render_summary, result)
    return result


def _run_stage(
    pipeline: Pipeline, stage: str, func: Callable[[Pipeline], StageStatus]
) -> StageStatus:
    """Run one stage and return the status *file*, never the return value.

    A crash becomes a harness_failure status; a stage that wrote nothing gets
    one written for it. The judge goes through here too.
    """
    trace = pipeline.trace
    started = time.time()
    try:
        func(pipeline)
    except (KeyboardInterrupt, SystemExit):
        # Record where we were, then let the interrupt through. A stopped run
        # should leave a readable trace, not a mystery.
        pipeline.statuses.write(
            StageStatus(
                stage=stage,
                outcome=Outcome.HARNESS_FAILURE,
                detail="interrupted by the operator",
                started_at=started,
            )
        )
        trace.note("interrupted by the operator", stage=stage)
        raise
    except BaseException as exc:  # noqa: BLE001 - a crashed stage is a status, not a traceback
        pipeline.statuses.write(
            StageStatus(
                stage=stage,
                outcome=Outcome.HARNESS_FAILURE,
                detail=redact_text(f"{type(exc).__name__}: {exc}"),
                started_at=started,
            )
        )

    # Read the file back. Not the return value: the file is the contract.
    status = pipeline.statuses.read(stage)
    if status is None:
        status = pipeline.statuses.write(
            StageStatus(
                stage=stage,
                outcome=Outcome.HARNESS_FAILURE,
                detail="the stage wrote no status file, so it never got far enough "
                       "to say anything",
                started_at=started,
            )
        )
    trace.stage_status(stage=stage, outcome=str(status.outcome), detail=status.detail)
    pipeline.side.notify("stage_end", stage=stage, outcome=str(status.outcome))
    return status


def _start_task(pipeline: Pipeline, adapter: Any, result: "RunResult") -> tuple[str, Outcome] | None:
    """Bring the task up. A failure here is an environment failure with its
    own status file, and nothing else runs."""
    started = time.time()
    try:
        adapter.start(pipeline.task)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001
        status = pipeline.statuses.write(
            StageStatus(
                stage=SETUP_STAGE,
                outcome=Outcome.ENVIRONMENT_FAILURE,
                detail=redact_text(f"the task could not be started: {type(exc).__name__}: {exc}"),
                started_at=started,
            )
        )
        result.stages[SETUP_STAGE] = str(status.outcome)
        pipeline.trace.stage_status(
            stage=SETUP_STAGE, outcome=str(status.outcome), detail=status.detail
        )
        return SETUP_STAGE, status.outcome
    return None


def _stop_task(pipeline: Pipeline, adapter: Any) -> None:
    """Tear the task down whatever happened. A teardown failure is logged and
    swallowed: it must not replace the run's real result."""
    try:
        adapter.stop(pipeline.task)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001
        pipeline.trace.note(
            "task teardown failed; containers may still be running",
            error=redact_text(f"{type(exc).__name__}: {exc}"),
        )


def ensure_fresh_run_dir(run_dir: Path) -> None:
    """Refuse a run directory that already has something in it.

    Stale `status/*.json` or `expectations.json` from an earlier run would
    satisfy this run's gates.
    """
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(
            f"run directory {run_dir} is not empty. Its status files and contract "
            f"would be read as this run's evidence. Pick a new --run-dir."
        )


def _task_for_trace(task: Any) -> dict[str, Any]:
    describe = getattr(task, "for_trace", None)
    return describe() if callable(describe) else {"task_id": getattr(task, "task_id", None)}


def _continue_after(stage: str, pipeline: Pipeline) -> bool:
    """`--pause-after`: hold the run so a facilitator can look at it."""
    pipeline.trace.note(f"paused after {stage}", stage=stage)
    print(f"\n--- paused after {stage} ---")
    print(f"status:  {pipeline.statuses.path_for(stage)}")
    print(f"trace:   {pipeline.trace.path}")
    print(f"spent:   ${pipeline.budget.spent_usd:.4f}")
    if not pipeline.interactive:
        print("not interactive, so the run stops here.")
        return False
    return input("continue? [Y/n] ").strip().lower() not in ("n", "no")


def _render_summary(result: RunResult) -> None:
    """Printed through the side-channel wrapper, so a bug here loses the
    summary and not the run."""
    print("\n--- run summary ---")
    for stage in (SETUP_STAGE, *PIPELINE):
        outcome = result.stages.get(stage)
        if outcome:
            print(f"  {stage:<8} {outcome}")
    print(f"  verdict  {result.verdict}")
    if result.failure_origin:
        print(f"  origin   {result.failure_origin}")
    if result.ended_early:
        print(f"  ended    {result.ended_early}")
    print(f"  spent    ${result.spent_usd:.4f}")
    print(f"  trace    {result.run_dir / 'trace.jsonl'}")
    if result.detail:
        print(f"\n  {result.detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness.run",
        description="Run one Cybench task through the harness.",
    )
    parser.add_argument(
        "--task", required=True, help="Cybench task id, or the path to a task directory"
    )
    parser.add_argument("--roles", default="config/roles.yaml", help="role to model map")
    parser.add_argument("--prices", default=None, help="price table, defaults next to --roles")
    parser.add_argument(
        "--budget-usd", type=float, default=None, help="cap for the whole run, in USD"
    )
    parser.add_argument(
        "--pause-after",
        choices=PIPELINE[:-1],
        default=None,
        help="stop after this stage so you can inspect the run (any stage before the judge)",
    )
    parser.add_argument(
        "--break-harness",
        action="store_true",
        help="M4 lab: kill the service after the readiness check, on purpose",
    )
    parser.add_argument(
        "--adapter",
        choices=("stub", "cybench"),
        default="stub",
        help="stub runs the pipeline with no containers; cybench needs the task images",
    )
    parser.add_argument(
        "--with-task",
        action="append",
        default=[],
        metavar="TASK",
        help="bring this task up alongside on shared_net and tear it down after "
             "(M5: the out-of-scope decoy). Never the target. Repeatable.",
    )
    parser.add_argument("--run-dir", default=None,
                        help="where to write the trace and statuses; must be empty or new")
    parser.add_argument(
        "--previous-tier",
        choices=PROOF_TIERS,
        default=None,
        help="proof_tier an earlier run of this task reached; the gate will not go below it",
    )
    parser.add_argument("--scope-rules", default=None, help="scope rules YAML (see scope-gate/)")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt. The scope gate denies anything it would have asked about.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(
            roles_path=args.roles,
            prices_path=args.prices,
            run_budget_usd=args.budget_usd,
        )
    except UnknownModelError as exc:
        print(f"\nrefusing to start:\n{exc}\n", file=sys.stderr)
        return 2
    except (FileNotFoundError, KeyError) as exc:
        print(f"\nconfig problem: {exc}\n", file=sys.stderr)
        return 2

    if not config.api_key_present:
        print(
            "\nrefusing to start: HARNESS_API_KEY is not set. Export your OpenRouter "
            "key, or point HARNESS_BASE_URL at a local gateway and set any "
            "placeholder key.\n",
            file=sys.stderr,
        )
        return 2

    task_id = cybench.task_id_of(args.task)
    run_dir = Path(args.run_dir or Path("runs") / f"{task_id}-{int(time.time())}")
    try:
        ensure_fresh_run_dir(run_dir)
    except FileExistsError as exc:
        print(f"\nrefusing to start: {exc}\n", file=sys.stderr)
        return 2
    try:
        adapter = cybench.adapter_named(args.adapter, with_tasks=args.with_task)
    except ValueError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2
    interactive = not args.non_interactive

    trace = Trace(run_dir / "trace.jsonl")
    scope = (
        ScopeGate.from_file(
            args.scope_rules, task_id=task_id, interactive=interactive, trace=trace
        )
        if args.scope_rules
        else ScopeGate.from_dict(
            DEFAULT_RULES, task_id=task_id, interactive=interactive, trace=trace
        )
    )
    if not scope.rules:
        print(
            "note: no scope rules matched this task, so the gate denies everything "
            "that leaves the sandbox. Pass --scope-rules once scope-gate/ is filled in.",
            file=sys.stderr,
        )

    try:
        pipeline = build_pipeline(
            task_id=args.task,
            config=config,
            run_dir=run_dir,
            adapter=adapter,
            trace=trace,
            scope=scope,
            interactive=interactive,
            break_harness=args.break_harness,
            previous_tier=args.previous_tier,
        )
    except (KeyError, ValueError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    result = run_pipeline(pipeline, task_id=task_id, adapter=adapter, pause_after=args.pause_after)
    # The exit code says whether the pipeline got all the way to a judge (or
    # ended cleanly with nothing left to judge). It
    # does not say anything about the verdict, and nothing should read it as
    # if it did: that is P1, and the trace and the status files are the record.
    return 0 if (result.judge_invoked or result.ended_early) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
