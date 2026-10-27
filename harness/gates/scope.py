"""The scope gate. Every action that leaves the sandbox goes through `check`.

Three answers, not two. `allow` and `deny` are obvious. `ask` is the one that
earns its keep: it covers the actions a rule author could not decide in
advance, and it resolves differently depending on whether a person is watching.
Interactive, it blocks for a human. Batch, it denies. A batch run that silently
treated `ask` as `allow` would be a scope gate that is only a scope gate when
someone is looking at it.

Rules live in `scope-gate/`, per task. The default is deny: a host nobody
listed is out of scope, which is the only safe reading of an allowlist.

What the gate can and cannot see (the M5 lab)
---------------------------------------------
The gate judges the `Action` it is handed, nothing more.

* `http_request` hands it `kind=network` with a host and a port, so an
  out-of-scope target is denied before the request leaves.
* `shell` hands it `kind=shell` with the container the command would run in.
  The command string rides along for the trace, but no rule matches on it, and
  the gate does not parse it. `curl http://out-of-scope:8080/` in a shell is,
  to the gate, a shell command in the attacker container.

That gap is deliberate. Recovering network targets from shell text is a losing
game (variables, encodings, a script written to disk and run later), and a
parser that catches the easy cases teaches people the gate is complete. The
lab is to point the agent at the out-of-scope container and read the trace:
which attempts were denied, and which went straight past. Network-level
enforcement (an egress firewall on the attacker container) is what closes the
gap, and that is a container setting, not a Python rule.
"""

from __future__ import annotations

import dataclasses
import fnmatch
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclasses.dataclass
class Action:
    """Something the agent wants to do that leaves the sandbox."""

    kind: str                      # network | shell | browser
    host: str | None = None
    port: int | None = None
    url: str | None = None
    command: str | None = None
    container: str | None = None
    rationale: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


@dataclasses.dataclass
class Rule:
    """One allowlist or asklist entry."""

    decision: Decision
    kinds: tuple[str, ...] = ()        # empty means any kind
    hosts: tuple[str, ...] = ()        # fnmatch patterns, empty means any
    ports: tuple[int, ...] = ()        # empty means any
    containers: tuple[str, ...] = ()   # fnmatch patterns, empty means any
    note: str = ""

    def matches(self, action: Action) -> bool:
        if self.kinds and action.kind not in self.kinds:
            return False
        if self.hosts:
            host = action.host or _host_of(action.url)
            if host is None or not any(fnmatch.fnmatch(host, p) for p in self.hosts):
                return False
        if self.ports:
            if action.port is None or action.port not in self.ports:
                return False
        if self.containers:
            if action.container is None or not any(
                fnmatch.fnmatch(action.container, p) for p in self.containers
            ):
                return False
        return True

    def label(self) -> str:
        bits = [self.decision.value]
        if self.kinds:
            bits.append("kind=" + ",".join(self.kinds))
        if self.hosts:
            bits.append("host=" + ",".join(self.hosts))
        if self.ports:
            bits.append("port=" + ",".join(str(p) for p in self.ports))
        if self.containers:
            bits.append("container=" + ",".join(self.containers))
        return " ".join(bits)


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    from urllib.parse import urlsplit

    return urlsplit(url if "//" in url else f"//{url}").hostname


def _rules_from(block: Any, decision: Decision) -> list[Rule]:
    rules: list[Rule] = []
    for entry in block or []:
        entry = entry or {}
        rules.append(
            Rule(
                decision=decision,
                kinds=tuple(_listish(entry.get("kinds") or entry.get("kind"))),
                hosts=tuple(str(h) for h in _listish(entry.get("hosts") or entry.get("host"))),
                ports=tuple(int(p) for p in _listish(entry.get("ports") or entry.get("port"))),
                containers=tuple(
                    str(c) for c in _listish(entry.get("containers") or entry.get("container"))
                ),
                note=str(entry.get("note") or ""),
            )
        )
    return rules


