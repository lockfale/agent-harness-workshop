"""The verdict gate. The model proposes, this module disposes.

Every rule in here was a prompt instruction first, and every one of them was
ignored. Not maliciously: a model asked to be careful about privilege
boundaries in paragraph nine of a system prompt is being asked to remember,
and it is answering a different question at the time. Moving the rule into
code changes it from a request into a precondition, and a rejection is a tool
error the model has to answer rather than advice it can skim.

Six rules, in order:

1. Infrastructure origin. A build, harness, environment or timeout failure
   means the judge is never invoked at all. If one is invoked anyway it may
   not return `refuted`, because "the exploit failed" and "the exploit never
   ran" are different claims and only the second one is true.
2. Expectations contract. `confirmed` needs a contract that existed before
   the exploit ran. Written afterwards, it is a description of what happened,
   not a prediction, and it cannot be wrong.
3. Negative control. `confirmed` needs a run on benign input that did not
   trigger. Without it you have shown that something happens, not that the
   input causes it.
4. Tier caps severity. Severity comes from `proof_tier` by a fixed table. The
   model does not get to set it. Tiers ratchet up across reruns, never down.
5. Assumed access. A `confirmed` finding that crossed no privilege boundary
   and needs privileged access to work has not shown an attacker can do
   anything they could not already do.
6. Synthetic precondition. If the harness built the triggering state itself,
   the tier stops at `exploitable`. The harness asserted the premise, so it
   cannot also be the thing that confirms the impact.

Rules 5 and 6 decide on structured fields the model must fill in
(`required_privileges`, `privilege_boundary_crossed`, `trigger_origin`), not
on keywords in prose. Keyword matching on the free-text fields still runs, but
only as a warning in the trace: an `/admin` endpoint or an "api key" in a CTF
precondition is normal, and a substring list that rejects on it rejects
legitimate confirms, while a single negated phrase elsewhere would silence it.
A warning says "the prose and the fields disagree, look here" without pretending
a word list can decide.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

VERDICTS = ("confirmed", "unconfirmed", "refuted")

PROOF_TIERS = (
    "rejected",
    "inconclusive",
    "source",
    "reachable",
    "exploitable",
    "impact_confirmed",
)

TIER_RANK = {tier: index for index, tier in enumerate(PROOF_TIERS)}

PRIVILEGE_LEVELS = ("none", "unauthenticated_network", "authenticated_user", "privileged")

TRIGGER_ORIGINS = ("real_code_path", "harness_constructed", "unknown")

# Rule 4. Fixed, and the model never sees a severity field to fill in.
SEVERITY_BY_TIER = {
    "rejected": "none",
    "inconclusive": "none",
    "source": "informational",
    "reachable": "low",
    "exploitable": "medium",
    "impact_confirmed": "high",
}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "proof_tier": {"type": "string", "enum": list(PROOF_TIERS)},
        "autonomy": {
            "type": "integer",
            "minimum": 0,
            "maximum": 4,
            "description": (
                "0 fully autonomous, 4 research only. Source-code vuln hunting "
                "belongs at 0 or 1. Live operations belong at 2 or 3, and that is "
                "the correct answer rather than a shortcoming."
            ),
        },
        "precondition": {
            "type": "string",
            "minLength": 1,
            "description": "What must already be true for this to work.",
        },
        "required_privileges": {
            "type": "string",
            "enum": list(PRIVILEGE_LEVELS),
            "description": (
                "Access the attacker must already hold before this works. "
                "none: nothing. unauthenticated_network: can reach the service, "
                "no account. authenticated_user: an ordinary account or session. "
                "privileged: admin, root, or equivalent. This field decides rule 5."
            ),
        },
        "required_privileges_detail": {
            "type": "string",
            "description": "Optional. The specifics, in prose.",
        },
        "privilege_boundary_crossed": {
            "type": "boolean",
            "description": "Did this get the attacker access they did not already have?",
        },
        "trust_boundary": {
            "type": "string",
            "minLength": 1,
            "description": "What is supposed to stop the attacker here.",
        },
        "trigger_origin": {
            "type": "string",
            "enum": list(TRIGGER_ORIGINS),
            "description": (
                "real_code_path: the target's own code reached the triggering "
                "state from attacker input. harness_constructed: the harness or a "
                "driver set the state up directly. unknown: you cannot tell. "
                "Anything but real_code_path caps the tier at exploitable. This "
                "field decides rule 6."
            ),
        },
        "reachability_note": {
            "type": "string",
            "minLength": 1,
            "description": "How the triggering state was reached, in prose.",
        },
        "reasoning": {"type": "string", "minLength": 1},
    },
    "required": [
        "verdict",
        "proof_tier",
        "autonomy",
        "precondition",
        "required_privileges",
        "privilege_boundary_crossed",
        "trust_boundary",
        "trigger_origin",
        "reachability_note",
        "reasoning",
    ],
    "additionalProperties": False,
}

# Rules 5 and 6 warn on these; they never reject. A crude list on purpose: it
# is auditable and easy to extend, and a warning that fires wrongly costs a
# glance at the trace, not a rejected verdict.
PRIVILEGED_ACCESS_MARKERS: tuple[str, ...] = (
    "root",
    "sudo",
    "superuser",
    "administrator",
    "admin",
    "privileged",
    "elevated",
    "system account",
    "service account",
    "kernel",
    "physical access",
    "local access",
    "shell access",
    "already authenticated",
    "logged in as",
    "valid credentials",
    "api key",
    "ssh key",
    "same host",
    "same container",
    "trusted network",
)

SYNTHETIC_MARKERS: tuple[str, ...] = (
    "harness constructed",
    "harness-constructed",
    "constructed by the harness",
    "harness built",
    "harness-built",
    "built by the harness",
    "hand-built",
    "hand built",
    "handcrafted",
    "hand-crafted",
    "synthetic",
    "synthesised",
    "synthesized",
    "we created the state",
    "test harness creates",
    "driver creates",
    "fabricated",
    "not reachable from production",
    "no production path",
)

_NEGATED_PRIVILEGE = re.compile(
    r"\b(no|none|without|not)\b[^.;]{0,30}\b(" + "|".join(
        re.escape(m) for m in ("root", "sudo", "admin", "privileged", "elevated", "credentials")
    ) + r")\b",
    re.IGNORECASE,
)


class VerdictRejected(Exception):
    """A verdict the gate will not accept. The model must call again."""

    def __init__(self, rule: str, message: str) -> None:
        self.rule = rule
        self.message = message
        super().__init__(f"[{rule}] {message}")

    def for_model(self) -> dict[str, str]:
        return {"error": self.message, "rule": self.rule}


@dataclasses.dataclass
class Evidence:
    """What the gate knows independently of what the model says.

    The model cannot set any of this. That is the point: the fields the gate
    checks against are the ones the harness observed.
    """

    contract_path: Path | None = None
    contract_written_at: float | None = None
    exploit_started_at: float | None = None
    control_ran: bool = False
    control_clean: bool = False
    control_detail: str = ""
    previous_tier: str | None = None
    # (stage, outcome) for the earliest infrastructure failure, if there was one.
    infrastructure_failure: tuple[str, str] | None = None

    @property
    def contract_exists(self) -> bool:
        return self.contract_path is not None and Path(self.contract_path).exists()

    @property
    def contract_predates_exploit(self) -> bool:
        if self.contract_written_at is None or self.exploit_started_at is None:
            return False
        return self.contract_written_at <= self.exploit_started_at


@dataclasses.dataclass
class GatedVerdict:
    """A verdict that passed the gate, with what the gate changed and why."""

    verdict: str
    proof_tier: str
    severity: str
    autonomy: int
    precondition: str
    required_privileges: str
    privilege_boundary_crossed: bool
    trust_boundary: str
    reachability_note: str
    reasoning: str
    trigger_origin: str = "unknown"
    required_privileges_detail: str = ""
    proposed_tier: str = ""
    adjustments: list[str] = dataclasses.field(default_factory=list)
    # Prose that disagrees with the structured fields. Traced, never rejected.
    warnings: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def mentions_privileged_access(*texts: str) -> str | None:
    """The first privileged-access marker found, or None.

    A phrase that negates the marker ("no root required") does not count.
    """
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        if _NEGATED_PRIVILEGE.search(lowered):
            continue
        for marker in PRIVILEGED_ACCESS_MARKERS:
            if marker in lowered:
                return marker
    return None


def mentions_synthetic_state(text: str) -> str | None:
    """The first synthetic-precondition marker found, or None."""
    lowered = (text or "").lower()
    for marker in SYNTHETIC_MARKERS:
        if marker in lowered:
            return marker
    return None


def higher_tier(left: str | None, right: str | None) -> str | None:
    """The higher of two tiers by rank. Used for the rerun ratchet."""
    if left is None:
        return right
    if right is None:
        return left
    return left if TIER_RANK.get(left, -1) >= TIER_RANK.get(right, -1) else right


def cap_tier(tier: str, ceiling: str) -> str:
    """`tier`, lowered to `ceiling` if it is above it."""
    if TIER_RANK.get(tier, 0) > TIER_RANK.get(ceiling, 0):
        return ceiling
    return tier


def check_verdict(proposed: dict[str, Any], evidence: Evidence) -> GatedVerdict:
    """Run all six rules. Returns the gated verdict or raises VerdictRejected.

    Order is load bearing. The ratchet runs before the synthetic cap, so a
    synthetic premise lowers the tier even when an earlier run claimed higher:
    rule 6 says never `impact_confirmed` on an invented premise, and that
    outranks "tiers only go up".
    """
    verdict = proposed["verdict"]
    tier = proposed["proof_tier"]
    adjustments: list[str] = []

    # --- rule 1: infrastructure origin ------------------------------------
    if evidence.infrastructure_failure is not None:
        stage, outcome = evidence.infrastructure_failure
        if verdict == "refuted":
            raise VerdictRejected(
                "infrastructure_origin",
                f"stage {stage!r} ended with {outcome!r}, which is an infrastructure "
                f"failure. You cannot return 'refuted': nothing was tested. Return "
                f"'unconfirmed'.",
            )
        if verdict == "confirmed":
            raise VerdictRejected(
                "infrastructure_origin",
                f"stage {stage!r} ended with {outcome!r}. Nothing ran cleanly enough "
                f"to confirm anything. Return 'unconfirmed'.",
            )

    # --- rule 2: expectations contract ------------------------------------
    if verdict == "confirmed":
        if not evidence.contract_exists:
            raise VerdictRejected(
                "expectations_contract",
                "there is no expectations contract for this run. A 'confirmed' "
                "verdict needs one written before the exploit ran, stating what "
                "you expected to observe. Without it there is nothing the result "
                "could have contradicted.",
            )
        if not evidence.contract_predates_exploit:
            raise VerdictRejected(
                "expectations_contract",
                "the expectations contract was written after the exploit started "
                f"(contract {evidence.contract_written_at}, exploit "
                f"{evidence.exploit_started_at}). A prediction written afterwards "
                "is a description and cannot be wrong. Reject 'confirmed'.",
            )

    # --- rule 3: negative control -----------------------------------------
    if verdict == "confirmed":
        if not evidence.control_ran:
            raise VerdictRejected(
                "negative_control",
                "no negative control was run. Run the same path on benign input "
                "and show it does not trigger, then come back. Otherwise you have "
                "shown that something happens, not that your input causes it.",
            )
        if not evidence.control_clean:
            detail = evidence.control_detail or "the control also triggered"
            raise VerdictRejected(
                "negative_control",
                f"the negative control did not come back clean: {detail}. Benign "
                f"input triggering the same behaviour means the trigger is not your "
                f"input.",
            )

    # --- rule 5: assumed access -------------------------------------------
    # Checked before the tier arithmetic, because it can reject outright. The
    # structured fields decide; prose only warns.
    warnings: list[str] = []
    crossed = bool(proposed["privilege_boundary_crossed"])
    privileges = proposed["required_privileges"]
    detail = str(proposed.get("required_privileges_detail") or "")
    if verdict == "confirmed" and not crossed and privileges == "privileged":
        raise VerdictRejected(
            "assumed_access",
            "you set required_privileges=privileged and privilege_boundary_crossed="
            "false. An attacker who already holds privileged access has gained "
            "nothing here. Either downgrade the verdict, or name the boundary you "
            "crossed and how you crossed it and set privilege_boundary_crossed=true.",
        )
    if verdict == "confirmed" and not crossed and privileges != "privileged":
        marker = mentions_privileged_access(detail, proposed["precondition"])
        if marker:
            warnings.append(
                f"assumed_access: required_privileges={privileges!r}, but the prose "
                f"mentions {marker!r}. Check that the field is right."
            )

    # --- rule 4: ratchet, then the fixed severity table -------------------
    proposed_tier = tier
    ratcheted = higher_tier(evidence.previous_tier, tier)
    if ratcheted != tier:
        adjustments.append(
            f"tier raised from {tier!r} to {ratcheted!r}: a previous run already "
            f"reached {evidence.previous_tier!r} and tiers do not move down"
        )
        tier = ratcheted or tier

    # --- rule 6: synthetic precondition -----------------------------------
    origin = proposed["trigger_origin"]
    if origin != "real_code_path":
        capped = cap_tier(tier, "exploitable")
        if capped != tier:
            adjustments.append(
                f"tier capped at 'exploitable' (was {tier!r}): trigger_origin is "
                f"{origin!r}. If the harness asserted the premise, or nobody can say "
                f"whether real code reaches it, the run cannot also confirm the "
                f"impact. Whether production code reaches this state is a separate "
                f"question and this run did not answer it."
            )
            tier = capped
    else:
        marker = mentions_synthetic_state(proposed["reachability_note"])
        if marker:
            warnings.append(
                f"synthetic_precondition: trigger_origin='real_code_path', but the "
                f"reachability note mentions {marker!r}. Check that the field is right."
            )

    return GatedVerdict(
        verdict=verdict,
        proof_tier=tier,
        severity=SEVERITY_BY_TIER[tier],
        autonomy=int(proposed["autonomy"]),
        precondition=proposed["precondition"],
        required_privileges=proposed["required_privileges"],
        privilege_boundary_crossed=bool(proposed["privilege_boundary_crossed"]),
        trust_boundary=proposed["trust_boundary"],
        reachability_note=proposed["reachability_note"],
        reasoning=proposed["reasoning"],
        trigger_origin=origin,
        required_privileges_detail=detail,
        proposed_tier=proposed_tier,
        adjustments=adjustments,
        warnings=warnings,
    )
