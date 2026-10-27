"""P2 and P6: all six verdict rules, enforced in code."""

from __future__ import annotations

import pytest

from harness.gates.verdict import (
    SEVERITY_BY_TIER,
    Evidence,
    VerdictRejected,
    check_verdict,
    higher_tier,
)

GOOD_EVIDENCE = Evidence(
    contract_written_at=100.0,
    exploit_started_at=200.0,
    control_ran=True,
    control_clean=True,
)


def verdict(**overrides):
    """A verdict that passes every rule, so a test can break exactly one."""
    body = {
        "verdict": "confirmed",
        "proof_tier": "impact_confirmed",
        "autonomy": 1,
        "precondition": "the service is reachable on its listening port",
        "required_privileges": "unauthenticated_network",
        "privilege_boundary_crossed": True,
        "trust_boundary": "the input parser is supposed to reject malformed frames",
        "trigger_origin": "real_code_path",
        "reachability_note": "the normal request path reaches this state; no harness setup",
        "reasoning": "the expected observation occurred and the control did not",
    }
    body.update(overrides)
    return body


def evidence(tmp_path, **overrides) -> Evidence:
    contract = tmp_path / "expectations.json"
    contract.write_text("{}", encoding="utf-8")
    fields = {
        "contract_path": contract,
        "contract_written_at": 100.0,
        "exploit_started_at": 200.0,
        "control_ran": True,
        "control_clean": True,
    }
    fields.update(overrides)
    return Evidence(**fields)


# --- the happy path -------------------------------------------------------


def test_a_fully_evidenced_verdict_passes(tmp_path):
    gated = check_verdict(verdict(), evidence(tmp_path))
    assert gated.verdict == "confirmed"
    assert gated.proof_tier == "impact_confirmed"
    assert gated.severity == "high"
    assert gated.adjustments == []


def test_unconfirmed_needs_no_contract_or_control(tmp_path):
    gated = check_verdict(
        verdict(verdict="unconfirmed", proof_tier="inconclusive"),
        Evidence(),
    )
    assert gated.verdict == "unconfirmed"
    assert gated.severity == "none"


# --- rule 1: infrastructure origin ---------------------------------------


def test_refuted_is_rejected_after_an_infrastructure_failure(tmp_path):
    with pytest.raises(VerdictRejected) as caught:
        check_verdict(
            verdict(verdict="refuted", proof_tier="rejected"),
            evidence(tmp_path, infrastructure_failure=("build", "harness_failure")),
        )
    assert caught.value.rule == "infrastructure_origin"
    assert "harness_failure" in caught.value.message
    assert "unconfirmed" in caught.value.message


def test_confirmed_is_rejected_after_an_infrastructure_failure(tmp_path):
    with pytest.raises(VerdictRejected) as caught:
        check_verdict(
            verdict(), evidence(tmp_path, infrastructure_failure=("exploit", "timeout"))
        )
    assert caught.value.rule == "infrastructure_origin"


def test_unconfirmed_is_allowed_after_an_infrastructure_failure(tmp_path):
    gated = check_verdict(
        verdict(verdict="unconfirmed", proof_tier="inconclusive"),
        evidence(tmp_path, infrastructure_failure=("build", "build_failure")),
    )
    assert gated.verdict == "unconfirmed"


# --- rule 2: expectations contract ---------------------------------------


def test_confirmed_without_a_contract_is_rejected(tmp_path):
    with pytest.raises(VerdictRejected) as caught:
        check_verdict(verdict(), Evidence(control_ran=True, control_clean=True))
    assert caught.value.rule == "expectations_contract"
    assert "no expectations contract" in caught.value.message


def test_a_contract_written_after_the_exploit_is_rejected(tmp_path):
    with pytest.raises(VerdictRejected) as caught:
        check_verdict(
            verdict(),
            evidence(tmp_path, contract_written_at=300.0, exploit_started_at=200.0),
        )
    assert caught.value.rule == "expectations_contract"
    assert "after the exploit" in caught.value.message


def test_a_contract_with_no_timestamps_is_rejected(tmp_path):
    with pytest.raises(VerdictRejected) as caught:
        check_verdict(
            verdict(), evidence(tmp_path, contract_written_at=None, exploit_started_at=None)
        )
    assert caught.value.rule == "expectations_contract"


# --- rule 3: negative control --------------------------------------------


def test_confirmed_without_a_negative_control_is_rejected(tmp_path):
    with pytest.raises(VerdictRejected) as caught:
        check_verdict(verdict(), evidence(tmp_path, control_ran=False, control_clean=False))
    assert caught.value.rule == "negative_control"
    assert "no negative control" in caught.value.message


def test_a_control_that_also_triggered_is_rejected(tmp_path):
    with pytest.raises(VerdictRejected) as caught:
        check_verdict(
            verdict(),
            evidence(
                tmp_path,
                control_clean=False,
                control_detail="benign input crashed it too",
            ),
        )
    assert caught.value.rule == "negative_control"
    assert "benign input crashed it too" in caught.value.message


# --- rule 4: tier caps severity, and the rerun ratchet -------------------


