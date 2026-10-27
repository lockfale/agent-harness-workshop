"""Config is read once per run, resolved, and echoed into the trace.

Reading config lazily inside a long-lived process is how you end up serving a
model nobody configured any more. The fix is boring: load everything at start,
resolve the defaults, write the resolved result into `run_start`, and never
look at the files again.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

from .pricing import PriceTable, Resolution

DEFAULT_ROLES_PATH = Path("config/roles.yaml")
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

PIPELINE: tuple[str, ...] = ("recon", "triage", "refute", "build", "exploit", "judge")


@dataclasses.dataclass
class RoleConfig:
    """One role's resolved settings."""

    name: str
    model: str
    max_output_tokens: int = 8192
    reasoning_effort: str | None = "medium"
    max_turns: int = 20
    budget_usd: float | None = None
    panel: list[str] = dataclasses.field(default_factory=list)
    # Send the previous turn's reasoning back to the provider. See llm.py.
    echo_reasoning: bool = True
    # Optional text appended to the role's system prompt, read once at start
    # from `system_prompt_suffix_file`. Used for prompt A/B runs. The text is
    # never written to the trace: only the file's basename and a sha256.
    system_prompt_suffix: str | None = dataclasses.field(default=None, repr=False)
    system_prompt_suffix_name: str | None = None
    system_prompt_suffix_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        body = dataclasses.asdict(self)
        body.pop("system_prompt_suffix", None)
        return body

    def suffix_for_trace(self) -> dict[str, str] | None:
        if self.system_prompt_suffix is None:
            return None
        return {
            "file": self.system_prompt_suffix_name or "",
            "sha256": self.system_prompt_suffix_sha256 or "",
        }


@dataclasses.dataclass
class HarnessConfig:
    """Everything one run needs to know, resolved and frozen."""

    roles: dict[str, RoleConfig]
    prices: PriceTable
    base_url: str
    api_key_present: bool
    roles_path: str
    prices_path: str
    run_budget_usd: float | None = None
    resolutions: list[Resolution] = dataclasses.field(default_factory=list)

    def role(self, name: str) -> RoleConfig:
        try:
            return self.roles[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.roles)) or "none"
            raise KeyError(
                f"role {name!r} is not configured in {self.roles_path}. Known roles: {known}"
            ) from exc

    def role_caps(self) -> dict[str, float]:
        return {
            name: role.budget_usd
            for name, role in self.roles.items()
            if role.budget_usd is not None
        }

    def models(self) -> list[str]:
        """Every model name this run could call, panels included."""
        names: list[str] = []
        for role in self.roles.values():
            names.append(role.model)
            names.extend(role.panel)
        return list(dict.fromkeys(names))

    def for_trace(self) -> dict[str, Any]:
        """The config echoed into `run_start`. Redaction happens in trace.py."""
        return {
            "roles_path": self.roles_path,
            "prices_path": self.prices_path,
            "prices_verified": self.prices.verified,
            "base_url": self.base_url,  # trace.redact() reduces this to a host
            "api_key_present": self.api_key_present,
            "run_budget_usd": self.run_budget_usd,
            "roles": {name: role.as_dict() for name, role in self.roles.items()},
            "resolved_prices": [r.as_dict() for r in self.resolutions],
        }


def load_config(
    roles_path: str | Path = DEFAULT_ROLES_PATH,
    prices_path: str | Path | None = None,
    run_budget_usd: float | None = None,
    env: dict[str, str] | None = None,
) -> HarnessConfig:
    """Load and resolve config, then validate every model against the prices.

    Raises `pricing.UnknownModelError` before a single token is spent if any
    configured model is unpriced.
    """
    env = dict(os.environ if env is None else env)
    roles_path = Path(roles_path)
    raw = yaml.safe_load(roles_path.read_text(encoding="utf-8")) or {}

    prices_path = Path(prices_path) if prices_path else roles_path.parent / "prices.yaml"
    prices = PriceTable.load(prices_path)

    defaults = raw.get("defaults") or {}
    roles: dict[str, RoleConfig] = {}
    for name, body in (raw.get("roles") or {}).items():
        body = body or {}
        roles[name] = RoleConfig(
            name=name,
            model=body["model"],
            max_output_tokens=int(
                body.get("max_output_tokens", defaults.get("max_output_tokens", 8192))
            ),
            reasoning_effort=body.get(
                "reasoning_effort", defaults.get("reasoning_effort", "medium")
            ),
            max_turns=int(body.get("max_turns", defaults.get("max_turns", 20))),
            budget_usd=_opt_float(body.get("budget_usd", defaults.get("budget_usd"))),
            panel=list(body.get("panel") or []),
            echo_reasoning=bool(
                body.get("echo_reasoning", defaults.get("echo_reasoning", True))
            ),
        )
        suffix_path = body.get("system_prompt_suffix_file")
        if suffix_path:
            _load_suffix(roles[name], suffix_path, roles_path)

    config = HarnessConfig(
        roles=roles,
        prices=prices,
        base_url=env.get("HARNESS_BASE_URL") or DEFAULT_BASE_URL,
        api_key_present=bool(env.get("HARNESS_API_KEY")),
        roles_path=str(roles_path),
        prices_path=str(prices_path),
        run_budget_usd=run_budget_usd,
    )
    # Fail fast, before any spending. This is the whole point of the table.
    config.resolutions = prices.validate(config.models())
    return config


def _load_suffix(role: RoleConfig, raw_path: str, roles_path: Path) -> None:
    """Read a role's system-prompt suffix once, at start, or refuse to start.

    `~` expands; a relative path is relative to roles.yaml. The path may be
    outside the repository, which is the point: a prompt you do not want in a
    public repo stays where it is.
    """
    path = Path(os.path.expanduser(str(raw_path)))
    if not path.is_absolute():
        path = roles_path.parent / path
    if not path.is_file():
        raise FileNotFoundError(
            f"role {role.name!r}: system_prompt_suffix_file {path.name!r} does not "
            f"exist (set in {roles_path}). Fix the path or remove the key; the run "
            f"will not start without it, because an A/B arm that silently ran "
            f"without its suffix is not an A/B."
        )
    data = path.read_bytes()
    role.system_prompt_suffix = data.decode("utf-8")
    role.system_prompt_suffix_name = path.name
    role.system_prompt_suffix_sha256 = hashlib.sha256(data).hexdigest()


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)
