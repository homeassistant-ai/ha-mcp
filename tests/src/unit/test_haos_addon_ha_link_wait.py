"""Unit tests for ``haos_runtime.wait_for_addon_ha_link_ready``.

The inaddon lane used to gate setup on ``wait_for_addon_mcp_ready`` alone,
which proves the addon's own HTTP listener is up. Tools depend on a different
layer: the addon reaches Core over the Supervisor WebSocket proxy, and that
proxy answers HTTP 502 for a beat after boot. Setup therefore returned while
the addon was half-ready and the session's first HA-backed tool call could
fail with ``CONNECTION_FAILED`` (observed on #1997:
``test_web_ui_debug_log_level_reaches_addon_log`` died on its first
``ha_get_app`` while the same job's main suite passed 1037 tests).

These drive the probe through a stub, so they need no booted HAOS. The clock
is faked so the poll loop is deterministic and instant.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tests.src.haos_runtime import (
    _ADDON_HA_LINK_POLL_S,
    _ADDON_HA_LINK_PROBE_S,
    _addon_link_transient_errors,
    _is_transient_link_error,
    _probe_addon_ha_link,
    wait_for_addon_ha_link_ready,
)

_URL = "http://127.0.0.1:9583/mcp_e2e_test_path"


class _Clock:
    """Stand-in for the ``time`` module that only advances when slept on.

    Substituted for ``haos_runtime.time`` rather than patching attributes on
    the stdlib module, so a faked clock cannot leak into pytest's own timing.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _run(
    probe: Any, *, timeout: float = 10.0, clock: _Clock | None = None
) -> tuple[bool, _Clock]:
    """Drive the helper with a stubbed probe on a faked clock.

    Pass ``clock`` when the probe itself needs to advance time (e.g. to
    simulate an attempt that consumes its whole budget).
    """
    clock = clock or _Clock()
    with (
        patch("tests.src.haos_runtime._probe_addon_ha_link", probe),
        patch("tests.src.haos_runtime.time", clock),
    ):
        return wait_for_addon_ha_link_ready(_URL, timeout=timeout), clock


def test_returns_immediately_when_link_is_up() -> None:
    """A link that already works costs one probe and no sleep."""
    calls: list[str] = []
    ready, clock = _run(lambda url, budget: calls.append(url))
    assert ready is True
    assert calls == [_URL]
    assert clock.now == 0.0


def test_retries_until_the_link_comes_up() -> None:
    """The 502 window is ridden out rather than surfaced to the first test.

    This is the regression: before the gate existed, the very first HA-backed
    call took this failure and the test carrying it had no reason to retry.
    """
    from fastmcp.exceptions import ToolError

    attempts: list[int] = []

    def probe(url: str, budget: float) -> None:
        attempts.append(len(attempts))
        if len(attempts) < 3:
            # Shape of the observed failure: the tool call raises with the
            # Supervisor proxy's 502 wrapped in a structured CONNECTION_FAILED.
            raise ToolError(
                '{"error": {"code": "CONNECTION_FAILED", "message": '
                '"server rejected WebSocket connection: HTTP 502"}}'
            )

    ready, clock = _run(probe)
    assert ready is True
    assert len(attempts) == 3
    # Slept between attempts — a paced loop, not a tight spin.
    assert clock.now == pytest.approx(2 * _ADDON_HA_LINK_POLL_S)


def test_gives_up_at_the_deadline() -> None:
    """A link that never comes up returns False for the caller to report."""
    attempts: list[int] = []

    def probe(url: str, budget: float) -> None:
        attempts.append(len(attempts))
        raise OSError("connection refused")

    ready, clock = _run(probe, timeout=10.0)
    assert ready is False
    # timeout 10s / poll 3s: probes at t=0,3,6,9 then the deadline stops it.
    assert len(attempts) == 4
    # Exactly the budget: the final sleep is clamped to the deadline rather
    # than overshooting to 12.0.
    assert clock.now == pytest.approx(10.0)


def test_each_attempt_is_bounded_by_the_remaining_deadline() -> None:
    """The probe is handed the time left, not an unbounded wait.

    Without a per-attempt bound the call inherits FastMCP's Streamable HTTP
    read default and can outlive this helper's own budget — see
    ``_probe_addon_ha_link`` for the default it would otherwise inherit.
    """
    budgets: list[float] = []

    def probe(url: str, budget: float) -> None:
        budgets.append(budget)
        raise OSError("connection refused")

    _run(probe, timeout=10.0)
    # Deterministic with timeout=10 and poll=3: probes at t=0,3,6,9, each handed
    # exactly the time then left. Asserting the sequence (not just "descending")
    # is what distinguishes `remaining` from a fixed per-attempt constant.
    assert budgets == [
        pytest.approx(10.0),
        pytest.approx(7.0),
        pytest.approx(4.0),
        pytest.approx(1.0),
    ], budgets


def test_a_stalled_attempt_is_abandoned_and_retried() -> None:
    """A stall costs one attempt, not the whole window.

    Regression for two shapes: the loop used to consult the deadline only
    between attempts (so an unbounded stall ran past the advertised timeout),
    and handing each attempt all the remaining time made the first stall
    monopolize the budget with zero retries — turning the transient this
    helper exists for into a hard setup failure.
    """
    budgets: list[float] = []
    clock = _Clock()

    def stalling_probe(url: str, budget: float) -> None:
        # What asyncio.wait_for does when the call never answers: consume the
        # budget, then raise a TimeoutError (a retried transient class).
        budgets.append(budget)
        clock.sleep(budget)
        raise TimeoutError("probe timed out")

    ready, clock = _run(stalling_probe, timeout=100.0, clock=clock)
    assert ready is False
    # Each attempt capped at _ADDON_HA_LINK_PROBE_S, so the window is retried
    # rather than spent on one call.
    assert len(budgets) > 1
    assert max(budgets) == pytest.approx(_ADDON_HA_LINK_PROBE_S)
    assert clock.now == pytest.approx(100.0)


