"""P1 and P6 end to end: status files, the judge skip, and fail-fast pricing."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from harness.config import load_config
from harness.cybench import StubAdapter
from harness.pricing import UnknownModelError, Usage
from harness.run import build_pipeline, can_ask, main, run_pipeline
from harness.status import Outcome

from .conftest import FakeLLM, finish_turn, tool_call, turn, write_config

CONTRACT = {
    "candidate": "the parser accepts a frame longer than the buffer",
    "expected_observation": "a sanitizer report naming a heap overflow in parse_frame",
    "falsifier": "the same input parses cleanly under the sanitizer",
    "benign_input": "a well formed frame, which should parse and return 0",
}

VERDICT = {
    "verdict": "unconfirmed",
    "proof_tier": "inconclusive",
    "autonomy": 1,
    "precondition": "the service accepts frames on its listening port",
    "required_privileges": "none",
    "privilege_boundary_crossed": False,
    "trust_boundary": "the frame parser is supposed to reject oversized frames",
    "trigger_origin": "real_code_path",
    "reachability_note": "the public request path reaches the parser",
    "reasoning": "the control could not be run, so nothing is confirmed",
}


CANDIDATE = {
    "dedup_key": "abc123",
    "claim": "the frame parser trusts the length field",
    "rank": 1,
    "must_be_true": "the length is used before it is checked",
    "first_check": "send a frame whose length exceeds its body",
    "kill_condition": "the parser rejects the frame",
}

STANDS = {"judgement": "stands", "reason": "could not find a reason it is not real"}


@pytest.fixture(autouse=True)
def minimal_rank_candidate(monkeypatch):
    """`rank_candidate` is an attendee TODO. These tests need candidates to
    reach the later stages, so they use the docstring's minimal version."""
    from harness.roles import triage
    from harness.tools.registry import ToolResult

    def rank(arguments, ctx):
        candidates = ctx.state.setdefault("candidates", [])
        candidates.append(dict(arguments))
        return ToolResult(content={"candidates": len(candidates)})

    monkeypatch.setattr(triage, "rank_candidate", rank)


def working_script() -> dict[str, list]:
    """A scripted run where every role does the minimum right thing."""
    return {
        "recon": [finish_turn("looked around")],
        "triage": [turn(tool_call("rank_candidate", CANDIDATE)), finish_turn("ranked")],
        "refute": [turn(tool_call("record_judgement", STANDS)), finish_turn("judged")],
        "build": [
            turn(tool_call("write_expectations_contract", CONTRACT)),
            finish_turn("contract written"),
        ],
        "exploit": [finish_turn("tried it")],
        "judge": [turn(tool_call("record_verdict", VERDICT)), finish_turn("judged")],
    }


def pipeline_for(tmp_path: Path, llm, **kwargs):
    config = load_config(roles_path=write_config(tmp_path), run_budget_usd=5.0)
    adapter = StubAdapter()
    run_dir = tmp_path / "run"
    pipeline = build_pipeline(
        task_id="test-task",
        config=config,
        run_dir=run_dir,
        adapter=adapter,
        llm=llm,
        interactive=False,
        **kwargs,
    )
    return pipeline, adapter


# --- the full pipeline ----------------------------------------------------


def test_a_clean_run_reaches_the_judge_and_writes_every_status(tmp_path):
    pipeline, adapter = pipeline_for(tmp_path, FakeLLM(working_script()))
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert result.judge_invoked is True
    assert result.verdict == "unconfirmed"
    assert result.failure_origin is None

    for stage in ("recon", "triage", "refute", "build", "exploit", "judge"):
        status = pipeline.statuses.read(stage)
        assert status is not None, f"{stage} wrote no status file"
        assert pipeline.statuses.path_for(stage).exists()

    events = {event["event"] for event in pipeline.trace.events}
    assert {"run_start", "role_start", "role_end", "model_response", "tool_call",
            "tool_result", "stage_status", "run_end"} <= events


def test_the_orchestrator_reads_the_status_file_not_the_return_value(tmp_path):
    """A stage that crashes is recorded as a harness failure, not as clean."""
    pipeline, adapter = pipeline_for(tmp_path, FakeLLM(working_script()))

    import harness.run as run_module

    def exploding_stage(_pipeline):
        raise RuntimeError("the stage blew up before writing anything")

    original = run_module.STAGE_FUNCS
    run_module.STAGE_FUNCS = [("recon", exploding_stage)]
    try:
        result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)
    finally:
        run_module.STAGE_FUNCS = original

    status = pipeline.statuses.read("recon")
    assert status.outcome is Outcome.HARNESS_FAILURE
    assert "blew up" in (status.detail or "")
    assert result.judge_invoked is False
    assert result.verdict == "unconfirmed"
    assert result.failure_origin == "harness_failure"


