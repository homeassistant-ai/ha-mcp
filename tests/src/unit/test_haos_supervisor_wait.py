"""Unit tests for HAOS Supervisor/Core image-variant readiness helpers.

The image pins stable OS/Core and a Supervisor minimum; Supervisor self-updates
to the channel head after boot. Until that finishes ``need_update`` is True and store
operations are blocked by ``JobCondition.SUPERVISOR_UPDATED``. The helpers wait
for readiness, select the requested channel, and install an exact Core version.
These tests mock the WebSocket so they need no booted HAOS; ``time.sleep`` is
patched out so polling runs instantly.
"""

from __future__ import annotations

import json
from threading import Event
from typing import Any
from unittest.mock import Mock, call, patch

import pytest
from websockets.exceptions import WebSocketException

from tests.haos_image_build.build_image import (
    HAWebSocket,
    OAuthCredentials,
    WSCommandError,
    _apply_supervisor_image_update,
    _configure_core_image_variant,
    _configure_supervisor_image_variant,
    _reconnect_during_supervisor_update,
    _SupervisorReadinessTimeout,
    _wait_core_version,
    _wait_supervisor_channel_metadata,
    _wait_supervisor_ready,
    onboard,
)


def test_supervisor_api_reports_a_disconnected_websocket() -> None:
    """A disconnected WebSocket produces a diagnostic transport error."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )

    with pytest.raises(
        ConnectionError,
        match=r"supervisor/api get /supervisor/info",
    ):
        ws.supervisor_api("/supervisor/info")


def test_supervisor_api_shares_receive_deadline_across_frames() -> None:
    """Skipped WebSocket frames consume the command's receive budget."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    socket = Mock()
    socket.recv.side_effect = [
        json.dumps({"id": 99, "type": "event", "event": {}}),
        json.dumps({"id": 1, "type": "result", "success": True, "result": {}}),
    ]
    ws._ws = socket

    with patch(
        "tests.haos_image_build.build_image.time.monotonic",
        side_effect=[10.0, 10.5, 10.5, 10.5, 10.5, 11.0, 12.5, 12.5],
    ):
        assert ws.supervisor_api("/supervisor/info", timeout=5.0) == {}

    assert socket.recv.call_args_list == [call(timeout=4.0), call(timeout=2.5)]


def test_supervisor_api_expiry_during_serialization_does_not_dispatch() -> None:
    """Serialization time is charged before a command can be dispatched."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    socket = Mock()
    ws._ws = socket

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[10.0, 12.0],
        ),
        patch(
            "tests.haos_image_build.build_image.json.dumps",
            return_value='{"serialized": true}',
        ) as serialize,
        pytest.raises(
            TimeoutError,
            match=r"supervisor/api post /core/update send exceeded its deadline",
        ),
    ):
        ws.supervisor_api("/core/update", method="post", data={}, timeout=1.0)

    serialize.assert_called_once()
    socket.send.assert_not_called()
    socket.recv.assert_not_called()


def test_supervisor_api_stalled_send_is_bounded() -> None:
    """A stalled socket write is cancelled within the command's deadline."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    socket = Mock()
    release_send = Event()

    def stalled_send(_: str) -> None:
        release_send.wait()

    socket.send.side_effect = stalled_send
    socket.close_socket.side_effect = release_send.set
    ws._ws = socket

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            return_value=10.0,
        ),
        pytest.raises(
            TimeoutError,
            match="command outcome is unknown",
        ),
    ):
        ws.supervisor_api("/core/update", method="post", data={}, timeout=0.01)

    socket.send.assert_called_once()
    socket.close_socket.assert_called_once()
    socket.recv.assert_not_called()
    assert ws._ws is None


