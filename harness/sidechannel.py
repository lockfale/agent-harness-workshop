"""Side channels: notifications, telemetry, report rendering.

None of these produce evidence, and none of them are allowed to end a run.

A notification helper with a bad format string once killed a seven-hour run at
the exact moment it found its first finding, because the notifier ran inside
the main loop and nothing caught it. The work was done and the result was lost.

So anything that is not the agent loop goes through `safe()`. Exceptions are
recorded in the trace as `side_channel_error` and then dropped. If your Slack
webhook is down, you find out by reading the trace, not by losing the run.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, TypeVar

T = TypeVar("T")


class Notifier(Protocol):
    """Anything that wants to hear about run events. Allowed to fail."""

    def __call__(self, event: str, payload: dict[str, Any]) -> None: ...


def safe(
    channel: str,
    fn: Callable[..., T],
    *args: Any,
    trace: Any = None,
    default: T | None = None,
    **kwargs: Any,
) -> T | None:
    """Call `fn` and swallow anything it raises.

    `BaseException` is caught rather than `Exception`, because a side channel
    that calls `sys.exit()` on a bad response code is still a side channel
    ending the run. A real interrupt from the operator is re-raised: that one
    is not the notifier's idea.
    """
    try:
        return fn(*args, **kwargs)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - deliberate, see docstring
        message = f"{type(exc).__name__}: {exc}"
        if trace is not None:
            try:
                trace.side_channel_error(channel=channel, error=message)
            except BaseException:  # noqa: BLE001 - tracing must not raise either
                pass
        return default


class SideChannels:
    """A bag of notifiers, every one of them optional and fallible."""

    def __init__(self, trace: Any = None) -> None:
        self.trace = trace
        self._notifiers: list[tuple[str, Notifier]] = []

    def add(self, name: str, notifier: Notifier) -> None:
        self._notifiers.append((name, notifier))

    def notify(self, event: str, **payload: Any) -> None:
        """Fan out one event. Every failure is recorded and then ignored."""
        for name, notifier in self._notifiers:
            safe(f"notifier:{name}", notifier, event, payload, trace=self.trace)

    def render(self, name: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T | None:
        """Render a report. A broken template loses the report, not the run."""
        return safe(f"report:{name}", fn, *args, trace=self.trace, **kwargs)
