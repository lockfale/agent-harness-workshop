"""Stage status files. The orchestrator reads these, never an exit code.

A stage that crashes still exits. Worse, a stage that crashes inside a `try`
can exit zero. Read that as "completed, nothing found" and you will report a
target clean because the scanner died.

So every stage writes a status file, and `run.py` branches on the file. An
absent status file is itself a status: the stage never got far enough to say
anything, which is a harness failure.
"""

from __future__ import annotations

import dataclasses
import json
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

STATUS_DIRNAME = "status"


class Outcome(StrEnum):
    """How a stage ended.

    Four of these are infrastructure, and they are the reason this enum is not
    just a bool. An exploit that did not land and an exploit that never got to
    run are different facts, and only one of them is evidence.
    """

    COMPLETED = "completed"
    # The role stopped talking without calling `finish`. Not a failure, not a
    # success: we do not know what it concluded.
    INCOMPLETE = "incomplete"
    # The model could not produce usable tool-call JSON twice in a row.
    TOOL_PROTOCOL_ERROR = "tool_protocol_error"
    # We stopped it ourselves, on cost.
    BUDGET_EXCEEDED = "budget_exceeded"

    # --- infrastructure origin. The judge is skipped for all four. ---
    BUILD_FAILURE = "build_failure"
    HARNESS_FAILURE = "harness_failure"
    ENVIRONMENT_FAILURE = "environment_failure"
    TIMEOUT = "timeout"


INFRASTRUCTURE_OUTCOMES: frozenset[Outcome] = frozenset(
    {
        Outcome.BUILD_FAILURE,
        Outcome.HARNESS_FAILURE,
        Outcome.ENVIRONMENT_FAILURE,
        Outcome.TIMEOUT,
    }
)

# A budget cut and a protocol error are not evidence either, but they are our
# decision rather than a broken environment, so they are tracked separately.
HARNESS_STATUSES: frozenset[Outcome] = frozenset(
    {Outcome.TOOL_PROTOCOL_ERROR, Outcome.BUDGET_EXCEEDED}
)


def is_infrastructure(outcome: Outcome | str | None) -> bool:
    """True when this outcome means the judge must not be invoked."""
    if outcome is None:
        return False
    try:
        return Outcome(outcome) in INFRASTRUCTURE_OUTCOMES
    except ValueError:
        return False


@dataclasses.dataclass
class StageStatus:
    """One stage's status file."""

    stage: str
    outcome: Outcome
    detail: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    artifacts: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def infrastructure(self) -> bool:
        return is_infrastructure(self.outcome)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.COMPLETED

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "outcome": str(self.outcome),
            "detail": self.detail,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "artifacts": self.artifacts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StageStatus":
        return cls(
            stage=data["stage"],
            outcome=Outcome(data["outcome"]),
            detail=data.get("detail"),
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            artifacts=data.get("artifacts") or {},
        )


class StatusStore:
    """Status files under `<run_dir>/status/<stage>.json`."""

    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir)
        self.dir = self.run_dir / STATUS_DIRNAME
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, stage: str) -> Path:
        return self.dir / f"{stage}.json"

    def write(self, status: StageStatus) -> StageStatus:
        if status.ended_at is None:
            status.ended_at = time.time()
        self.path_for(status.stage).write_text(
            json.dumps(status.as_dict(), indent=2, default=str) + "\n", encoding="utf-8"
        )
        return status

    def read(self, stage: str) -> StageStatus | None:
        """The stage's status, or None if it never wrote one."""
        path = self.path_for(stage)
        if not path.exists():
            return None
        try:
            return StageStatus.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, ValueError):
            # An unreadable status file is a harness failure, not a clean stage.
            return StageStatus(
                stage=stage,
                outcome=Outcome.HARNESS_FAILURE,
                detail=f"status file at {path} could not be parsed",
            )

    def all(self) -> dict[str, StageStatus]:
        out: dict[str, StageStatus] = {}
        for path in sorted(self.dir.glob("*.json")):
            status = self.read(path.stem)
            if status is not None:
                out[path.stem] = status
        return out

    def failure_origin(self) -> tuple[str, Outcome] | None:
        """The first stage that failed for an infrastructure reason, if any.

        Ordered by when the stage finished, so the earliest real cause wins
        rather than whatever happened to be read first.
        """
        failures = [
            (stage, status)
            for stage, status in self.all().items()
            if status.infrastructure
        ]
        if not failures:
            return None
        failures.sort(key=lambda pair: pair[1].ended_at or 0.0)
        stage, status = failures[0]
        return stage, status.outcome
