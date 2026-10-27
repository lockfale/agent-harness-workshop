"""Parsing an OpenAI-compatible response: stop reason and usage, every time."""

from __future__ import annotations

import pytest

from harness.llm import (
    LLMClient,
    ProviderError,
    normalise_stop_reason,
    parse_completion,
    parse_usage,
)
from harness.trace import base_url_host

RESPONSE = {
    "model": "vendor/big",
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "shell", "arguments": '{"command": "ls"}'},
                    }
                ],
            },
        }
    ],
    "usage": {
        "prompt_tokens": 1200,
        "completion_tokens": 340,
        "prompt_tokens_details": {"cached_tokens": 900},
        "completion_tokens_details": {"reasoning_tokens": 300},
    },
}


def test_stop_reason_and_usage_are_both_extracted():
    response = parse_completion(RESPONSE, "vendor/big")

    assert response.stop_reason == "tool_use"
    assert response.raw_stop_reason == "tool_calls"
    assert response.usage.input_tokens == 1200
    assert response.usage.output_tokens == 340
    assert response.usage.cache_read_tokens == 900
    assert response.usage.reasoning_tokens == 300
    assert response.wants_tools is True
    assert response.tool_calls[0]["function"]["name"] == "shell"


def test_a_response_with_no_usage_block_reports_zeros():
    response = parse_completion({"choices": [{"finish_reason": "stop", "message": {}}]}, "m")
    assert response.usage.input_tokens == 0
    assert response.stop_reason == "end_turn"
    assert response.wants_tools is False


def test_missing_detail_blocks_do_not_raise():
    usage = parse_usage({"prompt_tokens": 10, "completion_tokens": 5})
    assert usage.cache_read_tokens == 0
    assert usage.reasoning_tokens == 0


def test_alternative_usage_field_names_are_accepted():
    usage = parse_usage({"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 2})
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (7, 3, 2)


def test_sdk_objects_are_normalised():
    class Message:
        content = "hello"
        tool_calls = None

    class Choice:
        finish_reason = "end_turn"
        message = Message()

    class Raw:
        model = "vendor/small"
        choices = [Choice()]
        usage = None

    response = parse_completion(Raw(), "vendor/small")
    assert response.content == "hello"
    assert response.stop_reason == "end_turn"


def test_the_assistant_message_round_trips_tool_calls():
    response = parse_completion(RESPONSE, "vendor/big")
    message = response.as_message()
    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["id"] == "call_1"


def test_model_names_are_passed_through_untouched():
    """Prefix routing is the gateway's job. The client must not rewrite names."""
    sent: list[dict] = []

    class FakeCompletions:
        def create(self, **kwargs):
            sent.append(kwargs)
            return RESPONSE

    class FakeSDK:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    client = LLMClient(client=FakeSDK())
    for name in ("or/vendor/big", "local/my-model", "vendor/big"):
        client.complete(model=name, messages=[{"role": "user", "content": "x"}])

    assert [call["model"] for call in sent] == ["or/vendor/big", "local/my-model", "vendor/big"]


def test_role_and_turn_are_not_sent_to_the_provider():
    sent: list[dict] = []

    class FakeCompletions:
        def create(self, **kwargs):
            sent.append(kwargs)
            return RESPONSE

    class FakeSDK:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    LLMClient(client=FakeSDK()).complete(
        model="vendor/big",
        messages=[],
        max_output_tokens=512,
        reasoning_effort="high",
        role="recon",
        turn=3,
    )
    assert "role" not in sent[0]
    assert "turn" not in sent[0]
    assert sent[0]["max_tokens"] == 512
    assert sent[0]["reasoning_effort"] == "high"


def test_base_url_is_reduced_to_a_host_for_the_trace():
    assert base_url_host("https://openrouter.ai/api/v1") == "openrouter.ai"
    assert base_url_host("http://192.0.2.1:20128/v1?key=abc") == "192.0.2.1"
    assert base_url_host(None) is None


# --- stop reasons ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,normal",
    [("tool_calls", "tool_use"), ("stop", "end_turn"), ("length", "max_tokens"),
     ("content_filter", "content_filter"), ("tool_use", "tool_use"),
     ("something_new", "something_new"), (None, None)],
)
def test_openai_finish_reasons_are_normalised(raw, normal):
    assert normalise_stop_reason(raw) == normal


def test_the_trace_keeps_the_raw_stop_reason(tmp_path, config, trace, stub_task):
    from harness.budget import Budget
    from harness.gates.scope import ScopeGate
    from harness.roles.base import Pipeline, run_role
    from harness.sidechannel import SideChannels
    from harness.status import StatusStore
    from harness.tools.finish import register_finish
    from harness.tools.registry import ToolRegistry

    from .conftest import FakeLLM, finish_turn

    turn_ = finish_turn()
    turn_.raw_stop_reason = "tool_calls"
    pipeline = Pipeline(config=config, llm=FakeLLM(default=turn_), trace=trace,
                        budget=Budget(prices=config.prices, trace=trace),
                        statuses=StatusStore(tmp_path), run_dir=tmp_path, task=stub_task,
                        scope=ScopeGate.deny_all(), side=SideChannels(trace=trace))
    registry = ToolRegistry(role="recon")
    register_finish(registry)
    run_role(role="recon", pipeline=pipeline, registry=registry, messages=[])
    event = trace.of_kind("model_response")[0]
    assert (event["stop_reason"], event["raw_stop_reason"]) == ("tool_use", "tool_calls")


# --- error bodies ---------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "upstream overloaded", "code": 502}},
        {"error": "rate limited", "choices": []},
        {"choices": []},
        {},
    ],
)
def test_a_200_with_no_completion_is_a_provider_error(body):
    with pytest.raises(ProviderError):
        parse_completion(body, "m")