# --- the M4 lab -----------------------------------------------------------


def test_break_harness_gives_unconfirmed_with_a_failure_origin_and_no_judge(tmp_path):
    llm = FakeLLM(working_script())
    pipeline, adapter = pipeline_for(tmp_path, llm, break_harness=True)

    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert result.verdict == "unconfirmed"
    assert result.failure_origin == "harness_failure"
    assert result.judge_invoked is False
    assert "judge" not in result.stages

    # The judge model was never called.
    assert "judge" not in llm.roles_called()

    # The build stage says why, and the run end says so too.
    build_status = pipeline.statuses.read("build")
    assert build_status.outcome is Outcome.HARNESS_FAILURE
    assert build_status.artifacts["broken_deliberately"] is True
    assert pipeline.statuses.read("exploit") is None

    run_end = pipeline.trace.of_kind("run_end")[0]
    assert run_end["verdict"] == "unconfirmed"
    assert run_end["failure_origin"] == "harness_failure"


def test_an_infrastructure_failure_is_not_recorded_as_a_refutation(tmp_path):
    llm = FakeLLM(working_script())
    pipeline, adapter = pipeline_for(tmp_path, llm, break_harness=True)
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert result.verdict != "refuted"
    assert "Nothing here is evidence that the candidate is not real" in result.detail


def test_a_missing_contract_stops_the_exploit_role_as_a_build_failure(tmp_path):
    script = working_script()
    # The build role finishes without writing the contract.
    script["build"] = [finish_turn("forgot the contract")]
    pipeline, adapter = pipeline_for(tmp_path, FakeLLM(script))

    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert pipeline.statuses.read("build").outcome is Outcome.BUILD_FAILURE
    assert result.judge_invoked is False
    assert result.failure_origin == "build_failure"


# --- side channels in a real run -----------------------------------------


def test_a_broken_notifier_does_not_stop_the_run(tmp_path):
    def broken(event, payload):
        raise RuntimeError("pager is down")

    pipeline, adapter = pipeline_for(
        tmp_path, FakeLLM(working_script()), notifiers=[("pager", broken)]
    )
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert result.judge_invoked is True
    assert result.verdict == "unconfirmed"
    errors = pipeline.trace.of_kind("side_channel_error")
    assert errors, "the notifier failure was not recorded"
    assert all(e["channel"] == "notifier:pager" for e in errors)


# --- pause-after ----------------------------------------------------------


def test_pause_after_stops_a_non_interactive_run_at_that_stage(tmp_path, capsys):
    pipeline, adapter = pipeline_for(tmp_path, FakeLLM(working_script()))
    result = run_pipeline(
        pipeline, task_id="test-task", adapter=adapter, pause_after="triage"
    )

    assert result.paused_after == "triage"
    assert result.judge_invoked is False
    assert pipeline.statuses.read("build") is None
    assert "paused after triage" in capsys.readouterr().out


class _Terminal:
    def isatty(self):
        return True


class _Pipe:
    def isatty(self):
        return False


def test_with_no_terminal_the_harness_never_asks():
    """A coding assistant running the command has no terminal to answer on."""
    assert can_ask(False, _Pipe()) is False
    assert can_ask(False, object()) is False


def test_a_terminal_lets_the_harness_ask_unless_told_not_to():
    assert can_ask(False, _Terminal()) is True
    assert can_ask(True, _Terminal()) is False


# --- fail fast on an unknown model ---------------------------------------


def test_an_unknown_model_fails_before_anything_is_spent(tmp_path):
    roles_path = write_config(tmp_path, model="vendor/not-in-the-table", price_it=False)
    with pytest.raises(UnknownModelError) as caught:
        load_config(roles_path=roles_path)

    message = str(caught.value)
    assert "vendor/not-in-the-table" in message
    assert "prices.yaml" in message


# --- recon that oversteps is flagged --------------------------------------


