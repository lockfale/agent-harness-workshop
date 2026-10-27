"""One OpenAI-compatible chat-completions client for every role.

The harness never rewrites a model name. `or/vendor/model`, `local/my-model`
and a bare OpenRouter slug all go out exactly as `roles.yaml` spells them, and
the gateway decides where they land. That is what lets an attendee with two
GPUs and an attendee with an API key run identical code.

Two things are always recorded: the stop reason and the usage. A run summarised
without them is a guess wearing a number.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any, Protocol

from .pricing import Usage

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# OpenAI-compatible finish reasons, mapped onto one vocabulary so the trace,
# trace_stats and the budget breach message read the same whichever provider
# answered. The raw value is kept alongside (`raw_stop_reason`).
STOP_REASONS: dict[str, str] = {
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "tool_use": "tool_use",
    "stop": "end_turn",
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "length": "max_tokens",
    "max_tokens": "max_tokens",
    "content_filter": "content_filter",
    "refusal": "content_filter",
    "error": "error",
}


def normalise_stop_reason(raw: str | None) -> str | None:
    """`tool_calls` -> `tool_use`, `stop` -> `end_turn`, `length` -> `max_tokens`.

    Unknown values pass through unchanged rather than being guessed at.
    """
    if raw is None:
        return None
    return STOP_REASONS.get(str(raw).lower(), str(raw))


class ProviderError(RuntimeError):
    """The provider answered, but not with a completion.

    Some gateways return HTTP 200 with an `error` body and no `choices`. Read
    naively that is an empty assistant turn: the role gets nudged, the turn is
    billed again, and the role ends `incomplete` as if the model had gone
    quiet. It is an environment failure, and raising makes it one.
    """


@dataclasses.dataclass
class LLMResponse:
    """One assistant turn, normalised."""

    model: str
    content: str | None
    tool_calls: list[dict[str, Any]]
    stop_reason: str | None
    usage: Usage
    raw: Any = None
    # What the provider actually said, before normalise_stop_reason().
    raw_stop_reason: str | None = None
    # Reasoning the provider returned with this turn, if any. OpenRouter puts
    # a summary in `reasoning` and the replayable blocks in `reasoning_details`.
    reasoning: str | None = None
    reasoning_details: list[Any] | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def as_message(self, echo_reasoning: bool = True) -> dict[str, Any]:
        """The assistant message to append to the conversation.

        With reasoning enabled, some providers (Anthropic models among them)
        expect the previous turn's reasoning blocks back on the next turn of a
        tool loop, and reject the request without them. `reasoning_details` is
        echoed unmodified when present. Set `echo_reasoning: false` on a role
        whose provider rejects the field instead.
        """
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        if echo_reasoning:
            if self.reasoning_details:
                message["reasoning_details"] = self.reasoning_details
            elif self.reasoning:
                message["reasoning"] = self.reasoning
        return message


class ChatClient(Protocol):
    """What a role runner needs. The tests supply a fake with this shape."""

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        role: str | None = None,
        turn: int | None = None,
    ) -> LLMResponse: ...


def _as_dict(value: Any) -> dict[str, Any]:
    """Normalise an SDK object or a plain dict into a dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    for attr in ("model_dump", "to_dict", "dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return method()
            except TypeError:
                continue
    instance = {k: v for k, v in vars(value).items() if not k.startswith("_")}
    if instance:
        return instance
    # Last resort, for an object whose fields are class attributes.
    return {
        name: getattr(value, name)
        for name in dir(value)
        if not name.startswith("_") and not callable(getattr(value, name, None))
    }


def parse_usage(raw: Any) -> Usage:
    """Pull usage out of an OpenAI-compatible response.

    Providers disagree about the details. OpenRouter reports cached prompt
    tokens under `prompt_tokens_details.cached_tokens`, reasoning tokens under
    `completion_tokens_details.reasoning_tokens`, and some upstreams report
    neither. Missing means zero, never missing means absent.
    """
    usage = _as_dict(raw)
    if not usage:
        return Usage()
    prompt_details = _as_dict(usage.get("prompt_tokens_details"))
    completion_details = _as_dict(usage.get("completion_tokens_details"))
    return Usage(
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        ),
        cache_read_tokens=int(
            prompt_details.get("cached_tokens")
            or usage.get("cache_read_input_tokens")
            or 0
        ),
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
    )


def parse_completion(raw: Any, model: str) -> LLMResponse:
    """Normalise one chat-completions response.

    Raises ProviderError for a response that carries an error or no choices.
    """
    data = _as_dict(raw)
    error = data.get("error")
    choices = data.get("choices") or []
    if error or not choices:
        detail = error.get("message") if isinstance(error, dict) else error
        raise ProviderError(
            "the provider returned no completion"
            + (f": {detail or error}" if error else " (no choices in the response)")
        )
    choice = _as_dict(choices[0])
    message = _as_dict(choice.get("message"))
    tool_calls = [_as_dict(call) for call in (message.get("tool_calls") or [])]
    raw_stop = choice.get("finish_reason") or choice.get("stop_reason")
    details = message.get("reasoning_details")
    return LLMResponse(
        model=str(data.get("model") or model),
        content=message.get("content"),
        tool_calls=tool_calls,
        stop_reason=normalise_stop_reason(raw_stop),
        usage=parse_usage(data.get("usage")),
        raw=raw,
        raw_stop_reason=raw_stop,
        reasoning=message.get("reasoning") or message.get("reasoning_content"),
        reasoning_details=[_as_dict(d) for d in details] if details else None,
    )


class LLMClient:
    """Thin wrapper over the OpenAI SDK pointed at any compatible endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        client: Any = None,
        timeout: float = 600.0,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url or os.environ.get("HARNESS_BASE_URL") or DEFAULT_BASE_URL
        self._api_key = api_key or os.environ.get("HARNESS_API_KEY")
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = client

    @property
    def client(self) -> Any:
        """The underlying SDK client, built on first use.

        Imported late so the rest of the harness, and the whole test suite,
        runs without the SDK installed.
        """
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - environment problem
                raise RuntimeError(
                    "the `openai` package is required to call a model. "
                    "pip install -r harness/requirements.txt"
                ) from exc
            if not self._api_key:
                raise RuntimeError(
                    "HARNESS_API_KEY is not set. Export your OpenRouter key, or point "
                    "HARNESS_BASE_URL at a local gateway and set any placeholder key."
                )
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=self._api_key,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
        return self._client

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        role: str | None = None,
        turn: int | None = None,
    ) -> LLMResponse:
        """One chat completion. `role` and `turn` are for the caller's trace."""
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if max_output_tokens is not None:
            kwargs["max_tokens"] = max_output_tokens
        if reasoning_effort:
            # Over-deliberation should be a dial you can turn, not a surprise
            # you discover in a bill. Gateways that do not understand the field
            # ignore it.
            kwargs["reasoning_effort"] = reasoning_effort
        raw = self.client.chat.completions.create(**kwargs)
        return parse_completion(raw, model)