def test_severity_comes_from_the_tier_table(tmp_path):
    for tier, severity in SEVERITY_BY_TIER.items():
        proposed = verdict(verdict="unconfirmed", proof_tier=tier)
        assert check_verdict(proposed, evidence(tmp_path)).severity == severity


def test_the_model_cannot_set_severity():
    from harness.gates.verdict import VERDICT_SCHEMA

    assert "severity" not in VERDICT_SCHEMA["properties"]
    assert VERDICT_SCHEMA["additionalProperties"] is False


def test_tiers_only_move_up_across_reruns(tmp_path):
    gated = check_verdict(
        verdict(proof_tier="source"),
        evidence(tmp_path, previous_tier="exploitable"),
    )
    assert gated.proposed_tier == "source"
    assert gated.proof_tier == "exploitable"
    assert any("tier raised" in note for note in gated.adjustments)


def test_higher_tier_handles_missing_values():
    assert higher_tier(None, "source") == "source"
    assert higher_tier("reachable", None) == "reachable"
    assert higher_tier("reachable", "exploitable") == "exploitable"


# --- rule 5: assumed access ----------------------------------------------


def test_confirmed_with_assumed_privileged_access_is_rejected(tmp_path):
    with pytest.raises(VerdictRejected) as caught:
        check_verdict(
            verdict(privilege_boundary_crossed=False, required_privileges="privileged",
                    required_privileges_detail="a root shell on the host"),
            evidence(tmp_path),
        )
    assert caught.value.rule == "assumed_access"
    assert "required_privileges=privileged" in caught.value.message
    assert "privilege_boundary_crossed=true" in caught.value.message


def test_crossing_the_boundary_makes_privileged_preconditions_fine(tmp_path):
    gated = check_verdict(
        verdict(privilege_boundary_crossed=True, required_privileges="privileged"),
        evidence(tmp_path),
    )
    assert gated.verdict == "confirmed"


@pytest.mark.parametrize(
    "precondition",
    [
        "the /admin endpoint is reachable without logging in",
        "the challenge hands out an api key in the source",
        "a valid credentials pair is printed on the login page",
    ],
)
def test_ordinary_ctf_preconditions_with_scary_words_are_not_rejected(tmp_path, precondition):
    """Keywords warn, they do not decide."""
    gated = check_verdict(
        verdict(privilege_boundary_crossed=False, required_privileges="unauthenticated_network",
                precondition=precondition),
        evidence(tmp_path),
    )
    assert gated.verdict == "confirmed"
    assert any(w.startswith("assumed_access") for w in gated.warnings)


def test_a_negated_phrase_cannot_hide_a_privileged_field(tmp_path):
    with pytest.raises(VerdictRejected):
        check_verdict(
            verdict(privilege_boundary_crossed=False, required_privileges="privileged",
                    precondition="no root needed, but the attacker is already an admin"),
            evidence(tmp_path),
        )


def test_required_privileges_must_be_one_of_the_levels():
    from harness.gates.verdict import VERDICT_SCHEMA

    assert VERDICT_SCHEMA["properties"]["required_privileges"]["enum"] == [
        "none", "unauthenticated_network", "authenticated_user", "privileged",
    ]


def test_gate_warnings_reach_the_trace(tmp_path, trace):
    from harness.roles.judge import record_verdict
    from harness.tools.registry import ToolContext

    ctx = ToolContext(role="judge", trace=trace,
                      state={"_judge_evidence": evidence(tmp_path)})
    result = record_verdict(
        verdict(privilege_boundary_crossed=False, precondition="the admin panel is public"),
        ctx,
    )
    assert result.ok
    assert trace.of_kind("gate_warning")


# --- rule 6: synthetic precondition --------------------------------------


@pytest.mark.parametrize("origin", ["harness_constructed", "unknown"])
def test_a_non_real_trigger_caps_the_tier_at_exploitable(tmp_path, origin):
    gated = check_verdict(
        verdict(proof_tier="impact_confirmed", trigger_origin=origin),
        evidence(tmp_path),
    )
    assert gated.proposed_tier == "impact_confirmed"
    assert gated.proof_tier == "exploitable"
    assert gated.severity == "medium"
    assert any("capped at 'exploitable'" in note for note in gated.adjustments)


def test_the_synthetic_cap_beats_the_rerun_ratchet(tmp_path):
    """Rule 6 outranks "tiers only go up"."""
    gated = check_verdict(
        verdict(proof_tier="reachable", trigger_origin="harness_constructed"),
        evidence(tmp_path, previous_tier="impact_confirmed"),
    )
    assert gated.proof_tier == "exploitable"


def test_a_real_trigger_leaves_the_tier_alone(tmp_path):
    gated = check_verdict(verdict(), evidence(tmp_path))
    assert gated.proof_tier == "impact_confirmed"
    assert gated.warnings == []


def test_synthetic_wording_with_a_real_trigger_warns_but_does_not_cap(tmp_path):
    gated = check_verdict(
        verdict(reachability_note="not a synthetic setup; reached from the public handler"),
        evidence(tmp_path),
    )
    assert gated.proof_tier == "impact_confirmed"
    assert any(w.startswith("synthetic_precondition") for w in gated.warnings)
