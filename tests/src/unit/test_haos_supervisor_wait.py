"""Unit tests for HAOS Supervisor/Core image-variant readiness helpers.

HAOS pins only the OS version; the bundled Supervisor self-updates to the
channel head after boot. Until that finishes ``need_update`` is True and store
operations are blocked by ``JobCondition.SUPERVISOR_UPDATED``. The helpers wait
for readiness, select the requested channel, and install an exact Core version.
These tests mock the WebSocket so they need no booted HAOS; ``time.sleep`` is
patched out so polling runs instantly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, call, patch

import pytest
from websockets.exceptions import WebSocketException

from tests.haos_image_build.build_image import (
    WSCommandError,
    _configure_supervisor_image_variant,
    _wait_supervisor_ready,
)


def _info(
    update_available: bool,
    version: str = "2026.06.1",
    *,
    version_latest: str = "2026.06.1",
    channel: str = "stable",
) -> dict[str, Any]:
    return {
        "version": version,
        "version_latest": version_latest,
        "update_available": update_available,
        "arch": "amd64",
        "channel": channel,
    }


def _core_info(
    version: str,
    *,
    version_latest: str,
    update_available: bool,
) -> dict[str, Any]:
    return {
        "version": version,
        "version_latest": version_latest,
        "update_available": update_available,
        "arch": "amd64",
        "machine": "qemux86-64",
        "image": "ghcr.io/home-assistant/qemux86-64-homeassistant",
        "boot": True,
        "port": 8123,
        "ssl": False,
        "watchdog": True,
        "wait_boot": 600,
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


def test_wait_rejects_terminal_supervisor_error_without_retry() -> None:
    """A terminal Supervisor command error propagates immediately."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        _info(update_available=True, version="2026.07.5"),
        WSCommandError("invalid request", code="invalid_format"),
    ]

    with (
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
        pytest.raises(WSCommandError, match="invalid request"),
    ):
        _wait_supervisor_ready(ws)

    assert ws.supervisor_api.call_count == 2
    sleep.assert_called_once_with(10.0)
    ws.reconnect.assert_not_called()


def test_wait_requires_requested_channel() -> None:
    """A settled image on the wrong channel keeps polling."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        _info(
            update_available=False,
            version="2026.08.0",
            version_latest="2026.08.0",
            channel="stable",
        ),
        _info(
            update_available=False,
            version="2026.08.0",
            version_latest="2026.08.0",
            channel="beta",
        ),
    ]

    with patch("tests.haos_image_build.build_image.time.sleep") as sleep:
        result = _wait_supervisor_ready(
            ws,
            expected_channel="beta",
            minimum_version="2026.08.0",
        )

    assert result["channel"] == "beta"
    sleep.assert_called_once_with(10.0)


def test_wait_requires_requested_minimum_version() -> None:
    """A settled beta below the minimum version keeps polling."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        _info(
            update_available=False,
            version="2026.07.5",
            version_latest="2026.08.0",
            channel="beta",
        ),
        _info(
            update_available=False,
            version="2026.08.0",
            version_latest="2026.08.0",
            channel="beta",
        ),
    ]

    with patch("tests.haos_image_build.build_image.time.sleep") as sleep:
        result = _wait_supervisor_ready(
            ws,
            expected_channel="beta",
            minimum_version="2026.08.0",
        )

    assert result["version"] == "2026.08.0"
    sleep.assert_called_once_with(10.0)


def test_configure_variant_is_noop_when_settings_are_unset() -> None:
    """The stable image path performs no Supervisor/Core mutations."""
    ws = Mock()

    _configure_supervisor_image_variant(
        ws,
        channel=None,
        minimum_version=None,
    )

    ws.supervisor_api.assert_not_called()
    ws.reconnect.assert_not_called()


def test_configure_variant_rejects_minimum_without_channel() -> None:
    """A version floor cannot be silently ignored without a channel."""
    ws = Mock()

    with pytest.raises(ValueError, match="require a Supervisor channel"):
        _configure_supervisor_image_variant(
            ws,
            channel=None,
            minimum_version="2026.08.0",
        )

    ws.supervisor_api.assert_not_called()


def test_configure_variant_rejects_unsupported_dev_channel() -> None:
    """The versionless update flow supports stable and beta channels only."""
    ws = Mock()

    with pytest.raises(ValueError, match="Unsupported Supervisor channel"):
        _configure_supervisor_image_variant(
            ws,
            channel="dev",
            minimum_version="2026.09.0.dev1234",
        )

    ws.supervisor_api.assert_not_called()