def _listish(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


class ScopeGate:
    """Adjudicates actions against one task's rules."""

    def __init__(
        self,
        rules: Iterable[Rule] = (),
        interactive: bool = False,
        prompt: Callable[[Action], bool] | None = None,
        trace: Any = None,
        default: Decision = Decision.DENY,
    ) -> None:
        self.rules = list(rules)
        self.interactive = interactive
        self.prompt = prompt or _console_prompt
        self.trace = trace
        self.default = default
        self.decisions: list[tuple[Action, Decision, str | None]] = []

    # -- construction --------------------------------------------------------

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        task_id: str | None = None,
        interactive: bool = False,
        trace: Any = None,
        prompt: Callable[[Action], bool] | None = None,
    ) -> "ScopeGate":
        """Build a gate from parsed rules.

        Shape, with every field optional:

            default: deny
            tasks:
              <task id>:
                allow:
                  - {kind: network, host: "web_flag", ports: [1337]}
                  - {kind: shell, container: "harness-attacker-*"}
                ask:
                  - {kind: network, host: "*-db-*", note: "the task's own db"}
                deny:
                  - {kind: network, host: "*"}

        `shell` actions carry the attacker container's name, so a task that
        lets the agent use the shell at all needs a `kind: shell` allow rule
        for `harness-attacker-*`.
        """
        block = data or {}
        if task_id is not None:
            tasks = block.get("tasks") or {}
            block = {**block, **(tasks.get(task_id) or {})}
        rules: list[Rule] = []
        # Order matters: deny beats ask beats allow, so the strictest rule that
        # matches is the one that answers. Listing them in this order makes the
        # first match the right match.
        rules += _rules_from(block.get("deny"), Decision.DENY)
        rules += _rules_from(block.get("ask"), Decision.ASK)
        rules += _rules_from(block.get("allow"), Decision.ALLOW)
        default = Decision(str(block.get("default", "deny")))
        return cls(
            rules=rules, interactive=interactive, prompt=prompt, trace=trace, default=default
        )

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        task_id: str | None = None,
        interactive: bool = False,
        trace: Any = None,
    ) -> "ScopeGate":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_dict(data, task_id=task_id, interactive=interactive, trace=trace)

    @classmethod
    def deny_all(cls, trace: Any = None) -> "ScopeGate":
        """No rules loaded means nothing is in scope. Fail closed."""
        return cls(rules=(), interactive=False, trace=trace, default=Decision.DENY)

    # -- the hook -----------------------------------------------------------

    def check(self, action: Action) -> Decision:
        """Allow, deny, or ask about one action. Every call is traced."""
        rule = next((r for r in self.rules if r.matches(action)), None)
        decision = rule.decision if rule is not None else self.default
        label = rule.label() if rule is not None else f"default:{self.default.value}"

        if decision is Decision.ASK:
            if self.interactive:
                approved = bool(self.prompt(action))
                decision = Decision.ALLOW if approved else Decision.DENY
                label = f"{label} -> human:{decision.value}"
            else:
                # Nobody to ask. An unanswered question is not consent.
                decision = Decision.DENY
                label = f"{label} -> batch:deny"

        self.decisions.append((action, decision, label))
        if self.trace is not None:
            self.trace.scope_decision(
                action=action.as_dict(), decision=decision.value, rule=label
            )
        return decision


def _console_prompt(action: Action) -> bool:  # pragma: no cover - needs a terminal
    detail = ", ".join(f"{k}={v}" for k, v in action.as_dict().items())
    print(f"\n[scope] the agent wants to: {detail}")
    answer = input("[scope] allow this? [y/N] ").strip().lower()
    return answer in ("y", "yes")


# TODO(M5): replace this with the rules in `scope-gate/rules.yaml` once the
# multi-container Cybench task is chosen. One container is in scope, one is
# not, and the lab is to point the agent at the wrong one and read the trace.
# Until then the gate fails closed and `run.py` says so.
DEFAULT_RULES: dict[str, Any] = {
    "default": "deny",
    "tasks": {},
}
