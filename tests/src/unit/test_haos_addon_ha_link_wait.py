"""Unit tests for ``haos_runtime.wait_for_addon_ha_link_ready``.

The inaddon lane used to gate setup on ``wait_for_addon_mcp_ready`` alone,
which proves the addon's own HTTP listener is up. Tools depend on a different
layer: the addon reaches Core over the Supervisor WebSocket proxy, and that
proxy answers HTTP 502 for a beat after boot. Setup therefore returned while
the addon was half-ready and the session's first HA-backed tool call could
fail with ``CONNECTION_FAILED`` (observed on #1997:
``test_web_ui_debug_log_level_reaches_addon_log`` died on its first
``ha_get_addon`` while the same job's main suite passed 1037 tests).

These drive the probe through a stub, so they need no booted HAOS. The clock
is faked so the poll loop is deterministic and instant.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tests.src.haos_runtime import (
    _ADDON_HA_LINK_POLL_S,
    _addon_link_transient_errors,
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
    assert clock.now >= 10.0


def test_each_attempt_is_bounded_by_the_remaining_deadline() -> None:
    """The probe is handed the time left, not an unbounded wait.

    Without a per-attempt cap the call inherits FastMCP's Streamable HTTP
    default (``httpx.Timeout(30.0, read=300.0)``), so one stalled attempt can
    run 300s and blow through this helper's own budget.
    """
    budgets: list[float] = []

    def probe(url: str, budget: float) -> None:
        budgets.append(budget)
        raise OSError("connection refused")

    _, _clock = _run(probe, timeout=10.0)
    assert budgets, "probe was never called"
    # First attempt gets the whole budget, and each later one strictly less.
    assert budgets[0] == pytest.approx(10.0)
    assert budgets == sorted(budgets, reverse=True)
    assert all(0 < b <= 10.0 for b in budgets), budgets


def test_a_stalled_attempt_cannot_outlive_the_deadline() -> None:
    """A probe that burns its whole budget still stops at the deadline.

    Regression for the unbounded probe: the loop only consulted the deadline
    between attempts, so an attempt that hung held the fixture (and its HAOS
    diagnostics) well past the advertised timeout.
    """
    attempts: list[float] = []
    clock = _Clock()

    def stalling_probe(url: str, budget: float) -> None:
        # What asyncio.wait_for does when the call never answers: consume the
        # budget, then raise a TimeoutError (a retried transient class).
        attempts.append(budget)
        clock.sleep(budget)
        raise TimeoutError("probe timed out")

    ready, clock = _run(stalling_probe, timeout=10.0, clock=clock)
    assert ready is False
    # One stall eats the budget, so the helper returns at the deadline rather
    # than at 300s per attempt.
    assert clock.now == pytest.approx(10.0)
    assert len(attempts) == 1


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
