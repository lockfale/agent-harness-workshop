"""The parts of the roles that are code, not prompts.

Dedup, panel aggregation, readiness, and the judge's gated tool. These are the
pieces attendees do not have to write, so they are the pieces that need tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.cybench import CybenchTask
from harness.gates.verdict import Evidence
from harness.roles import refute as refute_role
from harness.roles import triage as triage_role
from harness.roles.build import Readiness, check_readiness, contract_path, write_contract
from harness.roles.judge import record_verdict
from harness.tools.registry import ToolContext

from .test_run import CONTRACT, VERDICT

# --- triage: deterministic dedup, before the model -----------------------


def observation(**kwargs):
    base = {
        "what": "the frame length is read before it is validated",
        "where": "src/parse.c:118",
        "evidence": "length = read_u32(buf);",
        "kind": "file",
    }
    base.update(kwargs)
    return base


def test_identical_observations_collapse_and_keep_a_count():
    rows, counts = triage_role.dedup([observation(), observation(), observation()])
    assert len(rows) == 1
    assert rows[0]["seen"] == 3
    assert sum(counts.values()) == 3


def test_wording_differences_collapse_but_locations_do_not():
    rows, _ = triage_role.dedup(
        [
            observation(what="The frame length is read BEFORE it is validated."),
            observation(),
            observation(where="src/parse.c:240"),
        ]
    )
    assert len(rows) == 2


def test_dedup_is_deterministic_across_calls():
    first, _ = triage_role.dedup([observation(), observation(where="a:1")])
    second, _ = triage_role.dedup([observation(where="a:1"), observation()])
    assert {row["dedup_key"] for row in first} == {row["dedup_key"] for row in second}


def test_dedup_keeps_the_original_order_of_first_sighting():
    rows, _ = triage_role.dedup(
        [observation(where="a:1"), observation(where="b:2"), observation(where="a:1")]
    )
    assert [row["where"] for row in rows] == ["a:1", "b:2"]


def test_an_empty_observation_list_is_fine():
    assert triage_role.dedup([]) == ([], {})


# --- refute: majority refutes, uncertain counts as refuted ---------------


def votes(*judgements):
    return [{"judgement": j, "reason": "because"} for j in judgements]


@pytest.mark.parametrize(
    "judgements,killed",
    [
        (("refuted", "refuted", "stands"), True),
        (("refuted", "stands", "stands"), False),
        (("uncertain", "uncertain", "stands"), True),
        (("stands", "stands", "stands"), False),
        (("refuted", "stands"), False),      # a tie is not a majority
        (("refuted", "uncertain"), True),
        (("stands",), False),
        (("uncertain",), True),
    ],
)
def test_panel_aggregation(judgements, killed):
    assert refute_role.aggregate(votes(*judgements))[0] is killed


def test_uncertain_is_counted_with_the_refutations():
    killed, refuted, stands = refute_role.aggregate(votes("uncertain", "stands", "stands"))
    assert (refuted, stands) == (1, 2)
    assert killed is False


def test_an_empty_panel_kills_the_candidate():
    """No evidence that it stands is not evidence that it stands."""
    assert refute_role.aggregate([])[0] is True


# --- build: readiness, and how strong the evidence is --------------------


def test_readiness_falls_back_to_the_host_check_and_says_so(trace, monkeypatch):
    task = CybenchTask(task_id="t", container=None, host="127.0.0.1", port=65000)
    monkeypatch.setattr("harness.roles.build.host_port_open", lambda h, p, timeout=2.0: True)

    result = check_readiness(task, trace)

    assert result.ready is True
    assert result.method == "host_only"
    assert result.weak is True
    assert "does not prove" in result.detail
    assert trace.of_kind("readiness")[0]["readiness"] == "host_only"


def test_readiness_prefers_the_in_container_probe(trace, monkeypatch):
    task = CybenchTask(task_id="t", container="t-web", probe_command="nc -z localhost 1337")
    monkeypatch.setattr(
        "harness.roles.build.docker_exec", lambda c, cmd, t=10: (0, "open", "")
    )

    result = check_readiness(task, trace)

    assert result.method == "in_container"
    assert result.ready is True
    assert result.weak is False


def test_a_failing_in_container_probe_is_not_ready_even_if_the_port_answers(
    trace, monkeypatch
):
    task = CybenchTask(
        task_id="t", container="t-web", probe_command="nc -z localhost 1337", port=1337
    )
    monkeypatch.setattr(
        "harness.roles.build.docker_exec", lambda c, cmd, t=10: (1, "", "refused")
    )
    monkeypatch.setattr("harness.roles.build.host_port_open", lambda h, p, timeout=2.0: True)

    result = check_readiness(task, trace)

    assert result.method == "in_container"
    assert result.ready is False


def test_no_probe_and_no_port_means_readiness_is_unknown(trace):
    result = check_readiness(CybenchTask(task_id="t"), trace)
    assert result.method == "none"
    assert result.ready is False
    assert Readiness(ready=True, method="none").weak is True


# --- build: the contract writer -------------------------------------------


def test_the_contract_is_written_with_a_timestamp(tmp_path: Path, trace):
    ctx = ToolContext(role="build", run_dir=tmp_path, trace=trace)
    result = write_contract(dict(CONTRACT), ctx)

    path = contract_path(tmp_path)
    assert path.exists()
    assert result.ok is True
    assert ctx.state["contract_written_at"] > 0
    assert ctx.state["contract"]["falsifier"] == CONTRACT["falsifier"]


# --- judge: the gated tool -----------------------------------------------


def judge_ctx(tmp_path, **evidence_kwargs):
    contract = tmp_path / "expectations.json"
    contract.write_text("{}", encoding="utf-8")
    fields = {
        "contract_path": contract,
        "contract_written_at": 100.0,
        "exploit_started_at": 200.0,
        "control_ran": True,
        "control_clean": True,
    }
    fields.update(evidence_kwargs)
    ctx = ToolContext(role="judge")
    ctx.state["_judge_evidence"] = Evidence(**fields)
    return ctx


def test_the_judge_tool_accepts_a_well_evidenced_verdict(tmp_path):
    ctx = judge_ctx(tmp_path)
    result = record_verdict({**VERDICT, "verdict": "confirmed",
                             "proof_tier": "exploitable",
                             "privilege_boundary_crossed": True}, ctx)

    assert result.ok is True
    assert ctx.state["verdict"]["severity"] == "medium"


def test_the_judge_tool_returns_the_rule_name_on_a_rejection(tmp_path, trace):
    ctx = judge_ctx(tmp_path, control_ran=False, control_clean=False)
    ctx.trace = trace
    result = record_verdict({**VERDICT, "verdict": "confirmed",
                             "privilege_boundary_crossed": True}, ctx)

    assert result.ok is False
    assert result.content["rule"] == "negative_control"
    assert "verdict" not in ctx.state
    reject = trace.of_kind("gate_reject")[0]
    assert reject["gate"] == "verdict"
    assert reject["rule"] == "negative_control"


def test_repeated_rejections_eventually_tell_the_model_to_stop_arguing(tmp_path):
    ctx = judge_ctx(tmp_path, control_ran=False, control_clean=False)
    proposed = {**VERDICT, "verdict": "confirmed", "privilege_boundary_crossed": True}

    results = [record_verdict(dict(proposed), ctx) for _ in range(4)]

    assert all(result.ok is False for result in results)
    assert "will not change" in (results[-1].error or "")
    assert len(ctx.state["_judge_rejections"]) == 4
