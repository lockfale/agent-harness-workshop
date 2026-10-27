"""Caps, and what gets recorded when one is breached."""

from __future__ import annotations

import pytest

from harness.budget import Budget, BudgetExceeded
from harness.pricing import Usage


def test_charges_accumulate_per_role_and_per_run(prices, trace):
    budget = Budget(prices=prices, trace=trace)
    budget.charge(role="recon", model="vendor/small", usage=Usage(input_tokens=1_000_000))
    budget.charge(role="judge", model="vendor/small", usage=Usage(output_tokens=1_000_000))

    assert budget.role_spent("recon") == pytest.approx(1.0)
    assert budget.role_spent("judge") == pytest.approx(2.0)
    assert budget.spent_usd == pytest.approx(3.0)


def test_a_role_cap_is_enforced_and_records_the_last_stop_reason(prices, trace):
    budget = Budget(prices=prices, role_caps_usd={"recon": 0.5}, trace=trace)
    budget.charge(
        role="recon",
        model="vendor/small",
        usage=Usage(input_tokens=1_000_000),
        stop_reason="tool_use",
    )

    with pytest.raises(BudgetExceeded) as caught:
        budget.enforce("recon")

    assert caught.value.scope == "role:recon"
    assert caught.value.last_stop_reason == "tool_use"
    assert "tool_use" in str(caught.value)

    breach = [e for e in trace.of_kind("budget") if e["breached"]]
    assert len(breach) == 1
    assert breach[0]["scope"] == "role:recon"
    assert breach[0]["last_stop_reason"] == "tool_use"
    assert breach[0]["cap_usd"] == 0.5


def test_a_run_cap_is_enforced_across_roles(prices, trace):
    budget = Budget(prices=prices, run_cap_usd=1.5, trace=trace)
    budget.charge(role="recon", model="vendor/small", usage=Usage(input_tokens=1_000_000))
    budget.enforce("recon")
    budget.charge(role="build", model="vendor/small", usage=Usage(input_tokens=1_000_000))

    with pytest.raises(BudgetExceeded) as caught:
        budget.enforce("build")
    assert caught.value.scope == "run"


def test_charging_does_not_raise_so_the_breach_can_be_traced_first(prices, trace):
    """The response that broke the cap still gets a cost in the trace."""
    budget = Budget(prices=prices, run_cap_usd=0.1, trace=trace)
    charge = budget.charge(
        role="recon", model="vendor/big", usage=Usage(input_tokens=1_000_000)
    )
    assert charge.cost_usd == pytest.approx(10.0)
    with pytest.raises(BudgetExceeded):
        budget.enforce("recon")


def test_local_models_never_breach_a_cap(prices, trace):
    budget = Budget(prices=prices, run_cap_usd=0.01, role_caps_usd={"recon": 0.01}, trace=trace)
    for _ in range(50):
        budget.charge(
            role="recon",
            model="local/whatever",
            usage=Usage(input_tokens=1_000_000, output_tokens=1_000_000),
        )
        budget.enforce("recon")
    assert budget.spent_usd == 0.0


def test_snapshot_records_a_non_breaching_budget_event(prices, trace):
    budget = Budget(prices=prices, run_cap_usd=5.0, trace=trace)
    budget.charge(role="recon", model="vendor/small", usage=Usage(input_tokens=1000))
    budget.snapshot("run")

    events = trace.of_kind("budget")
    assert events[-1]["breached"] is False
    assert events[-1]["cap_usd"] == 5.0


def test_remaining_tracks_the_run_cap(prices):
    budget = Budget(prices=prices, run_cap_usd=2.0)
    assert budget.remaining() == pytest.approx(2.0)
    budget.charge(role="recon", model="vendor/small", usage=Usage(input_tokens=1_000_000))
    assert budget.remaining() == pytest.approx(1.0)
    assert Budget(prices=prices).remaining() is None
