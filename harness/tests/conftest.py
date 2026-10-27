"""Test fixtures. Everything here runs offline, with no API key and no Docker.

The fake client implements the same `complete()` signature as `llm.LLMClient`,
which is the whole reason that signature takes `role` and `turn`: a scripted
client can answer differently per role without the harness knowing it is
scripted.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from harness.config import load_config
from harness.cybench import CybenchTask, StubAdapter
from harness.llm import LLMResponse
from harness.pricing import PriceTable, Usage
from harness.trace import Trace

# --- scripted model turns ------------------------------------------------


def tool_call(name: str, arguments: Any, call_id: str | None = None) -> dict[str, Any]:
    """One tool call, with arguments serialised the way a provider sends them."""
    return {
        "id": call_id or f"call_{name}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


def turn(
    *calls: dict[str, Any],
    text: str | None = None,
    stop_reason: str | None = None,
    usage: Usage | None = None,
    model: str = "fake/model",
) -> LLMResponse:
    """One assistant turn."""
    return LLMResponse(
        model=model,
        content=text,
        tool_calls=list(calls),
        stop_reason=stop_reason or ("tool_use" if calls else "end_turn"),
        usage=usage or Usage(input_tokens=100, output_tokens=50),
    )


def finish_turn(summary: str = "done", outcome: str = "success", **extra: Any) -> LLMResponse:
    return turn(tool_call("finish", {"summary": summary, "outcome": outcome, **extra}))


@dataclasses.dataclass
class FakeLLM:
    """A client that replays scripted turns, keyed by role.

    A role with no script, or one whose script runs out, gets `default`, which
    calls `finish`. That keeps the end-to-end tests about the stage the test is
    actually interested in.
    """

    scripts: dict[str, list[LLMResponse]] = dataclasses.field(default_factory=dict)
    default: LLMResponse | None = None
    calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def __post_init__(self) -> None:
        self.scripts = {role: list(turns) for role, turns in self.scripts.items()}

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        role: str | None = None,
        turn: int | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "role": role,
                "turn": turn,
                "model": model,
                "tools": [t["function"]["name"] for t in (tools or [])],
                "messages": len(messages),
            }
        )
        script = self.scripts.get(role or "")
        if script:
            return script.pop(0)
        return self.default or finish_turn()

    def roles_called(self) -> list[str]:
        return [call["role"] for call in self.calls]


class BoomLLM:
    """A client that raises. Used to prove a provider error is not a verdict."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc or RuntimeError("upstream is down")

    def complete(self, **_: Any) -> LLMResponse:
        raise self.exc


# --- fixtures -------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """The suite never reads the attendee's real gateway or key. Someone with
    HARNESS_BASE_URL pointed at a local gateway should see the same results as
    someone with nothing set."""
    monkeypatch.delenv("HARNESS_BASE_URL", raising=False)
    monkeypatch.delenv("HARNESS_API_KEY", raising=False)


@pytest.fixture
def prices() -> PriceTable:
    return PriceTable(
        models={
            "vendor/big": {"input": 10.0, "output": 30.0, "cache_read": 1.0},
            "vendor/small": {"input": 1.0, "output": 2.0},
            "fake/model": {"input": 0.0, "output": 0.0},
        },
        gateway_prefixes=("or", "gw"),
        local_prefixes=("local", "spark"),
        source="test-prices.yaml",
    )


@pytest.fixture
def trace() -> Trace:
    return Trace(path=None)


@pytest.fixture
def stub_task() -> CybenchTask:
    return StubAdapter().load("test-task")


def write_config(
    tmp_path: Path,
    model: str = "fake/model",
    extra_models: dict[str, Any] | None = None,
    role_budget: float | None = None,
    price_it: bool = True,
) -> Path:
    """Write a minimal roles.yaml and prices.yaml, and return the roles path.

    `price_it=False` leaves the model out of the price table, which is how the
    fail-fast tests get an unpriced model.
    """
    roles = {
        "defaults": {"max_output_tokens": 256, "max_turns": 6},
        "roles": {
            name: {"model": model, **({"budget_usd": role_budget} if role_budget else {})}
            for name in ("recon", "triage", "refute", "build", "exploit", "judge")
        },
    }
    models: dict[str, Any] = {
        **({model: {"input": 0.0, "output": 0.0}} if price_it else {}),
        **(extra_models or {}),
    }
    prices = {
        "prices_verified": "2026-10-26",
        "gateway_prefixes": ["or"],
        "local_prefixes": ["local", "spark"],
        "models": models,
    }
    import yaml

    roles_path = tmp_path / "roles.yaml"
    roles_path.write_text(yaml.safe_dump(roles), encoding="utf-8")
    (tmp_path / "prices.yaml").write_text(yaml.safe_dump(prices), encoding="utf-8")
    return roles_path


@pytest.fixture
def config(tmp_path: Path):
    return load_config(roles_path=write_config(tmp_path), run_budget_usd=5.0)
