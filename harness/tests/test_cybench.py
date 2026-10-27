"""The Cybench adapter and the attacker container: the agent never touches
the target container or the task directory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import cybench
from harness.cybench import CybenchTask, LocalDockerAdapter, slugify
from harness.gates.scope import ScopeGate
from harness.tools.http import http_handler
from harness.tools.registry import ToolContext
from harness.tools.shell import shell_handler


@pytest.mark.parametrize(
    "name,slug",
    [
        ("[Very Easy] Flag Command", "flag-command"),
        ("[Medium] LockTalk", "locktalk"),
        ("Labyrinth Linguist", "labyrinth-linguist"),
        ("flag-command", "flag-command"),
        ("  [Hard]  Weird__Name!! v2 ", "weird-name-v2"),
        ("[]", "task"),
    ],
)
def test_slugify(name, slug):
    assert slugify(name) == slug


def make_task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / "[Very Easy] Flag Command"
    (task_dir / "metadata" / "solution").mkdir(parents=True)
    (task_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    container_name: web_flag\n"
        "    build: .\n"
        "    ports: ['1337:1337']\n"
        "    healthcheck: {test: ['CMD', 'nc', '-z', 'localhost', '1337']}\n"
        "    networks: [shared_net]\n"
        "networks:\n"
        "  shared_net: {external: true}\n",
        encoding="utf-8",
    )
    (task_dir / "metadata" / "metadata.json").write_text(
        json.dumps(
            {
                "target_host": "web_flag:1337",
                "hard_prompt": "Find the flag on localhost:1337.",
                "easy_prompt": "Easy version.",
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "flag.txt").write_text("HTB{not-for-the-agent}", encoding="utf-8")
    (task_dir / "init_script.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    return task_dir


def test_a_task_dir_loads_with_a_slug_and_metadata(tmp_path):
    task = LocalDockerAdapter(tasks={}).load(str(make_task_dir(tmp_path)))
    assert task.task_id == "flag-command"
    assert task.source_name == "[Very Easy] Flag Command"
    assert (task.target_host, task.target_port) == ("web_flag", 1337)
    assert task.container == "web_flag"
    assert task.has_healthcheck is True
    assert task.attacker_container == "harness-attacker-flag-command"
    assert task.services == ["web"]
    # The prompt comes from metadata, and the fixed target line overrides localhost.
    assert task.prompt.startswith("Find the flag")
    assert "web_flag:1337" in task.prompt
    assert "HTB{" not in task.prompt


def test_tasks_entries_are_keyed_by_the_slug(tmp_path):
    adapter = LocalDockerAdapter(tasks={"flag-command": {"control_command": "true",
                                                         "easy_prompt": True}})
    task = adapter.load(str(make_task_dir(tmp_path)))
    assert task.control_command == "true"
    assert task.prompt.startswith("Easy version.")


def test_scope_rules_key_on_the_slug(tmp_path):
    task = LocalDockerAdapter(tasks={}).load(str(make_task_dir(tmp_path)))
    gate = ScopeGate.from_dict(
        {"tasks": {"flag-command": {"allow": [{"kind": "network", "host": "web_flag"}]}}},
        task_id=task.task_id,
    )
    assert len(gate.rules) == 1


def test_the_compose_override_resets_published_ports(tmp_path):
    adapter = LocalDockerAdapter(tasks={})
    task = adapter.load(str(make_task_dir(tmp_path)))
    args = adapter.compose_args(task)

    assert args[:3] == ["compose", "-p", "flag-command"]
    files = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
    assert files[0] == str(task.compose_file)
    override = Path(files[1])
    assert "ports: !reset []" in override.read_text(encoding="utf-8")
    assert '"web":' in override.read_text(encoding="utf-8")
    # Written outside the task directory, which is never modified.
    assert task.workdir not in override.parents


def test_compose_up_passes_the_override(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cybench, "_run", lambda *argv, **kw: calls.append(argv))
    adapter = LocalDockerAdapter(tasks={})
    task = adapter.load(str(make_task_dir(tmp_path)))
    adapter._compose_up(task)
    up = calls[0]
    assert up.count("-f") == 2 and "up" in up
    override = up[up.index("-f", up.index("-f") + 1) + 1]
    assert "!reset" in Path(override).read_text(encoding="utf-8")


def test_the_task_dir_is_never_mounted_into_the_attacker(tmp_path):
    task = LocalDockerAdapter(tasks={}).load(str(make_task_dir(tmp_path)))
    argv = cybench.attacker_argv(task)
    joined = " ".join(argv)
    assert str(task.workdir) not in joined
    assert "flag" not in joined.replace("flag-command", "")
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert mounts == [f"{task.files_volume}:{cybench.FILES_MOUNT}:ro"]


def test_init_script_runs_in_a_throwaway_container_with_the_task_dir_read_only(tmp_path):
    task = LocalDockerAdapter(tasks={}).load(str(make_task_dir(tmp_path)))
    argv = cybench.init_files_argv(task)
    assert argv[:2] == ["run", "--rm"]
    assert f"{task.workdir}:/task:ro" in argv
    assert task.attacker_image in argv


def test_start_never_runs_init_script_on_the_host(tmp_path, monkeypatch):
    docker_calls, host_calls = [], []
    monkeypatch.setattr(cybench, "_run", lambda *argv, **kw: docker_calls.append(argv)
                        or type("P", (), {"returncode": 0, "stdout": ""})())
    monkeypatch.setattr(cybench.subprocess, "run", lambda *a, **k: host_calls.append(a))
    adapter = LocalDockerAdapter(tasks={})
    adapter.start(adapter.load(str(make_task_dir(tmp_path))))
    assert host_calls == []
    assert any("init_script.sh" in " ".join(c) for c in docker_calls)


# --- the agent's tools never reach the target container ------------------


class RecordingExec:
    def __init__(self, result=(0, "ok", "")):
        self.calls = []
        self.result = result

    def __call__(self, container, command, timeout_s=60, stdin=None):
        self.calls.append({"container": container, "command": command, "stdin": stdin})
        return self.result


def allow_all_gate():
    return ScopeGate.from_dict(
        {"allow": [{"kind": "shell"}, {"kind": "network", "host": "web_flag"}]}
    )


def test_the_shell_runs_in_the_attacker_container_not_the_target(monkeypatch):
    recorder = RecordingExec()
    monkeypatch.setattr("harness.tools.shell.docker_exec", recorder)
    task = CybenchTask(task_id="t", container="web_flag", attacker_container="harness-attacker-t")
    result = shell_handler({"command": "id"}, ToolContext(role="recon", task=task,
                                                          scope=allow_all_gate()))
    assert result.ok
    assert [c["container"] for c in recorder.calls] == ["harness-attacker-t"]


def test_the_shell_refuses_without_an_attacker_container(monkeypatch):
    recorder = RecordingExec()
    monkeypatch.setattr("harness.tools.shell.docker_exec", recorder)
    task = CybenchTask(task_id="t", container="web_flag")  # target only
    result = shell_handler({"command": "cat /flag.txt"},
                           ToolContext(role="recon", task=task, scope=allow_all_gate()))
    assert not result.ok
    assert "attacker container" in result.error
    assert recorder.calls == []


def test_a_denied_shell_command_is_not_executed(monkeypatch):
    recorder = RecordingExec()
    monkeypatch.setattr("harness.tools.shell.docker_exec", recorder)
    task = CybenchTask(task_id="t", attacker_container="harness-attacker-t")
    result = shell_handler({"command": "id"},
                           ToolContext(role="recon", task=task, scope=ScopeGate.deny_all()))
    assert not result.ok
    assert result.content["decision"] == "deny"
    assert recorder.calls == []


def test_http_request_goes_through_the_scope_gate_by_host_and_port(monkeypatch, trace):
    recorder = RecordingExec((0, json.dumps({"status": 200, "headers": {}, "body": "hi"}), ""))
    monkeypatch.setattr("harness.tools.http.docker_exec", recorder)
    task = CybenchTask(task_id="t", attacker_container="harness-attacker-t")
    gate = ScopeGate.from_dict(
        {"allow": [{"kind": "network", "host": "web_flag", "ports": [1337]}]}, trace=trace
    )
    ctx = ToolContext(role="recon", task=task, scope=gate)

    denied = http_handler({"method": "GET", "url": "http://other_box:1337/"}, ctx)
    assert not denied.ok and recorder.calls == []

    wrong_port = http_handler({"method": "GET", "url": "http://web_flag/"}, ctx)
    assert not wrong_port.ok and recorder.calls == []

    allowed = http_handler({"method": "GET", "url": "http://web_flag:1337/"}, ctx)
    assert allowed.ok and allowed.content["status"] == 200
    assert recorder.calls[0]["container"] == "harness-attacker-t"
    assert json.loads(recorder.calls[0]["stdin"])["url"] == "http://web_flag:1337/"

    decisions = [e["decision"] for e in trace.of_kind("scope_decision")]
    assert decisions == ["deny", "deny", "allow"]


def test_the_gate_does_not_parse_shell_commands(monkeypatch):
    """The M5 lesson, pinned: curl in a shell is a shell action, not a network one."""
    recorder = RecordingExec()
    monkeypatch.setattr("harness.tools.shell.docker_exec", recorder)
    task = CybenchTask(task_id="t", attacker_container="harness-attacker-t")
    gate = ScopeGate.from_dict(
        {"allow": [{"kind": "shell"}], "deny": [{"kind": "network", "host": "other_box"}]}
    )
    result = shell_handler({"command": "curl http://other_box:1337/"},
                           ToolContext(role="recon", task=task, scope=gate))
    assert result.ok and len(recorder.calls) == 1


# --- readiness over shared_net -------------------------------------------


def test_readiness_probes_the_target_from_the_attacker_container(monkeypatch, trace):
    from harness.roles import build

    recorder = RecordingExec()
    monkeypatch.setattr(build, "docker_exec", recorder)
    monkeypatch.setattr(build, "container_health", lambda c: "healthy")
    task = CybenchTask(task_id="t", container="web_flag", attacker_container="harness-attacker-t",
                       target_host="web_flag", target_port=1337, has_healthcheck=True)
    result = build.check_readiness(task, trace)

    assert result.ready and result.method == "shared_net" and not result.weak
    assert recorder.calls[0]["container"] == "harness-attacker-t"
    assert recorder.calls[0]["command"][-2:] == ["web_flag", "1337"]


def test_an_unhealthy_but_reachable_target_is_ready(monkeypatch, trace):
    """Flag Command: BusyBox nc has no -z, so its healthcheck says unhealthy
    while the service answers on shared_net. Health is advisory only."""
    from harness.roles import build

    monkeypatch.setattr(build, "docker_exec", RecordingExec())
    monkeypatch.setattr(build, "container_health", lambda c: "unhealthy")
    task = CybenchTask(task_id="t", container="web_flag", attacker_container="a",
                       target_host="web_flag", target_port=1337, has_healthcheck=True)
    result = build.check_readiness(task, trace)

    assert result.ready is True
    assert result.health == "unhealthy"
    event = trace.of_kind("readiness")[0]
    assert event["ready"] is True and event["health_advisory"] == "unhealthy"


def test_an_unreachable_target_fails_after_bounded_retries(monkeypatch):
    from harness.roles import build

    recorder = RecordingExec((1, "", "ConnectionRefusedError"))
    monkeypatch.setattr(build, "docker_exec", recorder)
    monkeypatch.setattr(build, "container_health", lambda c: "healthy")
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    task = CybenchTask(task_id="t", container="web_flag", attacker_container="a",
                       target_host="web_flag", target_port=1337, has_healthcheck=True)
    result = build._probe_shared_net(task, "a", "web_flag", 1337, 5, total_s=30,
                                     sleep=sleep, clock=lambda: now[0])

    assert result.ready is False
    assert result.attempts == len(recorder.calls) > 1
    assert sum(sleeps) <= 30
    # Backoff grows to its cap; only the last sleep is clipped to the deadline.
    assert sleeps[:-1] == sorted(sleeps[:-1]) and max(sleeps) <= build.READINESS_MAX_DELAY_S
    assert "after" in result.detail and "ConnectionRefusedError" in result.detail
    # Healthy compose status does not rescue an unreachable service either.
    assert result.health == "healthy"


def test_a_target_that_comes_up_during_the_retries_is_ready(monkeypatch):
    from harness.roles import build

    answers = iter([(1, "", "refused"), (1, "", "refused"), (0, "", "")])
    monkeypatch.setattr(build, "docker_exec", lambda *a, **k: next(answers))
    task = CybenchTask(task_id="t", attacker_container="a", target_host="h", target_port=1)
    result = build._probe_shared_net(task, "a", "h", 1, 5, total_s=60,
                                     sleep=lambda s: None, clock=lambda: 0.0)
    assert result.ready and result.attempts == 3


def test_an_unreachable_target_becomes_harness_failure(tmp_path, monkeypatch):
    """Through the build stage: retries exhausted -> harness_failure, no judge."""
    from harness.roles import build

    from .test_run import FakeLLM, pipeline_for, working_script

    monkeypatch.setattr(build, "docker_exec", RecordingExec((1, "", "refused")))
    monkeypatch.setattr(build, "READINESS_TOTAL_S", 0.0)
    pipeline, _ = pipeline_for(tmp_path, FakeLLM(working_script()))
    pipeline.task = CybenchTask(task_id="t", attacker_container="a",
                                target_host="web_flag", target_port=1337)
    status = build.run(pipeline)
    assert status.outcome.value == "harness_failure"
    assert "not reachable" in status.detail


def test_compose_up_does_not_wait_on_healthchecks(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cybench, "_run", lambda *argv, **kw: calls.append(argv))
    adapter = LocalDockerAdapter(tasks={})
    task = adapter.load(str(make_task_dir(tmp_path)))
    adapter._compose_up(task)
    assert "up" in calls[0]
    assert "--wait" not in calls[0]


REAL_TASK = Path(__file__).resolve().parents[2].parent / "agent-harness-workshop" / "tasks" / "flag-command"


@pytest.mark.skipif(not REAL_TASK.is_dir(), reason="real Flag Command task dir not present")
def test_the_real_flag_command_task_dir_loads():
    """Read-only check against the provisional task in the main checkout."""
    adapter = LocalDockerAdapter(tasks={})
    task = adapter.load(str(REAL_TASK))
    assert task.task_id == "flag-command"
    assert (task.target_host, task.target_port) == ("web_flag", 1337)
    assert task.container == "web_flag" and task.has_healthcheck
    argv = " ".join(cybench.attacker_argv(task))
    assert str(REAL_TASK) not in argv
    assert "!reset" in cybench.compose_override(task.services)


def test_a_stub_task_says_it_is_a_stub(trace):
    from harness.roles import build

    result = build.check_readiness(cybench.StubAdapter().load("x"), trace)
    assert result.ready and result.method == "stub" and result.weak


# --- the workshop's TASKS entries and --with-task ------------------------


def make_repo_with_tasks(tmp_path: Path) -> Path:
    """A fake repo root with tasks/flag-command and tasks/locktalk."""
    root = tmp_path / "repo"
    fc = root / "tasks" / "flag-command"
    (fc / "metadata").mkdir(parents=True)
    (fc / "docker-compose.yml").write_text(
        "services:\n  web_flag:\n    container_name: web_flag\n    build: ./\n"
        "    ports: ['1337:1337']\n    networks: [shared_net]\n"
        "    healthcheck: {test: ['CMD-SHELL', 'nc -z localhost 1337 || exit 1']}\n"
        "networks:\n  shared_net: {external: true}\n", encoding="utf-8")
    (fc / "metadata" / "metadata.json").write_text(json.dumps(
        {"target_host": "web_flag:1337", "hard_prompt": "url: 'web_flag:1337'"}), encoding="utf-8")
    lt = root / "tasks" / "locktalk"
    (lt / "metadata").mkdir(parents=True)
    (lt / "docker-compose.yml").write_text(
        "services:\n  locktalk:\n    container_name: locktalk\n    build: .\n"
        "    ports: ['1337:1337']\n    networks: [shared_net]\n"
        "networks:\n  shared_net: {external: true}\n", encoding="utf-8")
    (lt / "metadata" / "metadata.json").write_text(json.dumps(
        {"target_host": "locktalk:1337", "hard_prompt": "decoy"}), encoding="utf-8")
    return root


def test_both_tasks_entries_load(tmp_path):
    root = make_repo_with_tasks(tmp_path)
    adapter = LocalDockerAdapter(root=root, with_tasks=["locktalk"])
    task = adapter.load("flag-command")

    assert task.task_id == "flag-command"
    assert task.workdir == (root / "tasks" / "flag-command").resolve()
    assert (task.container, task.target_host, task.target_port) == ("web_flag", "web_flag", 1337)
    assert task.control_marker == "HTB{"
    # BusyBox resolves localhost to ::1; Flask listens on IPv4 only.
    assert "127.0.0.1:1337" in task.control_command and "localhost" not in task.control_command
    assert "localhost" not in (task.probe_command or "")
    assert [c.task_id for c in task.companions] == ["locktalk"]
    assert task.containers == ["web_flag", "locktalk"]

    decoy = task.companions[0]
    assert decoy.workdir == (root / "tasks" / "locktalk").resolve()
    assert decoy.attacker_container is None and decoy.control_command is None
    assert decoy.prompt == ""


def test_workdirs_resolve_against_the_repo_root_not_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = LocalDockerAdapter(tasks={"x": {"workdir": "tasks/x"}}).load("x")
    assert task.workdir == (cybench.REPO_ROOT / "tasks" / "x").resolve()


def test_the_decoy_cannot_be_the_main_task(tmp_path):
    with pytest.raises(ValueError, match="--with-task locktalk"):
        LocalDockerAdapter(root=make_repo_with_tasks(tmp_path)).load("locktalk")


def test_with_task_composes_both_and_stops_both(tmp_path, monkeypatch):
    calls = []

    def fake_run(*argv, **kw):
        calls.append(argv)
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(cybench, "_run", fake_run)
    adapter = LocalDockerAdapter(root=make_repo_with_tasks(tmp_path), with_tasks=["locktalk"])
    task = adapter.load("flag-command")
    adapter.start(task)
    ups = [c for c in calls if c[:1] == ("compose",) and "up" in c]
    assert [c[2] for c in ups] == ["flag-command", "locktalk"]
    assert all(c.count("-f") == 2 for c in ups), "both get the ports override"

    calls.clear()
    adapter.stop(task)
    downs = [c for c in calls if c[:1] == ("compose",) and "down" in c]
    assert sorted(c[2] for c in downs) == ["flag-command", "locktalk"]


def test_with_task_stops_both_in_finally_when_a_stage_crashes(tmp_path, monkeypatch):
    import harness.run as run_module
    from harness.config import load_config
    from harness.run import build_pipeline, run_pipeline

    from .conftest import FakeLLM, write_config

    calls = []
    monkeypatch.setattr(cybench, "_run", lambda *argv, **kw: calls.append(argv)
                        or type("P", (), {"returncode": 0, "stdout": ""})())

    def crash(_pipeline):
        raise RuntimeError("boom")

    monkeypatch.setattr(run_module, "STAGE_FUNCS", [("recon", crash)])
    adapter = LocalDockerAdapter(root=make_repo_with_tasks(tmp_path), with_tasks=["locktalk"])
    config = load_config(roles_path=write_config(tmp_path), run_budget_usd=1.0)
    pipeline = build_pipeline(task_id="flag-command", config=config, run_dir=tmp_path / "run",
                              adapter=adapter, llm=FakeLLM(), interactive=False)
    run_pipeline(pipeline, task_id="flag-command", adapter=adapter)

    downs = [c[2] for c in calls if c[:1] == ("compose",) and "down" in c]
    assert sorted(downs) == ["flag-command", "locktalk"]


def test_the_decoy_is_never_the_shell_or_target_container(tmp_path, monkeypatch):
    task = LocalDockerAdapter(root=make_repo_with_tasks(tmp_path),
                              with_tasks=["locktalk"]).load("flag-command")
    assert task.container == "web_flag"
    assert task.target_host == "web_flag"
    assert task.attacker_container == "harness-attacker-flag-command"
    assert "locktalk" not in task.prompt

    recorder = RecordingExec()
    monkeypatch.setattr("harness.tools.shell.docker_exec", recorder)
    shell_handler({"command": "id"}, ToolContext(role="recon", task=task, scope=allow_all_gate()))
    assert recorder.calls[0]["container"] == "harness-attacker-flag-command"


def test_with_task_needs_the_cybench_adapter():
    with pytest.raises(ValueError):
        cybench.adapter_named("stub", with_tasks=["locktalk"])


MAIN_REPO = Path(__file__).resolve().parents[2].parent / "agent-harness-workshop"


@pytest.mark.skipif(not (MAIN_REPO / "tasks" / "locktalk").is_dir(),
                    reason="provisional task dirs not present")
def test_the_real_tasks_entries_load():
    task = LocalDockerAdapter(root=MAIN_REPO, with_tasks=["locktalk"]).load("flag-command")
    assert (task.container, task.target_host, task.target_port) == ("web_flag", "web_flag", 1337)
    assert task.containers == ["web_flag", "locktalk"]
    assert task.companions[0].targets == ["locktalk:1337"]


# --- names SETUP.md and verify.sh on main depend on -----------------------


def test_names_match_setup_and_verify(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    assert cybench.ATTACKER_IMAGE == "harness-attacker:latest"
    assert cybench.ATTACKER_DOCKERFILE_DIR == repo / "harness" / "attacker"
    for rel in ("harness/attacker/Dockerfile", "harness/requirements.txt",
                "config/roles.yaml", "config/prices.yaml"):
        assert (repo / rel).is_file(), rel

    adapter = LocalDockerAdapter(root=make_repo_with_tasks(tmp_path), with_tasks=["locktalk"])
    task = adapter.load("flag-command")
    decoy = task.companions[0]
    assert (task.project, decoy.project) == ("flag-command", "locktalk")
    assert cybench.compose_image(task, task.services[0]) == "flag-command-web_flag:latest"
    assert cybench.compose_image(decoy, decoy.services[0]) == "locktalk-locktalk:latest"
    for each in (task, decoy):
        args = adapter.compose_args(each)
        assert args[1:3] == ["-p", each.project], "project name must be explicit"


def test_compose_up_does_not_force_a_rebuild(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cybench, "_run", lambda *argv, **kw: calls.append(argv))
    adapter = LocalDockerAdapter(tasks={})
    adapter._compose_up(adapter.load(str(make_task_dir(tmp_path))))
    assert "--build" not in calls[0]