def test_recon_is_flagged_when_its_own_tools_bring_back_the_success_marker(tmp_path):
    from harness.roles import recon

    pipeline, _ = pipeline_for(tmp_path, FakeLLM())
    pipeline.task.control_marker = "MARK{"
    pipeline.trace.event("tool_result", role="recon", tool="http_request",
                         content={"body": "a page"})
    assert recon.overstepped_into_exploit(pipeline) is False

    # The exploit stage bringing it back is the point of the exploit stage.
    pipeline.trace.event("tool_result", role="exploit", tool="shell",
                         content={"stdout": "MARK{x}"})
    assert recon.overstepped_into_exploit(pipeline) is False

    pipeline.trace.event("tool_result", role="recon", tool="http_request",
                         content={"body": "MARK{x}"})
    assert recon.overstepped_into_exploit(pipeline) is True


def test_no_marker_means_no_overstep_check(tmp_path):
    from harness.roles import recon

    pipeline, _ = pipeline_for(tmp_path, FakeLLM())
    pipeline.task.control_marker = None
    pipeline.trace.event("tool_result", role="recon", tool="shell", content={"stdout": "x"})
    assert recon.overstepped_into_exploit(pipeline) is False


# --- the refuter panel runs in parallel -----------------------------------


class PanelByModel:
    """Answers by model, so the order the panelists run in cannot matter, and
    counts how many panelists were mid-turn at once."""

    VOTES = {"fake/a": "refuted", "fake/b": "stands", "fake/c": "stands"}

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def complete(self, *, model, messages, role=None, **kwargs):
        if any(m.get("role") == "tool" for m in messages):
            return finish_turn("judged")
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.2)
        with self.lock:
            self.active -= 1
        return turn(tool_call("record_judgement",
                              {"judgement": self.VOTES[model], "reason": "from the test"}))


def test_panelists_judge_at_once_and_each_vote_stays_with_its_model(tmp_path, monkeypatch):
    import yaml

    from harness.roles import refute

    monkeypatch.setattr(refute, "PANEL_WORKERS", 3)
    roles_path = write_config(
        tmp_path, extra_models={m: {"input": 0.0, "output": 0.0} for m in PanelByModel.VOTES}
    )
    roles = yaml.safe_load(roles_path.read_text(encoding="utf-8"))
    roles["roles"]["refute"]["panel"] = list(PanelByModel.VOTES)
    roles_path.write_text(yaml.safe_dump(roles), encoding="utf-8")
    llm = PanelByModel()
    pipeline = build_pipeline(
        task_id="test-task",
        config=load_config(roles_path=roles_path, run_budget_usd=5.0),
        run_dir=tmp_path / "run",
        adapter=StubAdapter(),
        llm=llm,
        interactive=False,
    )
    pipeline.state["candidates"] = [{"claim": "one", "rank": 1}, {"claim": "two", "rank": 2}]

    refute.run(pipeline)

    assert llm.peak > 1, "the panelists ran one after another"
    for result in pipeline.state["panel_results"]:
        # Read back in panel order, each vote with the model that cast it.
        assert [(j["model"], j["judgement"]) for j in result["judgements"]] == list(
            PanelByModel.VOTES.items()
        )
        assert result["killed"] is False
    assert "_panel_judgement" not in pipeline.state


def test_only_the_top_ranked_candidates_go_to_the_panel(tmp_path):
    from harness.roles import refute

    pipeline, _ = pipeline_for(tmp_path, FakeLLM(default=turn(
        tool_call("record_judgement", {"judgement": "stands", "reason": "from the test"}))))
    pipeline.state["candidates"] = [
        {"claim": f"c{rank}", "rank": rank} for rank in (5, 2, 4, 1, 3)
    ]

    status = refute.run(pipeline)

    judged = [r["claim"] for r in pipeline.state["panel_results"]]
    assert judged == ["c1", "c2", "c3"]
    assert [c["claim"] for c in pipeline.state["unjudged"]] == ["c4", "c5"]
    assert status.artifacts["judged"] == 3 and status.artifacts["unjudged"] == 2
    assert "2 lower-ranked not judged" in (status.detail or "")


def test_a_panel_model_is_priced_too(tmp_path):
    """A model only ever used by the refuter panel still has to be in the table."""
    import yaml

    roles_path = write_config(tmp_path)
    roles = yaml.safe_load(roles_path.read_text(encoding="utf-8"))
    roles["roles"]["refute"]["panel"] = ["fake/model", "vendor/unpriced-panelist"]
    roles_path.write_text(yaml.safe_dump(roles), encoding="utf-8")

    with pytest.raises(UnknownModelError, match="vendor/unpriced-panelist"):
        load_config(roles_path=roles_path)


