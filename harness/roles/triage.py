"""M3: triage. Turn observations into a ranked, defensible candidate list.

The important part of this module runs before the model does. `dedup()` is
deterministic code, and it is deliberately not a model's job: asking a model to
spot duplicates in a list of thirty observations gets you a different answer
every run, costs tokens, and silently drops things. Collapse what is provably
the same thing first, then spend model tokens on the part that actually needs
judgement, which is the ranking.

What you fill in
----------------
1. `SYSTEM_PROMPT`. Make it demand a defence of the ranking, not just an
   order. "Rank these" gets you a list. "Rank these and for each one say what
   would have to be true for it to be real, and what you would check first"
   gets you something the next role can act on.
2. The body of `rank_candidate`. Decide what a candidate looks like.
3. Optionally the `dedup_key` function, if your observation shape needs a
   different notion of "the same thing".
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Iterable

from ..status import StageStatus
from ..tools.finish import register_finish
from ..tools.registry import ToolContext, ToolRegistry, ToolResult
from .base import Pipeline, RoleOutcome, run_role

STAGE = "triage"

# TODO(M3): write this prompt.
#
# The prompt has one job the code cannot do: make the model justify the order.
# Ask for the check it would run first, and what result would kill the
# candidate. A ranking nobody has to defend drifts toward whatever sounds
# most dangerous.
SYSTEM_PROMPT = """TODO(M3): the triage role's system prompt goes here.
"""

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lowercase, collapse whitespace, drop surrounding punctuation."""
    return _WHITESPACE.sub(" ", (text or "").strip().lower()).strip(" .,:;-")


def dedup_key(observation: dict[str, Any]) -> str:
    """A stable key for "this is the same observation".

    Default: kind plus location plus the claim, normalised. Two observations
    that differ only in wording collapse. Two that point at different lines of
    the same file do not.

    TODO(M3), optional: if your recon role records something more structured,
    key on that instead. Keep it deterministic. The moment this function
    consults a model, the stage stops being reproducible.
    """
    parts = [
        normalise(str(observation.get("kind", ""))),
        normalise(str(observation.get("where", ""))),
        normalise(str(observation.get("what", ""))),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def dedup(
    observations: Iterable[dict[str, Any]],
    key: Callable[[dict[str, Any]], str] = dedup_key,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Collapse duplicates in code, before the model sees anything.

    Returns the surviving observations, each carrying a `seen` count, and a
    map of key to how many times it appeared. Nothing is discarded silently:
    the count is kept, because five independent sightings of one thing is a
    different signal from one sighting.
    """
    kept: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for observation in observations:
        identity = key(observation)
        counts[identity] = counts.get(identity, 0) + 1
        if identity in kept:
            kept[identity]["seen"] = counts[identity]
            continue
        row = dict(observation)
        row["dedup_key"] = identity
        row["seen"] = 1
        kept[identity] = row
    return list(kept.values()), counts


CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dedup_key": {
            "type": "string",
            "description": "The key of the observation this candidate comes from.",
        },
        "claim": {
            "type": "string",
            "minLength": 1,
            "description": "What you think is wrong, in one sentence.",
        },
        "rank": {
            "type": "integer",
            "minimum": 1,
            "description": "1 is the one to check first.",
        },
        "must_be_true": {
            "type": "string",
            "minLength": 1,
            "description": "What has to hold for this to be real. Be specific.",
        },
        "first_check": {
            "type": "string",
            "minLength": 1,
            "description": "The single cheapest check that would move your confidence.",
        },
        "kill_condition": {
            "type": "string",
            "minLength": 1,
            "description": "The result that would make you drop this candidate.",
        },
    },
    "required": ["dedup_key", "claim", "rank", "must_be_true", "first_check", "kill_condition"],
    "additionalProperties": False,
}


def rank_candidate(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """TODO(M3): store one ranked candidate for the refuter panel.

    Validated arguments again, so no checking needed. A minimal version:

        candidates = ctx.state.setdefault("candidates", [])
        candidates.append(dict(arguments))
        return ToolResult(content={"candidates": len(candidates)})

    Worth considering: reject a `dedup_key` that is not in
    `ctx.state["deduped_keys"]`, so the role cannot rank a candidate it
    invented. That is a gate, and gates belong in handlers.
    """
    raise NotImplementedError("TODO(M3): implement rank_candidate")


def build_registry(pipeline: Pipeline) -> ToolRegistry:
    registry = ToolRegistry(role=STAGE, trace=pipeline.trace)
    registry.register(
        name="rank_candidate",
        description=(
            "Record one ranked candidate. Call it once per candidate, in the "
            "order you want them checked."
        ),
        schema=CANDIDATE_SCHEMA,
        handler=rank_candidate,
    )
    register_finish(registry)
    return registry


def opening_messages(pipeline: Pipeline, deduped: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import json

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{len(deduped)} observations survived deduplication. Duplicates were "
                f"collapsed in code before you saw this list; the `seen` field is how "
                f"many times each one was reported.\n\n"
                f"{json.dumps(deduped, indent=2, default=str)}"
            ),
        },
    ]


def run(pipeline: Pipeline) -> StageStatus:
    observations = pipeline.state.get("observations") or []
    deduped, counts = dedup(observations)
    pipeline.state["deduped"] = deduped
    pipeline.state["deduped_keys"] = [row["dedup_key"] for row in deduped]
    pipeline.trace.note(
        "deterministic dedup ran before the model",
        stage=STAGE,
        observations_in=len(observations),
        observations_out=len(deduped),
        collapsed=sum(count - 1 for count in counts.values()),
    )

    outcome: RoleOutcome = run_role(
        role=STAGE,
        pipeline=pipeline,
        registry=build_registry(pipeline),
        messages=opening_messages(pipeline, deduped),
    )
    candidates = pipeline.state.get("candidates") or []
    return pipeline.statuses.write(
        outcome.to_status(
            STAGE,
            observations_in=len(observations),
            observations_deduped=len(deduped),
            candidates=len(candidates),
        )
    )