def test_configure_beta_variant_skips_update_when_image_is_current() -> None:
    """An already-current beta image only needs options/reload verification."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        {},
        {},
        _info(
            update_available=False,
            version="2026.08.0",
            version_latest="2026.08.0",
            channel="beta",
        ),
    ]

    _configure_supervisor_image_variant(
        ws,
        channel="beta",
        minimum_version="2026.08.0",
    )

    assert ws.supervisor_api.call_args_list == [
        call(
            "/supervisor/options",
            method="post",
            data={"channel": "beta"},
            timeout=30.0,
        ),
        call("/supervisor/reload", method="post", timeout=120.0),
        call("/supervisor/info", method="get", timeout=30.0),
    ]


def test_configure_beta_variant_installs_advertised_update() -> None:
    """A newly advertised beta is installed before the qcow2 is emitted."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        {},
        {},
        _info(
            update_available=True,
            version="2026.07.5",
            version_latest="2026.08.0",
            channel="beta",
        ),
        {},
        _info(
            update_available=False,
            version="2026.08.0",
            version_latest="2026.08.0",
            channel="beta",
        ),
    ]

    _configure_supervisor_image_variant(
        ws,
        channel="beta",
        minimum_version="2026.08.0",
    )

    assert (
        call(
            "/supervisor/update",
            method="post",
            timeout=600.0,
        )
        in ws.supervisor_api.call_args_list
    )
    assert ws.supervisor_api.call_args_list[-1] == call(
        "/supervisor/info",
        method="get",
        timeout=30.0,
    )


def test_configure_beta_variant_tolerates_restart_before_version_settles() -> None:
    """A Supervisor restart-window error cannot abort a requested beta update."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        {},
        {},
        _info(
            update_available=True,
            version="2026.07.5",
            version_latest="2026.08.0",
            channel="beta",
        ),
        {},
        WSCommandError("restarting", code="system_error"),
        _info(
            update_available=False,
            version="2026.08.0",
            version_latest="2026.08.0",
            channel="beta",
        ),
    ]

    with patch("tests.haos_image_build.build_image.time.sleep"):
        _configure_supervisor_image_variant(
            ws,
            channel="beta",
            minimum_version="2026.08.0",
        )

    assert ws.supervisor_api.call_args_list[-1] == call(
        "/supervisor/info",
        method="get",
        timeout=30.0,
    )


def test_configure_beta_variant_tolerates_restart_error_from_update_call() -> None:
    """A restart error from the accepted update call falls through to polling."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        {},
        {},
        _info(
            update_available=True,
            version="2026.07.5",
            version_latest="2026.08.0",
            channel="beta",
        ),
        WSCommandError("restarting", code="unknown_error"),
        _info(
            update_available=False,
            version="2026.08.0",
            version_latest="2026.08.0",
            channel="beta",
        ),
    ]

    _configure_supervisor_image_variant(
        ws,
        channel="beta",
        minimum_version="2026.08.0",
    )

    assert ws.supervisor_api.call_args_list[-1] == call(
        "/supervisor/info",
        method="get",
        timeout=30.0,
    )


def test_configure_beta_variant_rejects_terminal_update_error() -> None:
    """A non-restart Supervisor error from the update call remains terminal."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        {},
        {},
        _info(
            update_available=True,
            version="2026.07.5",
            version_latest="2026.08.0",
            channel="beta",
        ),
        WSCommandError("invalid update", code="invalid_format"),
    ]

    with pytest.raises(WSCommandError, match="invalid update"):
        _configure_supervisor_image_variant(
            ws,
            channel="beta",
            minimum_version="2026.08.0",
        )

    assert ws.supervisor_api.call_count == 4


def test_configure_beta_variant_accepts_dev_supervisor_versions() -> None:
    """A beta manifest may temporarily advertise a calendar dev build."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        {},
        {},
        _info(
            update_available=False,
            version="2026.09.0.dev1234",
            version_latest="2026.09.0.dev1234",
            channel="beta",
        ),
    ]

    _configure_supervisor_image_variant(
        ws,
        channel="beta",
        minimum_version="2026.09.0.dev1234",
    )

    assert ws.supervisor_api.call_count == 3


def test_configure_beta_variant_installs_exact_core_version() -> None:
    """The shared beta qcow2 contains Core from the same beta manifest."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        {},
        {},
        _info(
            update_available=False,
            version="2026.08.0",
            version_latest="2026.08.0",
            channel="beta",
        ),
        _core_info(
            "2026.8.2",
            version_latest="2026.8.3",
            update_available=True,
        ),
        WebSocketException("Core restarted"),
        _core_info(
            "2026.8.2",
            version_latest="2026.8.3",
            update_available=True,
        ),
        _core_info(
            "2026.8.3",
            version_latest="2026.8.3",
            update_available=False,
        ),
    ]

    with (
        patch("tests.haos_image_build.build_image._wait_http_ok") as wait_http_ok,
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
    ):
        _configure_supervisor_image_variant(
            ws,
            base_url="http://127.0.0.1:18123",
            channel="beta",
            minimum_version="2026.08.0",
            core_version="2026.8.3",
        )

    assert (
        call(
            "/core/update",
            method="post",
            data={"version": "2026.8.3", "backup": False},
            timeout=1800.0,
        )
        in ws.supervisor_api.call_args_list
    )
    wait_http_ok.assert_called_once_with(
        "http://127.0.0.1:18123/manifest.json", timeout=600.0
    )
    assert ws.reconnect.call_count == 2
    sleep.assert_called_once()
    assert ws.supervisor_api.call_args_list[-1] == call(
        "/core/info", method="get", timeout=30.0
    )
