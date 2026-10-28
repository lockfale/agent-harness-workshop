"""M5: allow, deny, ask. And what `ask` means with nobody there to ask."""

from __future__ import annotations

from harness.gates.scope import Action, Decision, ScopeGate

RULES = {
    "default": "deny",
    "tasks": {
        "task-a": {
            "allow": [{"kind": "network", "host": "127.0.0.1", "ports": [1337]}],
            "ask": [{"kind": "shell", "containers": ["*-db"]}],
            "deny": [{"kind": "network", "hosts": ["*.example.com"]}],
        }
    },
}


def gate(interactive=False, prompt=None, trace=None, task_id="task-a"):
    return ScopeGate.from_dict(
        RULES, task_id=task_id, interactive=interactive, prompt=prompt, trace=trace
    )


def test_an_allowlisted_host_and_port_is_allowed(trace):
    decision = gate(trace=trace).check(Action(kind="network", host="127.0.0.1", port=1337))
    assert decision is Decision.ALLOW
    assert trace.of_kind("scope_decision")[0]["decision"] == "allow"


def test_the_wrong_port_on_an_allowlisted_host_is_denied():
    assert gate().check(Action(kind="network", host="127.0.0.1", port=8080)) is Decision.DENY


def test_an_unlisted_host_is_denied_by_default():
    assert gate().check(Action(kind="network", host="192.0.2.5", port=1337)) is Decision.DENY


def test_a_deny_rule_beats_an_allow_rule():
    assert gate().check(Action(kind="network", host="api.example.com")) is Decision.DENY


def test_ask_denies_in_batch_mode(trace):
    decision = gate(trace=trace).check(Action(kind="shell", container="task-a-db", command="ls"))
    assert decision is Decision.DENY
    event = trace.of_kind("scope_decision")[0]
    assert event["decision"] == "deny"
    assert "batch:deny" in event["rule"]


def test_ask_blocks_for_a_human_in_interactive_mode(trace):
    asked: list[Action] = []

    def prompt(action):
        asked.append(action)
        return True

    decision = gate(interactive=True, prompt=prompt, trace=trace).check(
        Action(kind="shell", container="task-a-db", command="ls")
    )
    assert decision is Decision.ALLOW
    assert len(asked) == 1
    assert "human:allow" in trace.of_kind("scope_decision")[0]["rule"]


def test_a_human_saying_no_denies():
    decision = gate(interactive=True, prompt=lambda action: False).check(
        Action(kind="shell", container="task-a-db", command="ls")
    )
    assert decision is Decision.DENY


def test_an_unknown_task_gets_no_rules_and_denies_everything():
    g = gate(task_id="task-nobody-configured")
    assert g.rules == []
    assert g.check(Action(kind="network", host="127.0.0.1", port=1337)) is Decision.DENY


def test_deny_all_is_the_fallback_gate():
    g = ScopeGate.deny_all()
    assert g.check(Action(kind="browser", url="https://example.org/")) is Decision.DENY


def test_a_url_is_matched_by_its_host():
    rules = {"default": "deny", "allow": [{"kind": "browser", "hosts": ["docs.local"]}]}
    g = ScopeGate.from_dict(rules)
    assert g.check(Action(kind="browser", url="https://docs.local/page")) is Decision.ALLOW
    assert g.check(Action(kind="browser", url="https://other.local/page")) is Decision.DENY


def test_every_decision_is_recorded(trace):
    g = gate(trace=trace)
    g.check(Action(kind="network", host="127.0.0.1", port=1337))
    g.check(Action(kind="network", host="192.0.2.1", port=22))
    assert len(trace.of_kind("scope_decision")) == 2
    assert len(g.decisions) == 2


def test_rules_load_from_a_file(tmp_path, trace):
    import yaml

    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(RULES), encoding="utf-8")
    g = ScopeGate.from_file(path, task_id="task-a", trace=trace)
    assert g.check(Action(kind="network", host="127.0.0.1", port=1337)) is Decision.ALLOW


def test_the_shipped_rules_let_the_labs_reach_the_target_and_nothing_else():
    from harness.run import DEFAULT_SCOPE_RULES

    g = ScopeGate.from_file(DEFAULT_SCOPE_RULES, task_id="flag-command")
    assert g.check(Action(kind="network", host="web_flag", port=1337)) is Decision.ALLOW
    assert g.check(
        Action(kind="shell", container="harness-attacker-flag-command", command="id")
    ) is Decision.ALLOW
    # The scope lab's decoy is out of scope before anyone writes a rule for it.
    assert g.check(Action(kind="network", host="locktalk", port=1337)) is Decision.DENY
    assert g.check(Action(kind="network", host="web_flag", port=22)) is Decision.DENY
