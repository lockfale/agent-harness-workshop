"""P4: a side channel that throws loses its own output, not the run."""

from __future__ import annotations

import pytest

from harness.sidechannel import SideChannels, safe


def test_an_exception_in_a_notifier_is_recorded_and_swallowed(trace):
    channels = SideChannels(trace=trace)
    seen: list[str] = []

    def broken(event, payload):
        raise RuntimeError("webhook returned 500")

    def working(event, payload):
        seen.append(event)

    channels.add("broken", broken)
    channels.add("working", working)

    channels.notify("first_finding", candidate="x")

    assert seen == ["first_finding"], "a later notifier was skipped because an earlier one threw"
    errors = trace.of_kind("side_channel_error")
    assert len(errors) == 1
    assert errors[0]["channel"] == "notifier:broken"
    assert "webhook returned 500" in errors[0]["error"]


def test_a_notifier_that_raises_baseexception_is_also_contained(trace):
    """A side channel does not get to end the run, whatever it raises."""
    channels = SideChannels(trace=trace)

    def rude(event, payload):
        raise SystemExit(1)

    channels.add("rude", rude)
    channels.notify("run_end")

    assert trace.of_kind("side_channel_error")[0]["channel"] == "notifier:rude"


def test_a_broken_report_renderer_loses_the_report_only(trace):
    channels = SideChannels(trace=trace)

    def render(result):
        raise KeyError("severity")

    assert channels.render("summary", render, {"verdict": "confirmed"}) is None
    assert trace.of_kind("side_channel_error")[0]["channel"] == "report:summary"


def test_safe_returns_the_default_on_failure(trace):
    def boom():
        raise ValueError("no")

    assert safe("x", boom, trace=trace, default="fallback") == "fallback"
    assert safe("x", lambda: "fine", trace=trace) == "fine"


def test_safe_tolerates_a_broken_trace():
    """If even the trace is broken, the call still must not raise."""

    class BrokenTrace:
        def side_channel_error(self, **_):
            raise OSError("disk full")

    def boom():
        raise ValueError("no")

    assert safe("x", boom, trace=BrokenTrace()) is None


def test_no_notifiers_is_not_an_error(trace):
    SideChannels(trace=trace).notify("anything")
    assert trace.of_kind("side_channel_error") == []


@pytest.mark.parametrize("exc", [RuntimeError("x"), KeyError("k"), TypeError("t")])
def test_every_exception_type_is_contained(trace, exc):
    channels = SideChannels(trace=trace)
    channels.add("n", lambda event, payload: (_ for _ in ()).throw(exc))
    channels.notify("e")
    assert len(trace.of_kind("side_channel_error")) == 1
