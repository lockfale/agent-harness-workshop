"""The shared turn loop: finish, silence, malformed JSON, provider errors."""

from __future__ import annotations

from pathlib import Path

from harness.budget import Budget
from harness.gates.scope import ScopeGate
from harness.roles.base import Pipeline, run_role
from harness.sidechannel import SideChannels
from harness.status import INFRASTRUCTURE_OUTCOMES, Outcome, StatusStore
from harness.tools.finish import register_finish
from harness.tools.registry import ToolRegistry, ToolResult

from .conftest import BoomLLM, FakeLLM, finish_turn, tool_call, turn


def make_pipeline(tmp_path: Path, config, llm, trace, stub_task) -> Pipeline:
    return Pipeline(
        config=config,
        llm=llm,
        trace=trace,
        budget=Budget(prices=config.prices, trace=trace),
        statuses=StatusStore(tmp_path),
        run_dir=tmp_path,
        task=stub_task,
        scope=ScopeGate.deny_all(trace=trace),
        side=SideChannels(trace=trace),
        interactive=False,
    )


def registry_with(trace, handler=None):
    registry = ToolRegistry(role="recon", trace=trace)
    registry.register(
        "note",
        "write something down",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        handler or (lambda a, c: ToolResult(content={"noted": a["text"]})),
    )
    register_finish(registry)
    return registry


def test_a_role_that_calls_finish_completes(tmp_path, config, trace, stub_task):
    llm = FakeLLM({"recon": [turn(tool_call("note", {"text": "hi"})), finish_turn("all done")]})
    pipeline = make_pipeline(tmp_path, config, llm, trace, stub_task)

    outcome = run_role(
        role="recon",
        pipeline=pipeline,
        registry=registry_with(trace),
        messages=[{"role": "user", "content": "go"}],
    )

    assert outcome.outcome is Outcome.COMPLETED
    assert outcome.finish["summary"] == "all done"
    assert outcome.tool_calls == 2
    assert outcome.turns == 2
    assert trace.of_kind("role_end")[0]["status"] == "completed"


def test_finish_with_outcome_blocked_is_not_a_clean_stage(tmp_path, config, trace, stub_task):
    llm = FakeLLM({"recon": [finish_turn("the container is not up", outcome="blocked")]})
    pipeline = make_pipeline(tmp_path, config, llm, trace, stub_task)

    outcome = run_role(
        role="recon",
        pipeline=pipeline,
        registry=registry_with(trace),
        messages=[],
    )
    assert outcome.outcome is Outcome.INCOMPLETE


def test_a_role_that_goes_quiet_is_incomplete_not_successful(tmp_path, config, trace, stub_task):
    """One nudge, then it counts as "we do not know what it concluded"."""
    llm = FakeLLM(
        {"recon": [turn(text="I think I am done.", stop_reason="end_turn")]},
        default=turn(text="Still done.", stop_reason="end_turn"),
    )
    pipeline = make_pipeline(tmp_path, config, llm, trace, stub_task)

    outcome = run_role(
        role="recon", pipeline=pipeline, registry=registry_with(trace), messages=[]
    )

    assert outcome.outcome is Outcome.INCOMPLETE
    assert "without calling `finish`" in (outcome.detail or "")
    assert outcome.last_stop_reason == "end_turn"


def test_one_malformed_tool_call_gets_a_structured_error_and_a_retry(
    tmp_path, config, trace, stub_task
):
    llm = FakeLLM(
        {
            "recon": [
                turn(tool_call("note", "{not json at all")),
                finish_turn("recovered"),
            ]
        }
    )
    pipeline = make_pipeline(tmp_path, config, llm, trace, stub_task)

    outcome = run_role(
        role="recon", pipeline=pipeline, registry=registry_with(trace), messages=[]
    )

    assert outcome.outcome is Outcome.COMPLETED
    rejects = trace.of_kind("gate_reject")
    assert rejects[0]["rule"] == "malformed_tool_json"
    assert rejects[0]["attempt"] == 1


def test_two_malformed_tool_calls_end_the_role_as_a_protocol_error(
    tmp_path, config, trace, stub_task
):
    llm = FakeLLM(
        {
            "recon": [
                turn(tool_call("note", "{nope")),
                turn(tool_call("note", "also nope}")),
            ]
        }
    )
    pipeline = make_pipeline(tmp_path, config, llm, trace, stub_task)

    outcome = run_role(
        role="recon", pipeline=pipeline, registry=registry_with(trace), messages=[]
    )

    assert outcome.outcome is Outcome.TOOL_PROTOCOL_ERROR
    assert "twice" in (outcome.detail or "")
    assert len(trace.of_kind("gate_reject")) == 2


def test_a_provider_error_is_an_environment_failure_not_a_verdict(
    tmp_path, config, trace, stub_task
):
    pipeline = make_pipeline(tmp_path, config, BoomLLM(), trace, stub_task)

    outcome = run_role(
        role="recon", pipeline=pipeline, registry=registry_with(trace), messages=[]
    )

    assert outcome.outcome is Outcome.ENVIRONMENT_FAILURE
    assert outcome.outcome in INFRASTRUCTURE_OUTCOMES
    assert "upstream is down" in (outcome.detail or "")


def test_the_turn_limit_ends_the_role_as_incomplete(tmp_path, config, trace, stub_task):
    llm = FakeLLM(default=turn(tool_call("note", {"text": "again"})))
    pipeline = make_pipeline(tmp_path, config, llm, trace, stub_task)

    outcome = run_role(
        role="recon",
        pipeline=pipeline,
        registry=registry_with(trace),
        messages=[],
        max_turns=3,
    )

    assert outcome.outcome is Outcome.INCOMPLETE
    assert "turn limit of 3" in (outcome.detail or "")
    assert outcome.turns == 3


def test_every_turn_records_a_stop_reason_and_usage(tmp_path, config, trace, stub_task):
    llm = FakeLLM({"recon": [turn(tool_call("note", {"text": "x"})), finish_turn()]})
    pipeline = make_pipeline(tmp_path, config, llm, trace, stub_task)

    run_role(role="recon", pipeline=pipeline, registry=registry_with(trace), messages=[])

    responses = trace.of_kind("model_response")
    assert len(responses) == 2
    for event in responses:
        assert event["stop_reason"]
        assert event["usage"]["input_tokens"] == 100
        assert "cumulative_usd" in event


def test_an_invalid_tool_call_never_reaches_the_handler_mid_loop(
    tmp_path, config, trace, stub_task
):
    executed: list[dict] = []

    def handler(arguments, ctx):
        executed.append(arguments)
        return ToolResult(content="ok")

    llm = FakeLLM(
        {"recon": [turn(tool_call("note", {"txt": "typo"})), finish_turn("gave up")]}
    )
    pipeline = make_pipeline(tmp_path, config, llm, trace, stub_task)

    outcome = run_role(
        role="recon",
        pipeline=pipeline,
        registry=registry_with(trace, handler),
        messages=[],
    )

    assert outcome.outcome is Outcome.COMPLETED
    assert executed == []
    assert trace.of_kind("gate_reject")[0]["rule"] == "schema_validation"
    assert trace.of_kind("tool_result")[0]["ok"] is False
