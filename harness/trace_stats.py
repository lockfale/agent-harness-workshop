"""Per-role statistics from a trace file.

    python -m harness.trace_stats runs/<task>-<ts>/trace.jsonl

The last stop reason is in the table on purpose, and it is the column to read
first. A role summarised as "57k tokens, zero tool calls" sounds like a model
that would not act. The same role with `stop_reason=tool_use` on its last turn
was a model that had finished deliberating and was about to make its first
call when something else ended the run. One number, two opposite conclusions,
and the stop reason is what tells them apart.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Iterable

from .trace import RoleStats, read_trace


def collect(events: Iterable[dict[str, Any]]) -> tuple[dict[str, RoleStats], dict[str, Any]]:
    """Fold a trace into per-role stats plus a run-level summary."""
    roles: dict[str, RoleStats] = {}
    order: list[str] = []
    starts: dict[str, float] = {}
    run: dict[str, Any] = {
        "run_id": None,
        "task": None,
        "verdict": None,
        "failure_origin": None,
        "spent_usd": 0.0,
        "gate_rejects": 0,
        "scope_denials": 0,
        "side_channel_errors": 0,
        "readiness": None,
        "judge_invoked": None,
    }

    def role_for(name: str) -> RoleStats:
        if name not in roles:
            roles[name] = RoleStats(role=name)
            order.append(name)
        return roles[name]

    for event in events:
        kind = event.get("event")
        role = event.get("role")

        if kind == "run_start":
            run["run_id"] = event.get("run_id")
            run["task"] = event.get("task")
        elif kind == "role_start" and role:
            stats = role_for(role)
            stats.model = event.get("model") or stats.model
            starts.setdefault(role, float(event.get("ts") or 0.0))
        elif kind == "role_end" and role:
            stats = role_for(role)
            stats.status = event.get("status") or stats.status
            start = starts.get(role)
            if start:
                stats.wall_s += max(0.0, float(event.get("ts") or start) - start)
                starts.pop(role, None)
        elif kind == "model_response" and role:
            stats = role_for(role)
            stats.turns += 1
            usage = event.get("usage") or {}
            stats.input_tokens += int(usage.get("input_tokens") or 0)
            stats.output_tokens += int(usage.get("output_tokens") or 0)
            stats.cache_read_tokens += int(usage.get("cache_read_tokens") or 0)
            stats.reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
            stats.cost_usd += float(event.get("cost_usd") or 0.0)
            # Last one wins, which is what makes it the last stop reason.
            stats.last_stop_reason = event.get("stop_reason")
        elif kind == "tool_call" and role:
            role_for(role).tool_calls += 1
        elif kind == "tool_result" and role and event.get("ok") is False:
            role_for(role).tool_errors += 1
        elif kind == "gate_reject":
            run["gate_rejects"] += 1
        elif kind == "scope_decision" and event.get("decision") == "deny":
            run["scope_denials"] += 1
        elif kind == "side_channel_error":
            run["side_channel_errors"] += 1
        elif kind == "readiness":
            run["readiness"] = event.get("readiness")
        elif kind == "run_end":
            run["verdict"] = event.get("verdict")
            run["failure_origin"] = event.get("failure_origin")
            run["spent_usd"] = float(event.get("spent_usd") or 0.0)
            run["judge_invoked"] = event.get("judge_invoked")

    return {name: roles[name] for name in order}, run


HEADER = (
    f"{'role':<8} {'turns':>5} {'tools':>5} {'err':>4} {'in':>9} {'out':>8} "
    f"{'cache':>8} {'reason':>8} {'cost':>9} {'wall':>7}  {'status':<20} last_stop_reason"
)


def render(roles: dict[str, RoleStats], run: dict[str, Any]) -> str:
    lines = [
        f"run {run.get('run_id') or '?'}   task {run.get('task') or '?'}",
        "",
        HEADER,
        "-" * len(HEADER),
    ]
    for stats in roles.values():
        lines.append(
            f"{stats.role:<8} {stats.turns:>5} {stats.tool_calls:>5} {stats.tool_errors:>4} "
            f"{stats.input_tokens:>9} {stats.output_tokens:>8} {stats.cache_read_tokens:>8} "
            f"{stats.reasoning_tokens:>8} {stats.cost_usd:>9.4f} {stats.wall_s:>6.1f}s  "
            f"{stats.status:<20} {stats.last_stop_reason or '-'}"
        )
    lines.append("")
    lines.append(f"verdict          {run.get('verdict') or '-'}")
    if run.get("failure_origin"):
        lines.append(f"failure_origin   {run['failure_origin']}")
    lines.append(f"judge_invoked    {run.get('judge_invoked')}")
    lines.append(f"total spend      ${run.get('spent_usd') or 0.0:.4f}")
    lines.append(f"gate rejections  {run.get('gate_rejects')}")
    lines.append(f"scope denials    {run.get('scope_denials')}")
    if run.get("readiness"):
        lines.append(f"readiness        {run['readiness']}")
        if run["readiness"] != "in_container":
            lines.append(
                "                 (not probed from inside the target, so the "
                "evidence is weaker than it looks)"
            )
    if run.get("side_channel_errors"):
        lines.append(
            f"side channels    {run['side_channel_errors']} error(s), swallowed. "
            f"Grep the trace for side_channel_error."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness.trace_stats", description="Per-role stats from a trace file."
    )
    parser.add_argument("trace", help="path to trace.jsonl")
    args = parser.parse_args(argv)

    path = Path(args.trace)
    if not path.exists():
        print(f"no such trace: {path}", file=sys.stderr)
        return 2
    roles, run = collect(read_trace(path))
    print(render(roles, run))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
