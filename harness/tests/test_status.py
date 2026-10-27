"""P1: status files, and which outcomes mean the judge must be skipped."""

from __future__ import annotations

from harness.status import (
    HARNESS_STATUSES,
    INFRASTRUCTURE_OUTCOMES,
    Outcome,
    StageStatus,
    StatusStore,
    is_infrastructure,
)


def test_the_four_infrastructure_outcomes():
    assert INFRASTRUCTURE_OUTCOMES == {
        Outcome.BUILD_FAILURE,
        Outcome.HARNESS_FAILURE,
        Outcome.ENVIRONMENT_FAILURE,
        Outcome.TIMEOUT,
    }
    assert Outcome.INCOMPLETE not in INFRASTRUCTURE_OUTCOMES
    assert Outcome.COMPLETED not in INFRASTRUCTURE_OUTCOMES
    assert HARNESS_STATUSES.isdisjoint(INFRASTRUCTURE_OUTCOMES)


def test_is_infrastructure_accepts_strings_and_shrugs_at_junk():
    assert is_infrastructure("timeout") is True
    assert is_infrastructure(Outcome.TIMEOUT) is True
    assert is_infrastructure("completed") is False
    assert is_infrastructure("something-else") is False
    assert is_infrastructure(None) is False


def test_a_status_round_trips(tmp_path):
    store = StatusStore(tmp_path)
    store.write(
        StageStatus(
            stage="build",
            outcome=Outcome.COMPLETED,
            detail="ready",
            artifacts={"readiness": "in_container"},
        )
    )
    read_back = store.read("build")
    assert read_back.outcome is Outcome.COMPLETED
    assert read_back.artifacts["readiness"] == "in_container"
    assert read_back.ended_at is not None


def test_a_stage_that_wrote_nothing_reads_as_none(tmp_path):
    assert StatusStore(tmp_path).read("recon") is None


def test_an_unparseable_status_file_is_a_harness_failure_not_a_clean_stage(tmp_path):
    store = StatusStore(tmp_path)
    store.path_for("recon").write_text("{ this is not json", encoding="utf-8")

    status = store.read("recon")
    assert status.outcome is Outcome.HARNESS_FAILURE
    assert status.infrastructure is True


def test_failure_origin_picks_the_earliest_infrastructure_failure(tmp_path):
    store = StatusStore(tmp_path)
    store.write(StageStatus(stage="recon", outcome=Outcome.COMPLETED, ended_at=1.0))
    store.write(StageStatus(stage="exploit", outcome=Outcome.TIMEOUT, ended_at=3.0))
    store.write(StageStatus(stage="build", outcome=Outcome.BUILD_FAILURE, ended_at=2.0))

    assert store.failure_origin() == ("build", Outcome.BUILD_FAILURE)


def test_no_infrastructure_failure_means_no_origin(tmp_path):
    store = StatusStore(tmp_path)
    store.write(StageStatus(stage="recon", outcome=Outcome.INCOMPLETE, ended_at=1.0))
    store.write(StageStatus(stage="triage", outcome=Outcome.COMPLETED, ended_at=2.0))
    assert store.failure_origin() is None


def test_all_lists_every_written_stage(tmp_path):
    store = StatusStore(tmp_path)
    store.write(StageStatus(stage="recon", outcome=Outcome.COMPLETED))
    store.write(StageStatus(stage="triage", outcome=Outcome.COMPLETED))
    assert set(store.all()) == {"recon", "triage"}