def test_a_stall_that_clears_still_succeeds() -> None:
    """A stall followed by a healthy answer returns True.

    The point of capping each attempt: the retry after an abandoned stall is
    what makes this a poll loop rather than a one-shot.
    """
    clock = _Clock()
    calls: list[float] = []

    def probe(url: str, budget: float) -> None:
        calls.append(budget)
        if len(calls) == 1:
            clock.sleep(budget)
            raise TimeoutError("first attempt stalled")

    ready, clock = _run(probe, timeout=100.0, clock=clock)
    assert ready is True
    assert len(calls) == 2


def test_bugs_propagate_instead_of_being_retried() -> None:
    """A bug in the probe must fail loudly, not burn the whole budget.

    Mirrors .gemini/styleguide.md's polling-loop rule: only genuinely
    transient classes are retried.
    """

    def probe(url: str, budget: float) -> None:
        raise TypeError("probe called with the wrong shape")

    with pytest.raises(TypeError):
        _run(probe)


def test_transient_set_covers_the_canonical_polling_errors() -> None:
    """The copied set must not drift from the suite's canonical tuple.

    ``haos_runtime`` is imported bare by the e2e tests, so it cannot import
    the e2e package to reuse the tuple directly — this pins the copy instead.
    """
    from fastmcp.exceptions import ToolError

    from tests.src.e2e.utilities.wait_helpers import _POLLING_TRANSIENT_ERRORS

    transient = _addon_link_transient_errors()
    missing = set(_POLLING_TRANSIENT_ERRORS) - set(transient)
    assert not missing, f"drifted from the canonical polling set: {missing}"
    # The failure this gate exists for arrives as a ToolError.
    assert issubclass(ToolError, transient)


def test_a_transient_exception_group_is_retried() -> None:
    """Cancelling a stalled attempt can surface a group, not a bare error.

    ``asyncio.wait_for`` cancels a live anyio task group, whose teardown groups
    that cancellation with any child failure. A group carrying a ``BaseException``
    leaf is not an ``Exception``, so before it was classified it escaped the poll
    loop as a raw anyio traceback — losing the remaining retry budget and the
    caller's diagnostics pointer, which is the very failure mode this helper
    exists to prevent.
    """
    import asyncio

    attempts: list[int] = []

    def probe(url: str, budget: float) -> None:
        attempts.append(len(attempts))
        if len(attempts) < 2:
            raise BaseExceptionGroup(
                "teardown", [asyncio.CancelledError(), TimeoutError("stalled")]
            )

    ready, _clock = _run(probe)
    assert ready is True
    assert len(attempts) == 2


def test_a_group_carrying_a_bug_propagates() -> None:
    """A group is only transient when EVERY leaf is.

    Otherwise a genuine bug that happened inside the SDK's task group would be
    relabelled "not linked yet" and retried until the deadline.
    """

    def probe(url: str, budget: float) -> None:
        raise BaseExceptionGroup(
            "teardown", [TimeoutError("stalled"), TypeError("bug")]
        )

    with pytest.raises(BaseExceptionGroup):
        _run(probe)


def test_nested_group_carrying_a_bug_propagates() -> None:
    """The all-leaves rule recurses into nested groups."""

    def probe(url: str, budget: float) -> None:
        raise BaseExceptionGroup(
            "outer", [BaseExceptionGroup("inner", [TypeError("bug")])]
        )

    with pytest.raises(BaseExceptionGroup):
        _run(probe)


def test_transient_classifier_accepts_a_pure_cancellation_group() -> None:
    """A group of only our own cancellations classifies as transient."""
    import asyncio

    assert _is_transient_link_error(
        BaseExceptionGroup("teardown", [asyncio.CancelledError()])
    )
    assert not _is_transient_link_error(
        BaseExceptionGroup("teardown", [KeyError("bug")])
    )


def test_probe_honors_its_budget_against_a_silent_listener() -> None:
    """The real probe gives up on a socket that accepts and never answers.

    Every other test stubs ``_probe_addon_ha_link`` out, so nothing else
    executes the timeout wiring this module exists for: drop a ``timeout=``
    kwarg or the ``asyncio.wait_for`` wrapper and they all still pass while a
    stall silently reverts to FastMCP's ~300s read default. This binds a
    loopback socket that completes the TCP handshake and then stays silent —
    the exact shape the budget defends against — and asserts the probe raises
    a classified transient promptly.
    """
    import socket
    import threading
    import time as real_time

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    stop = threading.Event()
    held: list[socket.socket] = []

    def _accept_and_stall() -> None:
        server.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            held.append(conn)  # keep it open, never reply

    thread = threading.Thread(target=_accept_and_stall, daemon=True)
    thread.start()
    try:
        started = real_time.monotonic()
        with pytest.raises(_addon_link_transient_errors()) as excinfo:
            _probe_addon_ha_link(f"http://127.0.0.1:{port}/mcp", 2.0)
        elapsed = real_time.monotonic() - started
        assert _is_transient_link_error(excinfo.value), excinfo.value
        # Budget 2s; allow generous slack for FastMCP's shielded teardown.
        assert elapsed < 30.0, f"probe overran its budget: {elapsed:.1f}s"
    finally:
        stop.set()
        thread.join(timeout=5)
        for conn in held:
            conn.close()
        server.close()