def test_main_exits_with_a_pricing_message_for_an_unknown_model(tmp_path, capsys):
    roles_path = write_config(tmp_path, model="vendor/not-in-the-table", price_it=False)
    code = main(
        [
            "--task", "test-task",
            "--roles", str(roles_path),
            "--run-dir", str(tmp_path / "run"),
            "--non-interactive",
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "refusing to start" in err
    assert "prices.yaml" in err


def test_a_local_model_needs_no_price_table_entry(tmp_path):
    config = load_config(roles_path=write_config(tmp_path, model="local/whatever-i-pulled"))
    assert config.role("recon").model == "local/whatever-i-pulled"
    assert config.prices.cost("local/whatever-i-pulled", Usage(input_tokens=10**6)) == 0.0


def test_swapping_a_role_to_a_local_model_changes_nothing_else(tmp_path):
    """The harness passes model names through untouched."""
    llm = FakeLLM(working_script())
    config = load_config(
        roles_path=write_config(tmp_path, model="local/my-own-gpu"), run_budget_usd=5.0
    )
    adapter = StubAdapter()
    pipeline = build_pipeline(
        task_id="test-task",
        config=config,
        run_dir=tmp_path / "run",
        adapter=adapter,
        llm=llm,
        interactive=False,
    )
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert result.judge_invoked is True
    assert result.spent_usd == 0.0
    assert {call["model"] for call in llm.calls} == {"local/my-own-gpu"}


# --- config echoed into the trace ----------------------------------------


def test_run_start_echoes_the_resolved_config_without_secrets(tmp_path):
    pipeline, adapter = pipeline_for(tmp_path, FakeLLM(working_script()))
    run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    config = pipeline.trace.of_kind("run_start")[0]["config"]
    assert config["roles"]["judge"]["model"] == "fake/model"
    assert config["adapter"] == "stub"
    assert config["prices_verified"] == "2026-10-26"
    assert config["base_url"] == "openrouter.ai"
    # The key itself never appears, only whether one was present.
    assert config["api_key_present"] in (True, False)
    assert "sk-" not in str(config)
    assert "/api/v1" not in str(config), "the full base URL leaked into the trace"


def test_task_accepts_a_directory_path(tmp_path):
    from harness import cybench

    task_dir = tmp_path / "some-cybench-task"
    task_dir.mkdir()
    assert cybench.task_id_of(str(task_dir)) == "some-cybench-task"
    assert cybench.task_id_of("plain-id") == "plain-id"

    task = cybench.LocalDockerAdapter(tasks={}).load(str(task_dir))
    assert task.task_id == "some-cybench-task"
    assert task.workdir == task_dir.resolve()

    spec = {"some-cybench-task": {"container": "web", "port": 1337}}
    task = cybench.LocalDockerAdapter(tasks=spec).load(str(task_dir))
    assert (task.container, task.port, task.workdir) == ("web", 1337, task_dir.resolve())

    assert cybench.StubAdapter().load(str(task_dir)).task_id == "some-cybench-task"


# --- task lifecycle -------------------------------------------------------


class RecordingAdapter(StubAdapter):
    def __init__(self, fail_start=False, fail_stop=False):
        self.events = []
        self.fail_start = fail_start
        self.fail_stop = fail_stop

    def start(self, task):
        self.events.append("start")
        if self.fail_start:
            raise RuntimeError("compose up failed: https://user:pw@registry.example/v2?token=abc")

    def stop(self, task):
        self.events.append("stop")
        if self.fail_stop:
            raise RuntimeError("compose down failed")


def test_the_task_is_started_and_always_stopped(tmp_path):
    pipeline, _ = pipeline_for(tmp_path, FakeLLM(working_script()))
    adapter = RecordingAdapter()
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)
    assert adapter.events == ["start", "stop"]
    assert result.judge_invoked is True


def test_the_task_is_stopped_even_when_a_stage_crashes(tmp_path, monkeypatch):
    import harness.run as run_module

    def exploding(_pipeline):
        raise RuntimeError("boom")

    monkeypatch.setattr(run_module, "STAGE_FUNCS", [("recon", exploding)])
    pipeline, _ = pipeline_for(tmp_path, FakeLLM(working_script()))
    adapter = RecordingAdapter()
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)
    assert adapter.events == ["start", "stop"]
    assert result.failure_origin == "harness_failure"