def test_send_completion_after_deadline_invalidates_socket() -> None:
    """A send completing at its deadline still has an unknown outcome."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    socket = Mock()
    ws._ws = socket

    class CompletedThread:
        def __init__(self, *, target: Any, **_: Any) -> None:
            self._target = target

        def start(self) -> None:
            pass

        def join(self, timeout: float | None = None) -> None:
            del timeout
            self._target()

        def is_alive(self) -> bool:
            return False

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[10.0, 10.0, 10.0, 11.0],
        ),
        patch(
            "tests.haos_image_build.build_image.threading.Thread",
            CompletedThread,
        ),
        pytest.raises(TimeoutError, match="command outcome is unknown"),
    ):
        ws._send_with_deadline(
            "{}",
            deadline=11.0,
            operation="test supervisor send",
        )

    socket.send.assert_called_once_with("{}")
    socket.close_socket.assert_called_once()
    assert ws._ws is None


def test_supervisor_api_queued_send_timeout_does_not_dispatch() -> None:
    """A queued worker is cancelled without reporting an unknown outcome."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    socket = Mock()
    ws._ws = socket
    queued_targets: list[Any] = []

    class QueuedThread:
        def __init__(self, *, target: Any, **_: Any) -> None:
            queued_targets.append(target)

        def start(self) -> None:
            pass

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return True

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[10.0, 10.0, 11.0],
        ),
        patch(
            "tests.haos_image_build.build_image.threading.Thread",
            QueuedThread,
        ),
        pytest.raises(
            TimeoutError,
            match="exceeded its deadline before dispatch",
        ),
    ):
        ws.supervisor_api("/core/update", method="post", data={}, timeout=1.0)

    queued_targets[0]()
    socket.send.assert_not_called()
    socket.close_socket.assert_not_called()
    socket.recv.assert_not_called()
    assert ws._ws is socket


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_message"),
    [
        pytest.param(
            {"code": "unknown_error", "message": ""},
            "unknown_error",
            "",
            id="blank-unknown-error",
        ),
        pytest.param(
            {"code": "invalid_format", "message": "invalid request"},
            "invalid_format",
            "invalid request",
            id="terminal-error",
        ),
    ],
)
def test_supervisor_api_preserves_structured_error_frame(
    error: dict[str, str],
    expected_code: str,
    expected_message: str,
) -> None:
    """Actual WebSocket result frames retain code and raw message fields."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    socket = Mock()
    socket.recv.return_value = json.dumps(
        {
            "id": 1,
            "type": "result",
            "success": False,
            "error": error,
        }
    )
    ws._ws = socket

    with pytest.raises(WSCommandError) as exc_info:
        ws.supervisor_api("/core/update", method="post", data={})

    assert exc_info.value.code == expected_code
    assert exc_info.value.supervisor_message == expected_message
    sent = json.loads(socket.send.call_args.args[0])
    assert sent["type"] == "supervisor/api"
    assert sent["endpoint"] == "/core/update"
    assert sent["method"] == "post"


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param([], id="non-mapping-frame"),
        pytest.param(
            {"id": 1, "type": "event", "success": True, "result": {}},
            id="wrong-frame-type",
        ),
        pytest.param(
            {"id": 1, "type": "result", "result": {}},
            id="missing-success",
        ),
        pytest.param(
            {"id": 1, "type": "result", "success": "yes", "result": {}},
            id="non-boolean-success",
        ),
        pytest.param(
            {"id": 1, "type": "result", "success": True, "result": []},
            id="non-mapping-result",
        ),
    ],
)
def test_supervisor_api_rejects_malformed_result_frames(frame: Any) -> None:
    """A matching frame must satisfy the complete command-result contract."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    socket = Mock()
    socket.recv.return_value = json.dumps(frame)
    ws._ws = socket

    with pytest.raises(RuntimeError, match="invalid WebSocket result"):
        ws.supervisor_api("/supervisor/info")


