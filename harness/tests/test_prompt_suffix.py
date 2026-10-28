"""The prompt A/B mechanism: a per-role suffix file, appended, hashed, never traced."""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml

from harness.config import load_config
from harness.run import build_pipeline, main, run_pipeline
from harness.cybench import StubAdapter
from harness.roles import recon

from .conftest import FakeLLM, finish_turn, write_config

SECRET_LINE = "Pick the single most informative action. ZEBRA-7731"


def config_with_suffix(tmp_path, suffix_path):
    roles_path = write_config(tmp_path)
    roles = yaml.safe_load(roles_path.read_text(encoding="utf-8"))
    roles["roles"]["recon"]["system_prompt_suffix_file"] = str(suffix_path)
    roles_path.write_text(yaml.safe_dump(roles), encoding="utf-8")
    return roles_path


class CapturingLLM(FakeLLM):
    def complete(self, **kwargs):
        self.calls.append({"role": kwargs.get("role"), "messages": kwargs["messages"]})
        return finish_turn()


def run_once(tmp_path, roles_path):
    llm = CapturingLLM()
    config = load_config(roles_path=roles_path, run_budget_usd=1.0)
    adapter = StubAdapter()
    pipeline = build_pipeline(task_id="t", config=config, run_dir=tmp_path / "run",
                              adapter=adapter, llm=llm, interactive=False)
    run_pipeline(pipeline, task_id="t", adapter=adapter)
    return pipeline, llm


def test_the_suffix_is_appended_to_that_roles_system_prompt(tmp_path):
    suffix = tmp_path / "outside" / "discipline.md"
    suffix.parent.mkdir()
    suffix.write_text(SECRET_LINE, encoding="utf-8")
    _, llm = run_once(tmp_path, config_with_suffix(tmp_path, suffix))

    recon_system = next(c for c in llm.calls if c["role"] == "recon")["messages"][0]
    assert recon_system["role"] == "system"
    assert recon_system["content"].endswith(SECRET_LINE)
    # Compared against whatever the recon prompt is, so writing it in the recon
    # lab does not break this test.
    assert recon_system["content"].startswith(recon.SYSTEM_PROMPT), "appended, not replaced"
    triage_system = next(c for c in llm.calls if c["role"] == "triage")["messages"][0]
    assert SECRET_LINE not in triage_system["content"]


def test_the_trace_has_the_hash_and_basename_and_never_the_text(tmp_path):
    suffix = tmp_path / "discipline.md"
    suffix.write_text(SECRET_LINE, encoding="utf-8")
    pipeline, _ = run_once(tmp_path, config_with_suffix(tmp_path, suffix))

    on_disk = (tmp_path / "run" / "trace.jsonl").read_text(encoding="utf-8")
    in_memory = json.dumps(pipeline.trace.events)
    for blob in (on_disk, in_memory):
        assert "ZEBRA-7731" not in blob
        assert str(suffix) not in blob, "the suffix's full path leaked"

    start = next(e for e in pipeline.trace.of_kind("role_start") if e["role"] == "recon")
    assert start["prompt_suffix"] == {
        "file": "discipline.md",
        "sha256": hashlib.sha256(SECRET_LINE.encode()).hexdigest(),
    }
    recon_cfg = pipeline.trace.of_kind("run_start")[0]["config"]["roles"]["recon"]
    assert "system_prompt_suffix" not in recon_cfg
    assert recon_cfg["system_prompt_suffix_sha256"] == start["prompt_suffix"]["sha256"]


def test_tilde_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "s.md").write_text("x", encoding="utf-8")
    config = load_config(roles_path=config_with_suffix(tmp_path, "~/s.md"))
    assert config.role("recon").system_prompt_suffix == "x"


def test_a_missing_suffix_file_fails_fast(tmp_path):
    roles_path = config_with_suffix(tmp_path, tmp_path / "nope.md")
    with pytest.raises(FileNotFoundError, match="nope.md"):
        load_config(roles_path=roles_path)


def test_main_refuses_to_start_with_a_missing_suffix(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "placeholder")
    code = main(["--task", "t", "--roles", str(config_with_suffix(tmp_path, tmp_path / "nope.md")),
                 "--run-dir", str(tmp_path / "run"), "--non-interactive"])
    assert code == 2
    assert "system_prompt_suffix_file" in capsys.readouterr().err


def test_off_by_default(tmp_path):
    config = load_config(roles_path=write_config(tmp_path))
    assert all(r.system_prompt_suffix is None for r in config.roles.values())
