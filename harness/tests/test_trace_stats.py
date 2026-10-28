"""P8: the stats table carries the last stop reason per role."""

from __future__ import annotations

import json

from harness.pricing import Usage
from harness.trace import Trace, read_trace
from harness.trace_stats import collect, main, render


def build_trace(path=None) -> Trace:
    """A trace with the exact shape that reads as "zero tool calls"."""
    trace = Trace(path=path)
    trace.run_start(run_id="abc123", task="test-task", config={"api_key": "sk-secret"})

    trace.role_start(role="build", model="vendor/big")
    trace.model_response(
        role="build",
        turn=0,
        model="vendor/big",
        stop_reason="max_tokens",
        usage=Usage(input_tokens=1000, output_tokens=32000, reasoning_tokens=31000),
        cost_usd=0.5,
        cumulative_usd=0.5,
    )
    trace.model_response(
        role="build",
        turn=1,
        model="vendor/big",
        stop_reason="tool_use",
        usage=Usage(input_tokens=2000, output_tokens=25000, reasoning_tokens=24000),
        cost_usd=0.6,
        cumulative_usd=1.1,
    )
    trace.budget(scope="run", spent_usd=1.1, cap_usd=1.0, breached=True,
                 last_stop_reason="tool_use")
    trace.role_end(role="build", status="budget_exceeded")

    trace.role_start(role="recon", model="vendor/small")
    trace.model_response(
        role="recon",
        turn=0,
        model="vendor/small",
        stop_reason="tool_use",
        usage=Usage(input_tokens=500, output_tokens=100, cache_read_tokens=400),
        cost_usd=0.01,
        cumulative_usd=1.11,
    )
    trace.tool_call(role="recon", turn=0, tool="shell", call_id="c1", arguments={"command": "ls"})
    trace.tool_result(role="recon", turn=0, tool="shell", call_id="c1", ok=True, content="a b")
    trace.tool_result(role="recon", turn=0, tool="shell", call_id="c2", ok=False, content="nope",
                      error="nope")
    trace.model_response(
        role="recon",
        turn=1,
        model="vendor/small",
        stop_reason="end_turn",
        usage=Usage(input_tokens=600, output_tokens=50),
        cost_usd=0.01,
        cumulative_usd=1.12,
    )
    trace.role_end(role="recon", status="completed")

    trace.readiness(target="task-web", method="host_only", ready=True)
    trace.gate_reject(gate="verdict", rule="negative_control", message="no control")
    trace.run_end(run_id="abc123", verdict="unconfirmed", failure_origin="budget_exceeded",
                  spent_usd=1.12, judge_invoked=False)
    return trace


def test_the_last_stop_reason_is_reported_per_role():
    roles, run = collect(build_trace().events)

    assert roles["build"].last_stop_reason == "tool_use"
    assert roles["recon"].last_stop_reason == "end_turn"
    # The "zero tool calls" reading: it is true and it means the opposite.
    assert roles["build"].tool_calls == 0
    assert roles["build"].output_tokens == 57000
    assert roles["build"].reasoning_tokens == 55000


def test_the_rendered_table_prints_the_last_stop_reason():
    roles, run = collect(build_trace().events)
    table = render(roles, run)

    assert "last_stop_reason" in table
    header_index = table.index("last_stop_reason")
    assert header_index > 0
    build_line = next(line for line in table.splitlines() if line.startswith("build"))
    assert build_line.rstrip().endswith("tool_use")
    recon_line = next(line for line in table.splitlines() if line.startswith("recon"))
    assert recon_line.rstrip().endswith("end_turn")


def test_overlapping_panelists_count_their_span_not_one_of_them():
    trace = Trace(path=None)
    trace.event("role_start", role="refute", model="a", ts=1.0)
    trace.event("role_start", role="refute", model="b", ts=2.0)
    trace.event("role_end", role="refute", status="completed", ts=6.0)
    trace.event("role_end", role="refute", status="completed", ts=11.0)
    # One after another, the next candidate's panel.
    trace.event("role_start", role="refute", model="a", ts=12.0)
    trace.event("role_end", role="refute", status="completed", ts=15.0)

    roles, _ = collect(trace.events)

    assert roles["refute"].wall_s == 10.0 + 3.0


def test_a_probe_over_the_agents_network_is_not_called_weak():
    """build.py counts shared_net as strong evidence; the table must agree."""
    trace = Trace(path=None)
    trace.event("readiness", readiness="shared_net")
    table = render(*collect(trace.events))
    assert "readiness        shared_net" in table
    assert "weaker than it looks" not in table


def test_the_accepted_verdict_shows_its_tier_and_trigger_origin():
    """Step 6 asks for trigger_origin; it should not take a grep to find."""
    trace = Trace(path=None)
    trace.tool_call(role="judge", turn=0, tool="record_verdict", call_id="v1",
                    arguments=json.dumps({"verdict": "confirmed", "proof_tier": "impact_confirmed",
                                          "trigger_origin": "harness_constructed"}))
    trace.tool_result(role="judge", turn=0, tool="record_verdict", call_id="v1",
                      ok=False, content=None, error="rule 6 caps this at exploitable")
    trace.tool_call(role="judge", turn=1, tool="record_verdict", call_id="v2",
                    arguments={"verdict": "confirmed", "proof_tier": "exploitable",
                               "trigger_origin": "harness_constructed"})
    trace.tool_result(role="judge", turn=1, tool="record_verdict", call_id="v2",
                      ok=True, content={"verdict": "confirmed"}, error=None)

    roles, run = collect(trace.events)
    table = render(roles, run)

    # The accepted call, not the rejected one before it.
    assert "proof_tier       exploitable" in table
    assert "trigger_origin   harness_constructed" in table


def test_the_summary_carries_the_verdict_and_the_failure_origin():
    roles, run = collect(build_trace().events)
    table = render(roles, run)

    assert "unconfirmed" in table
    assert "budget_exceeded" in table
    assert "gate rejections  1" in table
    assert "host_only" in table
    assert "weaker than it looks" in table


def test_per_role_counts_are_right():
    roles, _ = collect(build_trace().events)
    assert roles["recon"].turns == 2
    assert roles["recon"].tool_calls == 1
    assert roles["recon"].tool_errors == 1
    assert roles["recon"].cache_read_tokens == 400
    assert roles["recon"].status == "completed"
    assert roles["build"].status == "budget_exceeded"


def test_it_reads_a_file_and_skips_junk_lines(tmp_path, capsys):
    path = tmp_path / "trace.jsonl"
    build_trace(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("this line is not json\n\n")

    assert main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "last_stop_reason" in out
    assert "tool_use" in out


def test_a_missing_trace_is_an_error(tmp_path, capsys):
    assert main([str(tmp_path / "nope.jsonl")]) == 2
    assert "no such trace" in capsys.readouterr().err


def test_secrets_are_redacted_on_the_way_in(tmp_path):
    path = tmp_path / "trace.jsonl"
    build_trace(path)
    text = path.read_text(encoding="utf-8")
    assert "sk-secret" not in text
    assert "<redacted>" in text
    first = json.loads(text.splitlines()[0])
    assert first["config"]["api_key"] == "<redacted>"


def test_read_trace_yields_dicts(tmp_path):
    path = tmp_path / "trace.jsonl"
    build_trace(path)
    events = list(read_trace(path))
    assert events[0]["event"] == "run_start"
    assert events[-1]["event"] == "run_end"
