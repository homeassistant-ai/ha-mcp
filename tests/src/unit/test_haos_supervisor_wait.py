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

from tests.haos_image_build.build_image import _wait_supervisor_ready


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
    with patch("tests.haos_image_build.build_image.time.sleep"):
        _wait_supervisor_ready(ws)
    assert ws.supervisor_api.call_count == 3


def test_tolerates_transient_error_during_update() -> None:
    """A transient WS error mid-update (Supervisor restart) keeps polling."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        _info(update_available=True, version="2026.05.1"),
        RuntimeError("supervisor restarting"),
        _info(update_available=False, version="2026.06.1"),
    ]
    with patch("tests.haos_image_build.build_image.time.sleep"):
        _wait_supervisor_ready(ws)
    assert ws.supervisor_api.call_count == 3


def test_raises_on_update_timeout() -> None:
    """update_available never clears within the budget -> TimeoutError."""
    ws = Mock()
    ws.supervisor_api.return_value = _info(update_available=True, version="2026.05.1")
    with patch("tests.haos_image_build.build_image.time.sleep"):
        with pytest.raises(TimeoutError):
            _wait_supervisor_ready(ws, update_timeout=0.0)
