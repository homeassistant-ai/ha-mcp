"""Unit tests for ``build_image._wait_supervisor_ready``.

HAOS pins only the OS version; the bundled Supervisor self-updates to the
channel head after boot. Until that finishes ``need_update`` is True and store
operations are blocked by ``JobCondition.SUPERVISOR_UPDATED`` (the first one is
``add_repository``). The helper polls ``/supervisor/info`` until
``update_available`` clears so the caller's store calls run against an
up-to-date Supervisor. These tests mock the WebSocket so they need no booted
HAOS; ``time.sleep`` is patched out so the poll loop runs instantly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch

import pytest
from websockets.exceptions import WebSocketException

from tests.haos_image_build.build_image import WSCommandError, _wait_supervisor_ready


def _info(update_available: bool, version: str = "2026.06.1") -> dict[str, Any]:
    return {
        "version": version,
        "version_latest": "2026.06.1",
        "update_available": update_available,
        "arch": "amd64",
    }


def test_returns_immediately_when_up_to_date() -> None:
    """No update pending: a single /supervisor/info read, no polling."""
    ws = Mock()
    ws.supervisor_api.return_value = _info(update_available=False)
    with patch("tests.haos_image_build.build_image.time.sleep") as sleep:
        _wait_supervisor_ready(ws)
    assert ws.supervisor_api.call_count == 1
    sleep.assert_not_called()


def test_waits_until_update_clears() -> None:
    """Polls /supervisor/info until update_available flips False."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        _info(update_available=True, version="2026.05.1"),
        _info(update_available=True, version="2026.05.1"),
        _info(update_available=False, version="2026.06.1"),
    ]
    with patch("tests.haos_image_build.build_image.time.sleep") as sleep:
        _wait_supervisor_ready(ws)
    assert ws.supervisor_api.call_count == 3
    # Slept before the 2nd and 3rd polls — proves a paced loop, not a tight spin.
    assert sleep.call_count == 2


def test_tolerates_transient_error_during_update() -> None:
    """Transient errors mid-update (Supervisor restart) keep polling."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        _info(update_available=True, version="2026.05.1"),
        WSCommandError("restart", code="system_error"),
        OSError("connection reset"),
        WebSocketException("connection closed"),
        _info(update_available=False, version="2026.06.1"),
    ]
    with patch("tests.haos_image_build.build_image.time.sleep") as sleep:
        _wait_supervisor_ready(ws)
    # Reached the 5th call + slept four times — proves it resumed past all three
    # transients (WSCommandError, OSError, WebSocketException).
    assert ws.supervisor_api.call_count == 5
    assert sleep.call_count == 4


def test_raises_on_update_timeout() -> None:
    """update_available never clears within the budget -> TimeoutError."""
    ws = Mock()
    ws.supervisor_api.return_value = _info(update_available=True, version="2026.05.1")
    # Monotonic sequence: starts at 0, advances past deadline on 3rd call
    monotonic_values = [0.0, 5.0, 15.0]
    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=monotonic_values,
        ),
        patch("tests.haos_image_build.build_image.time.sleep"),
        pytest.raises(TimeoutError),
    ):
        _wait_supervisor_ready(ws, update_timeout=10.0)
    # Should have made at least 2 calls (initial + 1 loop iteration before timeout)
    assert ws.supervisor_api.call_count >= 2


def test_persistent_error_surfaced_in_timeout() -> None:
    """Persistent WSCommandError -> timeout message includes last error."""
    ws = Mock()
    # Initial read sees a pending update; the poll then hits a persistent
    # WSCommandError until the deadline (the initial read is outside the
    # tolerant loop, so it must succeed for the loop to be exercised).
    ws.supervisor_api.side_effect = [
        _info(update_available=True, version="2026.05.1"),
        WSCommandError("supervisor unavailable", code="system_error"),
    ]
    # Monotonic sequence: deadline calc, loop-entry check, post-error check (>deadline)
    monotonic_values = [0.0, 5.0, 15.0]
    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=monotonic_values,
        ),
        patch("tests.haos_image_build.build_image.time.sleep"),
        pytest.raises(TimeoutError, match=r"last error.*WSCommandError"),
    ):
        _wait_supervisor_ready(ws, update_timeout=10.0)
    # Loop ran at least once before timing out
    assert ws.supervisor_api.call_count >= 2
