"""The trace: one JSON object per line, append only.

The trace is the run. If something is not in here you cannot say it happened,
and a summary that leaves out stop reasons and per-turn usage is worse than no
summary, because it reads as a conclusion.

"57k tokens, zero tool calls" sounds like a model that will not act. Read the
stop reasons and it was a model that had finished deliberating and was about to
make its first tool call. Same run, opposite conclusion.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from .pricing import Usage

REDACTED = "<redacted>"

# Config keys whose values never reach the trace.
SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "key",
        "token",
        "secret",
        "password",
        "authorization",
        "openrouter_api_key",
        "harness_api_key",
    }
)


def base_url_host(url: str | None) -> str | None:
    """Host of a base URL, with scheme, path, query and credentials dropped.

    The host is worth keeping: it tells you which gateway actually served the
    run, which is how you notice a long-lived process still pointing at last
    week's config. Everything else is either noise or a credential.
    """
    if not url:
        return None
    parts = urlsplit(url if "//" in url else f"//{url}")
    return parts.hostname or None


def redact(value: Any) -> Any:
    """Recursively replace secret-looking values, and reduce URLs to hosts."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in SECRET_KEYS:
                out[key] = REDACTED if item not in (None, "") else None
            elif lowered in ("base_url", "baseurl"):
                out[key] = base_url_host(item if isinstance(item, str) else None)
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bearer tokens and Authorization headers.
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # key=value / key: value for secret-looking names.
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization)(\s*[=:]\s*)(\"?)[^\s\"',;]+"),
     r"\1\2\3" + REDACTED),
    # Provider-style keys: sk-..., sk-or-v1-..., and long hex/base64-ish blobs after them.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
)
_URL = re.compile(r"\b(https?)://[^\s'\"<>]+")


def redact_text(text: str | None) -> str | None:
    """Scrub secrets and URLs out of free text, such as an exception message.

    Provider SDK errors often echo the request URL (with the gateway's path,
    and sometimes a key in the query string) or an Authorization header. URLs
    are reduced to scheme and host, the same as `base_url` in config.
    """
    if not text:
        return text
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)

    def _host_only(match: re.Match[str]) -> str:
        host = base_url_host(match.group(0))
        return f"{match.group(1)}://{host}" if host else match.group(0)

    return _URL.sub(_host_only, out)


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Cut `text` to `limit` characters. Returns (text, was_truncated)."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit] + f"... [{len(text) - limit} more characters]", True


class Trace:
    """JSONL event writer.

    Give it a path and it appends. Give it no path and it keeps events in
    memory, which is what the tests use.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        max_result_chars: int = 4000,
        clock: Any = time.time,
    ) -> None:
        self.path = Path(path) if path else None
        self.max_result_chars = max_result_chars
        self._clock = clock
        self.events: list[dict[str, Any]] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()

    # -- writing -------------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        record = {"ts": self._clock(), "event": kind}
        record.update(fields)
        self.events.append(record)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        return record

    # -- the required event set ---------------------------------------------

    def run_start(self, *, run_id: str, task: str, config: dict[str, Any]) -> None:
        self.event("run_start", run_id=run_id, task=task, config=redact(config))

    def role_start(self, *, role: str, model: str,
                   prompt_suffix: dict[str, str] | None = None) -> None:
        if prompt_suffix:
            self.event("role_start", role=role, model=model, prompt_suffix=prompt_suffix)
        else:
            self.event("role_start", role=role, model=model)

    def role_end(self, *, role: str, status: str, detail: str | None = None) -> None:
        self.event("role_end", role=role, status=status, detail=detail)

    def model_response(
        self,
        *,
        role: str,
        turn: int,
        model: str,
        stop_reason: str | None,
        usage: Usage,
        cost_usd: float,
        cumulative_usd: float,
        raw_stop_reason: str | None = None,
    ) -> None:
        self.event(
            "model_response",
            role=role,
            turn=turn,
            model=model,
            stop_reason=stop_reason,
            raw_stop_reason=raw_stop_reason,
            usage=usage.as_dict(),
            cost_usd=round(cost_usd, 6),
            cumulative_usd=round(cumulative_usd, 6),
        )

    def tool_call(
        self, *, role: str, turn: int, tool: str, call_id: str | None, arguments: Any
    ) -> None:
        text, cut = truncate(
            arguments if isinstance(arguments, str) else json.dumps(arguments, default=str),
            self.max_result_chars,
        )
        self.event(
            "tool_call",
            role=role,
            turn=turn,
            tool=tool,
            call_id=call_id,
            arguments=text,
            truncated=cut,
        )

    def tool_result(
        self,
        *,
        role: str,
        turn: int,
        tool: str,
        call_id: str | None,
        ok: bool,
        content: Any,
        error: str | None = None,
    ) -> None:
        text, cut = truncate(
            content if isinstance(content, str) else json.dumps(content, default=str),
            self.max_result_chars,
        )
        self.event(
            "tool_result",
            role=role,
            turn=turn,
            tool=tool,
            call_id=call_id,
            ok=ok,
            content=text,
            error=error,
            truncated=cut,
        )

    def gate_reject(self, *, gate: str, rule: str, message: str, **extra: Any) -> None:
        self.event("gate_reject", gate=gate, rule=rule, message=message, **extra)

    def budget(self, *, scope: str, spent_usd: float, cap_usd: float | None, breached: bool,
               last_stop_reason: str | None = None) -> None:
        self.event(
            "budget",
            scope=scope,
            spent_usd=round(spent_usd, 6),
            cap_usd=cap_usd,
            breached=breached,
            last_stop_reason=last_stop_reason,
        )

    def stage_status(self, *, stage: str, outcome: str, detail: str | None = None,
                     **extra: Any) -> None:
        self.event("stage_status", stage=stage, outcome=outcome, detail=detail, **extra)

    def scope_decision(self, *, action: dict[str, Any], decision: str, rule: str | None) -> None:
        self.event("scope_decision", action=action, decision=decision, rule=rule)

    def readiness(self, *, target: str, method: str, ready: bool, detail: str | None = None,
                  **extra: Any) -> None:
        self.event("readiness", target=target, readiness=method, ready=ready, detail=detail,
                   **extra)

    def side_channel_error(self, *, channel: str, error: str) -> None:
        self.event("side_channel_error", channel=channel, error=error)

    def note(self, message: str, **fields: Any) -> None:
        self.event("note", message=message, **fields)

    def run_end(self, *, run_id: str, verdict: str | None, failure_origin: str | None,
                spent_usd: float, **extra: Any) -> None:
        self.event(
            "run_end",
            run_id=run_id,
            verdict=verdict,
            failure_origin=failure_origin,
            spent_usd=round(spent_usd, 6),
            **extra,
        )

    # -- reading -------------------------------------------------------------

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("event") == kind]


def read_trace(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield events from a trace file, skipping blank and unparseable lines."""
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


@dataclasses.dataclass
class RoleStats:
    """What `trace_stats` reports for one role."""

    role: str
    model: str = ""
    turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0
    status: str = ""
    last_stop_reason: str | None = None