def test_a_provider_error_ends_the_role_as_an_environment_failure(tmp_path, config, trace, stub_task):
    from harness.budget import Budget
    from harness.gates.scope import ScopeGate
    from harness.roles.base import Pipeline, run_role
    from harness.sidechannel import SideChannels
    from harness.status import Outcome, StatusStore
    from harness.tools.finish import register_finish
    from harness.tools.registry import ToolRegistry

    class ErrorBody:
        def __init__(self):
            self.calls = 0

        def complete(self, **_):
            self.calls += 1
            return parse_completion({"error": {"message": "upstream overloaded"}}, "m")

    llm = ErrorBody()
    pipeline = Pipeline(config=config, llm=llm, trace=trace,
                        budget=Budget(prices=config.prices, trace=trace),
                        statuses=StatusStore(tmp_path), run_dir=tmp_path, task=stub_task,
                        scope=ScopeGate.deny_all(), side=SideChannels(trace=trace))
    registry = ToolRegistry(role="recon")
    register_finish(registry)
    outcome = run_role(role="recon", pipeline=pipeline, registry=registry, messages=[])

    assert outcome.outcome is Outcome.ENVIRONMENT_FAILURE
    assert llm.calls == 1, "an error body must not be nudged and billed again"
    assert trace.of_kind("model_response") == []


# --- reasoning round trip -------------------------------------------------


def reasoning_response():
    body = {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "reasoning": "summary",
                "reasoning_details": [{"type": "reasoning.text", "text": "t", "signature": "s"}],
                "tool_calls": RESPONSE["choices"][0]["message"]["tool_calls"],
            },
        }],
    }
    return parse_completion(body, "m")


def test_reasoning_details_are_echoed_back_unmodified():
    message = reasoning_response().as_message()
    assert message["reasoning_details"] == [
        {"type": "reasoning.text", "text": "t", "signature": "s"}
    ]
    assert "reasoning" not in message


def test_reasoning_echo_can_be_turned_off():
    message = reasoning_response().as_message(echo_reasoning=False)
    assert "reasoning_details" not in message and "reasoning" not in message


def test_echo_reasoning_is_configurable_per_role(tmp_path):
    import yaml

    from harness.config import load_config

    from .conftest import write_config

    roles_path = write_config(tmp_path)
    roles = yaml.safe_load(roles_path.read_text(encoding="utf-8"))
    roles["roles"]["judge"]["echo_reasoning"] = False
    roles_path.write_text(yaml.safe_dump(roles), encoding="utf-8")
    config = load_config(roles_path=roles_path)
    assert config.role("judge").echo_reasoning is False
    assert config.role("recon").echo_reasoning is True