def test_supervisor_api_accepts_null_write_result_as_empty_mapping() -> None:
    """A successful write acknowledgement may omit a result payload."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    socket = Mock()
    socket.recv.return_value = json.dumps(
        {"id": 1, "type": "result", "success": True, "result": None}
    )
    ws._ws = socket

    assert ws.supervisor_api("/supervisor/reload", method="post") == {}


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


def test_reconnect_refreshes_the_onboarding_access_token() -> None:
    """A long beta build gets a fresh access token before every reconnect."""
    base_url = "http://127.0.0.1:18123"
    http_responses = [
        {"auth_code": "code"},
        {"access_token": "stale", "refresh_token": "refresh"},
        {"access_token": "fresh"},
    ]

    with patch(
        "tests.haos_image_build.build_image._http", side_effect=http_responses
    ) as http:
        credentials = onboard(base_url)
        ws = HAWebSocket(base_url, credentials)
        old_socket = Mock()
        ws._ws = old_socket
        new_socket = Mock()
        new_socket.recv.side_effect = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok", "ha_version": "2026.8.0"}),
        ]
        with (
            patch(
                "websockets.sync.client.connect",
                return_value=new_socket,
            ) as connect,
            patch.object(ws, "_wait_supervisor_api_ready"),
        ):
            ws.reconnect()

    old_socket.close.assert_called_once_with()
    connect.assert_called_once_with(
        "ws://127.0.0.1:18123/api/websocket",
        open_timeout=30,
        close_timeout=10,
    )
    auth_frame = json.loads(new_socket.send.call_args.args[0])
    assert auth_frame == {"type": "auth", "access_token": "fresh"}

    assert credentials.access_token == "fresh"
    http.assert_called_with(
        "POST",
        f"{base_url}/auth/token",
        form={
            "client_id": base_url,
            "grant_type": "refresh_token",
            "refresh_token": "refresh",
        },
    )


def test_deadline_connect_bounds_the_authentication_send() -> None:
    """A reconnect deadline also bounds the authentication frame send."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    socket = Mock()
    socket.recv.side_effect = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok", "ha_version": "2026.8.0"}),
    ]
    auth_message = json.dumps({"type": "auth", "access_token": "access"})

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            return_value=1.0,
        ),
        patch("websockets.sync.client.connect", return_value=socket),
        patch.object(ws, "_send_with_deadline") as send,
    ):
        ws._connect(deadline=10.0)

    send.assert_called_once_with(
        auth_message,
        deadline=10.0,
        operation="WebSocket authentication send",
    )
    socket.send.assert_not_called()


def test_reconnect_rejects_a_refresh_response_without_an_access_token() -> None:
    """A malformed refresh response fails before WebSocket authentication."""
    base_url = "http://127.0.0.1:18123"
    with patch(
        "tests.haos_image_build.build_image._http",
        side_effect=[
            {"auth_code": "code"},
            {"access_token": "stale", "refresh_token": "refresh"},
            {},
        ],
    ):
        credentials = onboard(base_url)
        ws = HAWebSocket(base_url, credentials)
        ws._ws = Mock()
        with (
            patch.object(HAWebSocket, "__enter__", autospec=True, return_value=ws),
            patch.object(ws, "_wait_supervisor_api_ready"),
            pytest.raises(RuntimeError, match="no access token"),
        ):
            ws.reconnect()


def test_reconnect_propagates_one_absolute_deadline() -> None:
    """Every blocking reconnect phase consumes the same absolute deadline."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[1.0, 2.0, 4.0],
        ),
        patch.object(ws, "_refresh_access_token") as refresh,
        patch.object(ws, "_connect") as connect,
        patch.object(ws, "_wait_supervisor_api_ready") as wait_ready,
    ):
        ws.reconnect(deadline=10.0)

    refresh.assert_called_once_with(timeout=8.0)
    connect.assert_called_once_with(deadline=10.0)
    wait_ready.assert_called_once_with(timeout=6.0)


def test_supervisor_api_readiness_respects_probe_and_sleep_budget() -> None:
    """A readiness probe and its backoff cannot overrun their shared budget."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    unknown_command = WSCommandError(
        "handler not registered",
        code="unknown_command",
        supervisor_message="Unknown command.",
    )

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[0.0, 0.0, 2.5, 3.0],
        ),
        patch.object(
            ws,
            "supervisor_api",
            side_effect=unknown_command,
        ) as supervisor_api,
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
        pytest.raises(TimeoutError, match="within 3s"),
    ):
        ws._wait_supervisor_api_ready(timeout=3.0)

    supervisor_api.assert_called_once_with(
        "/supervisor/info",
        method="get",
        timeout=3.0,
    )
    sleep.assert_called_once_with(0.5)


def test_returns_immediately_when_up_to_date() -> None:
    """No update pending: a single /supervisor/info read, no polling."""
    ws = Mock()
    ws.supervisor_api.return_value = _info(update_available=False)
    with patch("tests.haos_image_build.build_image.time.sleep") as sleep:
        _wait_supervisor_ready(ws)
    assert ws.supervisor_api.call_count == 1
    sleep.assert_not_called()