def test_a_failed_start_is_an_environment_failure_and_nothing_runs(tmp_path):
    llm = FakeLLM(working_script())
    pipeline, _ = pipeline_for(tmp_path, llm)
    adapter = RecordingAdapter(fail_start=True)
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert adapter.events == ["start", "stop"]
    assert llm.calls == []
    assert result.judge_invoked is False
    assert result.verdict == "unconfirmed"
    assert result.failure_origin == "environment_failure"
    detail = pipeline.statuses.read("setup").detail
    assert "pw@" not in detail and "token=abc" not in detail


def test_a_failed_teardown_does_not_replace_the_result(tmp_path):
    pipeline, _ = pipeline_for(tmp_path, FakeLLM(working_script()))
    result = run_pipeline(pipeline, task_id="test-task", adapter=RecordingAdapter(fail_stop=True))
    assert result.judge_invoked is True
    assert any("teardown failed" in e.get("message", "") for e in pipeline.trace.of_kind("note"))


# --- harness statuses through the whole pipeline -------------------------


def test_budget_exceeded_stops_the_run_without_a_judge(tmp_path):
    import yaml

    roles_path = write_config(tmp_path, role_budget=0.0001)
    prices = yaml.safe_load((tmp_path / "prices.yaml").read_text(encoding="utf-8"))
    prices["models"]["fake/model"] = {"input": 100.0, "output": 100.0}
    (tmp_path / "prices.yaml").write_text(yaml.safe_dump(prices), encoding="utf-8")
    config = load_config(roles_path=roles_path, run_budget_usd=5.0)

    llm = FakeLLM(working_script())
    adapter = StubAdapter()
    pipeline = build_pipeline(task_id="test-task", config=config, run_dir=tmp_path / "run",
                              adapter=adapter, llm=llm, interactive=False)
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert result.stages["recon"] == "budget_exceeded"
    assert result.failure_origin == "budget_exceeded"
    assert result.judge_invoked is False
    assert llm.roles_called() == ["recon"]


def test_a_tool_protocol_error_stops_the_run_without_a_judge(tmp_path):
    bad = turn(tool_call("finish", "{not json"))
    script = working_script()
    script["recon"] = [bad, bad]
    pipeline, adapter = pipeline_for(tmp_path, FakeLLM(script))
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert result.stages["recon"] == "tool_protocol_error"
    assert result.failure_origin == "tool_protocol_error"
    assert result.judge_invoked is False


def test_a_judge_crash_is_a_harness_failure_not_judge_incomplete(tmp_path, monkeypatch):
    import harness.run as run_module

    def crash(_pipeline):
        raise RuntimeError("judge blew up")

    monkeypatch.setattr(run_module.judge_role, "run", crash)
    pipeline, adapter = pipeline_for(tmp_path, FakeLLM(working_script()))
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert result.judge_invoked is True
    assert result.stages["judge"] == "harness_failure"
    assert result.failure_origin == "harness_failure"
    assert result.verdict == "unconfirmed"


def test_a_judge_provider_error_keeps_its_classification(tmp_path):
    class JudgeDown(FakeLLM):
        def complete(self, **kwargs):
            if kwargs.get("role") == "judge":
                raise RuntimeError("upstream 503")
            return super().complete(**kwargs)

    pipeline, adapter = pipeline_for(tmp_path, JudgeDown(working_script()))
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert result.stages["judge"] == "environment_failure"
    assert result.failure_origin == "environment_failure"


def test_the_exploit_role_refuses_to_start_without_a_contract(tmp_path):
    from harness.roles import exploit as exploit_role

    llm = FakeLLM(working_script())
    pipeline, _ = pipeline_for(tmp_path, llm)
    status = exploit_role.run(pipeline)

    assert status.outcome is Outcome.BUILD_FAILURE
    assert "did not start" in status.detail
    assert "exploit" not in llm.roles_called()
    rejects = pipeline.trace.of_kind("gate_reject")
    assert rejects and rejects[0]["rule"] == "expectations_contract_missing"


def test_a_target_that_is_not_ready_is_a_harness_failure(tmp_path, monkeypatch):
    from harness.roles import build as build_role

    monkeypatch.setattr(
        build_role, "check_readiness",
        lambda task, trace=None, timeout_s=10: build_role.Readiness(False, "shared_net", "refused"),
    )
    llm = FakeLLM(working_script())
    pipeline, adapter = pipeline_for(tmp_path, llm)
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert result.stages["build"] == "harness_failure"
    assert result.failure_origin == "harness_failure"
    assert "judge" not in llm.roles_called()


