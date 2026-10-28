"""A structured HTTP tool. Its host and port go through the scope gate.

This is the counterpart to the shell tool, and the pair is the M5 lab. The
shell is checked as "a command in the attacker container" and nothing more,
because the gate does not parse commands. This tool is different: the request
is a schema'd object, so the gate sees the host and the port before anything
leaves, and an out-of-scope target is denied with a rule in the trace.

Same agent, same target, two tools: one the gate can reason about and one it
cannot. Point the agent at the out-of-scope container and read which attempts
show up as `scope_decision deny`.

The request is sent from inside the attacker container, so hostnames resolve
on the task network exactly as they would for the shell. The request spec goes
in on stdin as JSON and a fixed Python script sends it: nothing the model
writes is ever interpreted by a shell.

Redirects are not followed. A followed redirect is a second request to a host
the gate never saw, so the tool returns the `Location` header instead and the
agent has to ask again, through the gate.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from urllib.parse import urlsplit

from ..gates.scope import Action, Decision
from .registry import ToolContext, ToolRegistry, ToolResult
from .shell import MAX_OUTPUT_CHARS, attacker_container_for, docker_exec

HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

HTTP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "enum": list(HTTP_METHODS)},
        "url": {
            "type": "string",
            # Anchored at both ends on purpose. Some providers enforce tool
            # schemas while decoding and read `pattern` as a full match, so
            # "^https?://" alone lets the model emit only "http://".
            "pattern": "^https?://\\S+$",
            "description": "Absolute http(s) URL. Use the target's hostname on the task network.",
        },
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Request headers, name to value.",
        },
        "body": {"type": "string", "description": "Request body, sent as-is."},
        "timeout_s": {"type": "integer", "minimum": 1, "maximum": 120},
        "rationale": {
            "type": "string",
            "description": "One line on why you are sending this. Shows up in the trace.",
        },
    },
    "required": ["method", "url"],
    "additionalProperties": False,
}

DEFAULT_TIMEOUT_S = 30

# Runs inside the attacker container. Reads one JSON request on stdin, writes
# one JSON response on stdout. Standard library only.
_SENDER = r"""
import json, sys, urllib.request, urllib.error
spec = json.load(sys.stdin)
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
opener = urllib.request.build_opener(NoRedirect)
data = spec["body"].encode() if spec.get("body") is not None else None
req = urllib.request.Request(spec["url"], data=data, method=spec["method"],
                             headers=spec.get("headers") or {})
try:
    resp = opener.open(req, timeout=spec["timeout_s"])
    status, headers, body = resp.status, dict(resp.headers), resp.read()
except urllib.error.HTTPError as e:
    status, headers, body = e.code, dict(e.headers or {}), e.read()
print(json.dumps({"status": status, "headers": headers,
                  "body": body.decode("utf-8", "replace")}))
"""


def target_of(url: str) -> tuple[str | None, int | None]:
    """(host, port) for a URL, with the port defaulted from the scheme."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return parts.hostname, None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return parts.hostname, port


def http_handler(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    container = attacker_container_for(ctx)
    if not container:
        return ToolResult.failure(
            "no attacker container is configured, so there is nowhere sandboxed to "
            "send this from. The tool does not send requests from the host."
        )

    url = arguments["url"]
    host, port = target_of(url)
    if host is None or port is None:
        return ToolResult.failure(f"could not read a host and port from {url!r}")

    action = Action(
        kind="network",
        host=host,
        port=port,
        url=url,
        container=container,
        rationale=arguments.get("rationale"),
    )
    if ctx.scope is None:
        return ToolResult.failure(
            "no scope gate is configured, so this request cannot be checked and was not sent."
        )
    decision = ctx.scope.check(action)
    if decision is not Decision.ALLOW:
        return ToolResult.failure(
            f"the scope gate returned {decision.value} for {host}:{port}. The request "
            f"was not sent.",
            decision=decision.value,
        )

    timeout_s = int(arguments.get("timeout_s") or DEFAULT_TIMEOUT_S)
    spec = {
        "method": arguments["method"],
        "url": url,
        "headers": arguments.get("headers") or {},
        "body": arguments.get("body"),
        "timeout_s": timeout_s,
    }
    try:
        code, out, err = docker_exec(
            container, ["python3", "-c", _SENDER], timeout_s + 10, stdin=json.dumps(spec)
        )
    except subprocess.TimeoutExpired:
        return ToolResult.failure(f"request timed out after {timeout_s}s", timed_out=True)
    except FileNotFoundError as exc:
        return ToolResult.failure(str(exc))

    if code != 0:
        return ToolResult.failure(
            "the request could not be sent",
            stderr=err[-2000:],
        )
    try:
        response = json.loads(out)
    except json.JSONDecodeError:
        return ToolResult.failure("the sender returned something that is not JSON",
                                  stdout=out[:2000])
    body = str(response.get("body") or "")
    return ToolResult(
        content={
            "status": response.get("status"),
            "headers": response.get("headers") or {},
            "body": body[:MAX_OUTPUT_CHARS],
            "body_truncated": len(body) > MAX_OUTPUT_CHARS,
        }
    )


def register_http(registry: ToolRegistry, name: str = "http_request") -> None:
    registry.register(
        name=name,
        description=(
            "Send one HTTP request from the attacker container. The host and port "
            "are checked against the task's scope rules before anything is sent. "
            "Redirects are not followed; a 3xx comes back with its Location header."
        ),
        schema=HTTP_SCHEMA,
        handler=http_handler,
    )