def test_tolerates_setup_state_from_initial_probe() -> None:
    """A setup-state error on the first readiness probe is retried."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        WSCommandError(
            "not ready",
            code="unknown_error",
            supervisor_message="System is not ready with state: setup",
        ),
        _info(update_available=False),
    ]

    with patch("tests.haos_image_build.build_image.time.sleep") as sleep:
        result = _wait_supervisor_ready(ws)

    assert result["version"] == "2026.06.1"
    assert ws.supervisor_api.call_count == 2
    sleep.assert_called_once_with(10.0)
    ws.reconnect.assert_called_once()
    assert ws.reconnect.call_args.kwargs["deadline"] is not None


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
        WSCommandError("restart", code="unknown_error", supervisor_message=""),
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
    """A response that arrives after the budget is not accepted."""
    ws = Mock()
    ws.supervisor_api.return_value = _info(update_available=False)
    # Monotonic sequence: deadline, pre-request budget, post-request expiry.
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
    ws.supervisor_api.assert_called_once_with(
        "/supervisor/info", method="get", timeout=5.0
    )


def test_zero_update_timeout_makes_no_readiness_request() -> None:
    """An exhausted readiness budget performs no Supervisor request."""
    ws = Mock()

    with (
        patch("tests.haos_image_build.build_image.time.monotonic", return_value=5.0),
        pytest.raises(TimeoutError),
    ):
        _wait_supervisor_ready(ws, update_timeout=0.0)

    ws.supervisor_api.assert_not_called()


def test_persistent_error_surfaced_in_timeout() -> None:
    """Persistent WSCommandError -> timeout message includes last error."""
    ws = Mock()
    # Initial read sees a pending update; later probes hit a transient
    # WSCommandError until the deadline.
    ws.supervisor_api.side_effect = [
        _info(update_available=True, version="2026.05.1"),
        WSCommandError(
            "supervisor unavailable", code="unknown_error", supervisor_message=""
        ),
    ]
    # Deadline, first probe, first response, second probe, post-sleep, expiry.
    monotonic_values = [0.0, 0.0, 0.0, 5.0, 5.0, 15.0]
    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=monotonic_values,
        ),
        patch("tests.haos_image_build.build_image.time.sleep"),
        pytest.raises(TimeoutError, match=r"last error.*WSCommandError"),
    ):
        _wait_supervisor_ready(ws, update_timeout=10.0)
    assert ws.supervisor_api.call_count == 2


def test_wait_timeout_prefers_the_last_reconnect_error() -> None:
    """A failed reconnect is the most recent readiness-timeout diagnostic."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        _info(update_available=True, version="2026.05.1"),
        ConnectionError("poll failed"),
    ]
    ws.reconnect.side_effect = ConnectionError("refresh failed")

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 5.0, 5.0, 6.0, 15.0],
        ),
        patch("tests.haos_image_build.build_image.time.sleep"),
        pytest.raises(TimeoutError, match="refresh failed"),
    ):
        _wait_supervisor_ready(ws, update_timeout=10.0)

    ws.reconnect.assert_called_once_with(deadline=10.0)


def test_channel_metadata_timeout_includes_the_last_transient_error() -> None:
    """A beta-channel reload timeout preserves its final transport detail."""
    ws = Mock()
    ws.supervisor_api.side_effect = WSCommandError(
        "supervisor unavailable", code="unknown_error", supervisor_message=""
    )

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[0.0, 0.75, 1.0],
        ),
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
        pytest.raises(TimeoutError, match=r"last error.*WSCommandError"),
    ):
        _wait_supervisor_channel_metadata(
            ws,
            channel="beta",
            minimum_version="2026.08.0",
            deadline=1.0,
        )

    ws.supervisor_api.assert_called_once_with(
        "/supervisor/info", method="get", timeout=1.0
    )
    ws.reconnect.assert_called_once_with(deadline=1.0)
    sleep.assert_called_once_with(0.25)


def test_channel_metadata_timeout_prefers_the_last_reconnect_error() -> None:
    """A failed reconnect is the final channel-reload timeout diagnostic."""
    ws = Mock()
    ws.supervisor_api.side_effect = ConnectionError("poll failed")
    ws.reconnect.side_effect = ConnectionError("refresh failed")

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[0.0, 1.0, 1.0],
        ),
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
        pytest.raises(TimeoutError, match="refresh failed"),
    ):
        _wait_supervisor_channel_metadata(
            ws,
            channel="beta",
            minimum_version="2026.08.0",
            deadline=1.0,
        )

    ws.reconnect.assert_called_once_with(deadline=1.0)
    sleep.assert_not_called()


