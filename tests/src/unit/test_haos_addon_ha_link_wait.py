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


def _run(probe: Any, *, timeout: float = 10.0) -> tuple[bool, _Clock]:
    clock = _Clock()
    with (
        patch("tests.src.haos_runtime._probe_addon_ha_link", probe),
        patch("tests.src.haos_runtime.time", clock),
    ):
        return wait_for_addon_ha_link_ready(_URL, timeout=timeout), clock


def test_returns_immediately_when_link_is_up() -> None:
    """A link that already works costs one probe and no sleep."""
    calls: list[str] = []
    ready, clock = _run(lambda url: calls.append(url))
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

    def probe(url: str) -> None:
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

    def probe(url: str) -> None:
        attempts.append(len(attempts))
        raise OSError("connection refused")

    ready, clock = _run(probe, timeout=10.0)
    assert ready is False
    # timeout 10s / poll 3s: probes at t=0,3,6,9 then the deadline stops it.
    assert len(attempts) == 4
    assert clock.now >= 10.0


def test_bugs_propagate_instead_of_being_retried() -> None:
    """A bug in the probe must fail loudly, not burn the whole budget.

    Mirrors .gemini/styleguide.md's polling-loop rule: only genuinely
    transient classes are retried.
    """

    def probe(url: str) -> None:
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