# --- refute panel ---------------------------------------------------------


def test_zero_survivors_ends_the_run_before_build(tmp_path):
    script = working_script()
    script["refute"] = [
        turn(tool_call("record_judgement", {"judgement": "refuted", "reason": "no"})),
        finish_turn("refuted"),
    ]
    llm = FakeLLM(script)
    pipeline, adapter = pipeline_for(tmp_path, llm)
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    assert result.ended_early == "no_survivors"
    assert result.judge_invoked is False
    assert result.failure_origin is None
    assert not {"build", "exploit", "judge"} & set(llm.roles_called())


def test_an_infra_failing_panelist_halts_the_panel(tmp_path):
    class PanelDown(FakeLLM):
        def complete(self, **kwargs):
            if kwargs.get("role") == "refute":
                raise RuntimeError("panelist provider down")
            return super().complete(**kwargs)

    llm = PanelDown(working_script())
    pipeline, adapter = pipeline_for(tmp_path, llm)
    result = run_pipeline(pipeline, task_id="test-task", adapter=adapter)

    status = pipeline.statuses.read("refute")
    assert status.outcome is Outcome.ENVIRONMENT_FAILURE
    assert status.artifacts["halted_by"] == "fake/model"
    assert result.failure_origin == "environment_failure"
    assert "build" not in llm.roles_called()


def test_refute_run_with_an_infra_failing_panelist(tmp_path):
    from harness.roles import refute as refute_role

    class PanelDown(FakeLLM):
        def complete(self, **kwargs):
            raise RuntimeError("down")

    pipeline, _ = pipeline_for(tmp_path, PanelDown())
    pipeline.state["candidates"] = [CANDIDATE, {**CANDIDATE, "dedup_key": "def"}]
    status = refute_role.run(pipeline)

    assert status.outcome is Outcome.ENVIRONMENT_FAILURE
    # Halted on the first panelist; the second candidate was never judged.
    assert status.artifacts["judged"] == 0
    assert pipeline.state["survivors"] == []


# --- startup refusals -----------------------------------------------------


def test_a_non_empty_run_dir_is_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "placeholder")
    run_dir = tmp_path / "run"
    (run_dir / "status").mkdir(parents=True)
    (run_dir / "expectations.json").write_text("{}", encoding="utf-8")
    code = main(["--task", "test-task", "--roles", str(write_config(tmp_path)),
                 "--run-dir", str(run_dir), "--non-interactive"])
    assert code == 2
    assert "not empty" in capsys.readouterr().err

    config = load_config(roles_path=write_config(tmp_path))
    with pytest.raises(FileExistsError):
        build_pipeline(task_id="test-task", config=config, run_dir=run_dir,
                       adapter=StubAdapter(), llm=FakeLLM())


def test_a_missing_api_key_fails_at_start(tmp_path, capsys):
    code = main(["--task", "test-task", "--roles", str(write_config(tmp_path)),
                 "--run-dir", str(tmp_path / "run"), "--non-interactive"])
    assert code == 2
    assert "HARNESS_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()


def test_pause_after_judge_is_not_offered():
    from harness.run import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--task", "t", "--pause-after", "judge"])


def test_run_dir_names_use_the_slug(tmp_path, monkeypatch):
    from harness import cybench

    assert cybench.task_id_of("[Easy] Some: Task/../x") == "some-task-x"


def test_previous_tier_feeds_the_ratchet(tmp_path):
    pipeline, adapter = pipeline_for(tmp_path, FakeLLM(working_script()), previous_tier="source")
    run_pipeline(pipeline, task_id="test-task", adapter=adapter)
    gated = pipeline.state["verdict"]
    assert gated["proposed_tier"] == "inconclusive"
    assert gated["proof_tier"] == "source"


def test_the_shipped_refute_panel_shares_no_lineage_with_the_generators():
    """P10, on the config attendees actually get."""
    repo_roles = Path(__file__).resolve().parents[2] / "config" / "roles.yaml"
    config = load_config(roles_path=repo_roles)

    def vendor(model):
        return model.split("/")[-2] if "/" in model else model

    generators = {vendor(config.role(r).model) for r in ("recon", "triage", "build", "exploit")}
    panel = [vendor(m) for m in config.role("refute").panel]
    assert len(set(panel)) == len(panel) >= 3
    assert not generators & set(panel)
    assert vendor(config.role("judge").model) not in generators