def test_supervisor_update_reconnect_propagates_deadline() -> None:
    """Supervisor update reconnects retain the image-build deadline."""
    ws = Mock()

    assert (
        _reconnect_during_supervisor_update(
            ws,
            context="during test",
            deadline=10.0,
        )
        is None
    )

    ws.reconnect.assert_called_once_with(deadline=10.0)


def test_supervisor_update_retry_caps_sleep_to_remaining_budget() -> None:
    """A near-expiry Supervisor retry cannot sleep past its deadline."""
    ws = Mock()
    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[0.0, 0.0, 0.75, 1.0],
        ),
        patch(
            "tests.haos_image_build.build_image._wait_supervisor_ready",
            side_effect=[
                ConnectionError("restart"),
                _SupervisorReadinessTimeout("done"),
            ],
        ) as wait_ready,
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
        pytest.raises(_SupervisorReadinessTimeout, match="done"),
    ):
        _apply_supervisor_image_update(
            ws,
            channel="beta",
            minimum_version="2026.08.0",
            deadline=1.0,
            timeout=1.0,
        )

    ws.supervisor_api.assert_called_once_with(
        "/supervisor/update", method="post", timeout=1.0
    )
    assert [
        wait_call.kwargs["update_timeout"] for wait_call in wait_ready.call_args_list
    ] == [1.0, 0.0]
    ws.reconnect.assert_called_once_with(deadline=1.0)
    sleep.assert_called_once_with(0.25)


def test_supervisor_update_retry_skips_sleep_when_budget_expires() -> None:
    """An exhausted Supervisor retry budget performs no backoff sleep."""
    ws = Mock()
    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[0.0, 0.0, 1.0],
        ),
        patch(
            "tests.haos_image_build.build_image._wait_supervisor_ready",
            side_effect=ConnectionError("restart"),
        ) as wait_ready,
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
        pytest.raises(TimeoutError, match="beta-image deadline"),
    ):
        _apply_supervisor_image_update(
            ws,
            channel="beta",
            minimum_version="2026.08.0",
            deadline=1.0,
            timeout=1.0,
        )

    ws.supervisor_api.assert_called_once_with(
        "/supervisor/update", method="post", timeout=1.0
    )
    wait_ready.assert_called_once_with(
        ws,
        update_timeout=1.0,
        expected_channel="beta",
        minimum_version="2026.08.0",
    )
    ws.reconnect.assert_called_once_with(deadline=1.0)
    sleep.assert_not_called()


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


def test_configure_variant_rejects_core_without_base_url_before_mutation() -> None:
    """A missing Core URL fails before changing the Supervisor channel."""
    ws = Mock()
    ws.supervisor_api.side_effect = AssertionError("unexpected mutation")

    with pytest.raises(ValueError, match="requires the Home Assistant base URL"):
        _configure_supervisor_image_variant(
            ws,
            channel="beta",
            minimum_version="2026.08.0",
            core_version="2026.8.3",
        )

    ws.supervisor_api.assert_not_called()


def test_configure_variant_rejects_invalid_core_version_before_mutation() -> None:
    """An invalid Core release identifier cannot reach Supervisor update APIs."""
    ws = Mock()
    ws.supervisor_api.side_effect = AssertionError("unexpected mutation")

    with pytest.raises(ValueError, match="Invalid Core version"):
        _configure_supervisor_image_variant(
            ws,
            base_url="http://127.0.0.1:18123",
            channel="beta",
            minimum_version="2026.08.0",
            core_version="../latest",
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
    """A newly advertised beta triggers update and version-floor readiness polling."""
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
        WSCommandError("restarting", code="unknown_error", supervisor_message=""),
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
    """An inconclusive restart-window error falls through to readiness polling."""
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
        WSCommandError("restarting", code="unknown_error", supervisor_message=""),
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


def test_configure_beta_variant_tolerates_setup_state_while_polling() -> None:
    """A setup-state read during restart is retried after the update POST."""
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
        WSCommandError(
            "not ready",
            code="unknown_error",
            supervisor_message="System is not ready with state: setup",
        ),
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

    ws.reconnect.assert_called_once()
    assert ws.reconnect.call_args.kwargs["deadline"] is not None
    assert ws.supervisor_api.call_args_list[-1] == call(
        "/supervisor/info",
        method="get",
        timeout=30.0,
    )


def test_configure_beta_variant_rejects_permanent_unknown_update_error() -> None:
    """A bridged permanent rejection is not mistaken for a restart."""
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
        WSCommandError(
            "update rejected",
            code="unknown_error",
            supervisor_message="System is not ready with state: setup",
        ),
    ]

    with pytest.raises(WSCommandError, match="update rejected"):
        _configure_supervisor_image_variant(
            ws,
            channel="beta",
            minimum_version="2026.08.0",
        )

    assert ws.supervisor_api.call_count == 4


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


def test_configure_beta_variant_preserves_readiness_timeout_details() -> None:
    """A still-pending Supervisor reports version diagnostics on timeout."""
    ws = Mock()
    pending = _info(
        update_available=True,
        version="2026.07.5",
        version_latest="2026.08.0",
        channel="beta",
    )
    ws.supervisor_api.side_effect = [{}, {}, pending, {}, pending]

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0, 2.0, 2.0],
        ),
        patch("tests.haos_image_build.build_image.time.sleep"),
        pytest.raises(TimeoutError, match="did not finish self-updating"),
    ):
        _configure_supervisor_image_variant(
            ws,
            channel="beta",
            minimum_version="2026.08.0",
            timeout=1.0,
        )


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


def test_configure_beta_variant_accepts_final_for_dev_minimum() -> None:
    """A final calendar release satisfies its matching development floor."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        {},
        {},
        _info(
            update_available=False,
            version="2026.09.0",
            version_latest="2026.09.0",
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
    """Beta image setup requests and verifies the manifest's exact Core version."""
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


def test_core_variant_retries_rejected_bootstrap_job() -> None:
    """A first-boot Core job rejects the update before it starts; retry it."""
    ws = Mock()
    ws.supervisor_api.side_effect = [
        {"version": "2026.9.1"},
        WSCommandError(
            "Core job busy",
            code="unknown_error",
            supervisor_message="Another job is running for job group home_assistant_core",
        ),
        {},
    ]
    with (
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
        patch("tests.haos_image_build.build_image._wait_http_ok"),
        patch("tests.haos_image_build.build_image._wait_core_version") as wait_version,
    ):
        _configure_core_image_variant(ws, base_url="http://ha", core_version="2026.9.0")

    updates = [
        args
        for args in ws.supervisor_api.call_args_list
        if args.args[0] == "/core/update"
    ]
    assert len(updates) == 2
    assert updates[0] == updates[1]
    assert updates[0].kwargs["data"] == {"version": "2026.9.0", "backup": False}
    sleep.assert_called_once_with(5.0)
    wait_version.assert_called_once_with(ws, "2026.9.0", timeout=600.0)


def test_core_variant_busy_job_has_bounded_admission_wait() -> None:
    """A stuck bootstrap job must time out without a request at the deadline."""
    ws = Mock()
    busy = WSCommandError(
        "Core job busy",
        code="unknown_error",
        supervisor_message="Another job is running for job group home_assistant_core",
    )
    ws.supervisor_api.side_effect = [{"version": "2026.9.1"}, busy]
    elapsed = [0.0]

    def expire(_delay: float) -> None:
        elapsed[0] = 600.0

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=lambda: elapsed[0],
        ),
        patch("tests.haos_image_build.build_image.time.sleep", side_effect=expire),
        patch("tests.haos_image_build.build_image._wait_core_version") as wait_version,
        pytest.raises(TimeoutError, match="Core job admission"),
    ):
        _configure_core_image_variant(ws, base_url="http://ha", core_version="2026.9.0")

    assert ws.supervisor_api.call_count == 2
    wait_version.assert_not_called()


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("unknown_error", "Another job is running for job group supervisor"),
        ("unknown_error", "Core update blocked because system is unhealthy"),
        ("unauthorized", "Another job is running for job group home_assistant_core"),
    ],
)
def test_core_variant_does_not_retry_other_command_failures(
    code: str, message: str
) -> None:
    """The bootstrap exception must not swallow auth or other semantic errors."""
    ws = Mock()
    failure = WSCommandError("Rejected", code=code, supervisor_message=message)
    ws.supervisor_api.side_effect = [{"version": "2026.9.1"}, failure]
    with (
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
        pytest.raises(WSCommandError) as raised,
    ):
        _configure_core_image_variant(ws, base_url="http://ha", core_version="2026.9.0")

    assert raised.value is failure
    assert ws.supervisor_api.call_count == 2
    sleep.assert_not_called()


def test_wait_core_version_reports_non_convergence() -> None:
    """A Core update that never reaches the target preserves runtime details."""
    ws = Mock()
    old_info = _core_info(
        "2026.8.2",
        version_latest="2026.8.3",
        update_available=True,
    )
    ws.supervisor_api.return_value = old_info

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[0.0, 0.0, 0.5, 1.0, 1.0],
        ),
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
        pytest.raises(TimeoutError) as exc_info,
    ):
        _wait_core_version(ws, "2026.8.3", timeout=1.0)

    message = str(exc_info.value)
    assert "expected='2026.8.3'" in message
    assert repr(old_info) in message
    ws.reconnect.assert_called_once_with(deadline=1.0)
    ws.supervisor_api.assert_called_once_with("/core/info", method="get", timeout=0.5)
    sleep.assert_not_called()


def test_wait_core_version_rejects_matching_response_at_deadline() -> None:
    """A matching Core response received at the deadline is not accepted."""
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    socket = Mock()
    socket.recv.return_value = json.dumps(
        {
            "id": 1,
            "type": "result",
            "success": True,
            "result": _core_info(
                "2026.8.3",
                version_latest="2026.8.3",
                update_available=False,
            ),
        }
    )
    ws._ws = socket

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
        ),
        patch.object(ws, "reconnect") as reconnect,
        patch.object(ws, "_send_with_deadline") as send,
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
        pytest.raises(TimeoutError) as exc_info,
    ):
        _wait_core_version(ws, "2026.8.3", timeout=1.0)

    assert "receive exceeded its deadline" in str(exc_info.value)
    reconnect.assert_called_once_with(deadline=1.0)
    send.assert_called_once()
    socket.recv.assert_called_once_with(timeout=1.0)
    sleep.assert_not_called()


def test_wait_core_version_skips_probe_after_reconnect_exhausts_budget() -> None:
    """No Core version probe starts after reconnect consumes the deadline."""
    ws = Mock()

    with (
        patch(
            "tests.haos_image_build.build_image.time.monotonic",
            side_effect=[0.0, 0.0, 1.0, 1.0, 1.0],
        ),
        patch("tests.haos_image_build.build_image.time.sleep") as sleep,
        pytest.raises(TimeoutError, match="Core version check"),
    ):
        _wait_core_version(ws, "2026.8.3", timeout=1.0)

    ws.reconnect.assert_called_once_with(deadline=1.0)
    ws.supervisor_api.assert_not_called()
    sleep.assert_not_called()


def test_configure_beta_variant_polls_after_blank_unknown_core_update_error() -> None:
    """A blank Core bridge error falls through to exact-version polling."""
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
        WSCommandError(
            "supervisor/api post /core/update failed",
            code="unknown_error",
            supervisor_message="",
        ),
        _core_info(
            "2026.8.3",
            version_latest="2026.8.3",
            update_available=False,
        ),
    ]

    with patch("tests.haos_image_build.build_image._wait_http_ok") as wait_http_ok:
        _configure_supervisor_image_variant(
            ws,
            base_url="http://127.0.0.1:18123",
            channel="beta",
            minimum_version="2026.08.0",
            core_version="2026.8.3",
        )

    wait_http_ok.assert_called_once_with(
        "http://127.0.0.1:18123/manifest.json", timeout=600.0
    )
    assert ws.reconnect.call_count == 1
    assert ws.supervisor_api.call_args_list[-1] == call(
        "/core/info", method="get", timeout=30.0
    )


def test_configure_beta_variant_rejects_terminal_core_update_error() -> None:
    """A nonblank Core bridge rejection remains terminal."""
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
        WSCommandError(
            "core update rejected",
            code="unknown_error",
            supervisor_message="System is not ready with state: setup",
        ),
    ]

    with (
        patch("tests.haos_image_build.build_image._wait_http_ok") as wait_http_ok,
        pytest.raises(WSCommandError, match="core update rejected"),
    ):
        _configure_supervisor_image_variant(
            ws,
            base_url="http://127.0.0.1:18123",
            channel="beta",
            minimum_version="2026.08.0",
            core_version="2026.8.3",
        )

    wait_http_ok.assert_not_called()
