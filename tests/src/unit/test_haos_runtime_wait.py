"""Unit tests for ``haos_runtime._wait_supervisor_update_done``.

Regression coverage for the inaddon-path Supervisor self-update wait
(PR #1600): ``/supervisor/info`` is proxied by HA Core, and while the
Supervisor backend restarts mid-self-update Core returns a structured
``success=False`` frame. The wait must TOLERATE that frame (record it and
re-poll on a fresh id) rather than hard-raise — otherwise it aborts on the
very restart window it exists to span, re-introducing the #1594 inaddon-setup
flake.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

from tests.src.haos_runtime import (
    _is_transient_supervisor_job_error,
    _wait_supervisor_running,
    _wait_supervisor_update_done,
)

_SETTLED = {
    "success": True,
    "result": {
        "update_available": False,
        "version": "2026.06.1",
        "version_latest": "2026.06.1",
    },
}
_PENDING = {
    "success": True,
    "result": {
        "update_available": True,
        "version": "2026.05.1",
        "version_latest": "2026.06.1",
    },
}
_FAILURE = {"success": False, "error": {"code": "unknown_error"}}
_SETUP_STATE = {
    "success": False,
    "error": {
        "code": "unknown_error",
        "message": "System is not ready with state: setup",
    },
}
_UNAUTHORIZED = {
    "success": False,
    "error": {"code": "unauthorized", "message": "Unauthorized"},
}
_STARTUP = {"success": True, "result": {"state": "startup"}}
_RUNNING = {"success": True, "result": {"state": "running"}}


class _FakeWS:
    """Minimal ``websockets.sync.client`` stand-in for the wait's send/recv.

    Echoes the id of the last sent frame back on recv so the wait's
    id-matching loop resolves; serves ``responses`` in order, then ``default``
    indefinitely (or raises ``TimeoutError`` if neither remains).
    """

    def __init__(
        self,
        responses: list[dict[str, Any]],
        *,
        default: dict[str, Any] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._default = default
        self._last_id: int | None = None
        self.sent_ids: list[int] = []

    def send(self, raw: str) -> None:
        msg_id = json.loads(raw)["id"]
        self._last_id = msg_id
        self.sent_ids.append(msg_id)

    def recv(self, timeout: float | None = None) -> str:
        if self._responses:
            resp = dict(self._responses.pop(0))
        elif self._default is not None:
            resp = dict(self._default)
        else:
            raise TimeoutError
        resp.setdefault("id", self._last_id)
        return json.dumps(resp)


def _next_id() -> Callable[[], int]:
    counter = {"n": 0}

    def _next() -> int:
        counter["n"] += 1
        return counter["n"]

    return _next


def test_tolerates_success_false_then_settles() -> None:
    """A success=False restart frame is recorded + re-polled, not raised."""
    ws = _FakeWS([_FAILURE, _SETTLED])
    with (
        patch("tests.src.haos_runtime.time.monotonic", return_value=0.0),
        patch("tests.src.haos_runtime.time.sleep") as sleep,
    ):
        _wait_supervisor_update_done(ws, 1000.0, _next_id())
    # Two polls on strictly-increasing ids; slept once between them.
    assert ws.sent_ids == [1, 2]
    assert sleep.call_count == 1


def test_pending_then_settles() -> None:
    """update_available True (pending) then False (+version_latest) -> settles."""
    ws = _FakeWS([_PENDING, _SETTLED])
    with (
        patch("tests.src.haos_runtime.time.monotonic", return_value=0.0),
        patch("tests.src.haos_runtime.time.sleep"),
    ):
        _wait_supervisor_update_done(ws, 1000.0, _next_id())
    assert ws.sent_ids == [1, 2]


def test_persistent_failure_surfaced_in_timeout() -> None:
    """Persistent success=False -> TimeoutError surfacing the last frame."""
    ws = _FakeWS([], default=_FAILURE)
    clock = {"t": 0.0}

    def _monotonic() -> float:
        clock["t"] += 5.0
        return clock["t"]

    with (
        patch("tests.src.haos_runtime.time.monotonic", side_effect=_monotonic),
        patch("tests.src.haos_runtime.time.sleep"),
        pytest.raises(TimeoutError, match=r"last error.*unknown_error"),
    ):
        _wait_supervisor_update_done(ws, 20.0, _next_id())


def test_malformed_frame_raises_descriptive_error() -> None:
    """A non-JSON frame raises a descriptive RuntimeError, not a bare decode."""

    class _BadWS:
        def send(self, raw: str) -> None:
            pass

        def recv(self, timeout: float | None = None) -> str:
            return "{not valid json"

    with (
        patch("tests.src.haos_runtime.time.monotonic", return_value=0.0),
        patch("tests.src.haos_runtime.time.sleep"),
        pytest.raises(RuntimeError, match=r"malformed WS frame"),
    ):
        _wait_supervisor_update_done(_BadWS(), 1000.0, _next_id())


def test_running_wait_settles_after_startup() -> None:
    """A startup-state /info answer is re-polled until the state is running."""
    ws = _FakeWS([_STARTUP, _RUNNING])
    with (
        patch("tests.src.haos_runtime.time.monotonic", return_value=0.0),
        patch("tests.src.haos_runtime.time.sleep") as sleep,
    ):
        _wait_supervisor_running(ws, 1000.0, _next_id())
    assert ws.sent_ids == [1, 2]
    assert sleep.call_count == 1


def test_running_wait_polls_root_info_endpoint() -> None:
    """The state lives on the root /info endpoint, not /supervisor/info."""
    sent: list[dict[str, Any]] = []

    class _RecordingWS(_FakeWS):
        def send(self, raw: str) -> None:
            sent.append(json.loads(raw))
            super().send(raw)

    ws = _RecordingWS([_RUNNING])
    with patch("tests.src.haos_runtime.time.monotonic", return_value=0.0):
        _wait_supervisor_running(ws, 1000.0, _next_id())
    assert sent == [
        {
            "id": 1,
            "type": "supervisor/api",
            "endpoint": "/info",
            "method": "get",
            "timeout": 30,
        }
    ]


def test_running_wait_tolerates_success_false_then_settles() -> None:
    """A success=False frame mid-boot is recorded + re-polled, not raised."""
    ws = _FakeWS([_FAILURE, _RUNNING])
    with (
        patch("tests.src.haos_runtime.time.monotonic", return_value=0.0),
        patch("tests.src.haos_runtime.time.sleep") as sleep,
    ):
        _wait_supervisor_running(ws, 1000.0, _next_id())
    assert ws.sent_ids == [1, 2]
    assert sleep.call_count == 1


def test_running_wait_timeout_surfaces_last_state() -> None:
    """Persistent startup state -> TimeoutError naming the last state seen."""
    ws = _FakeWS([], default=_STARTUP)
    clock = {"t": 0.0}

    def _monotonic() -> float:
        clock["t"] += 5.0
        return clock["t"]

    with (
        patch("tests.src.haos_runtime.time.monotonic", side_effect=_monotonic),
        patch("tests.src.haos_runtime.time.sleep"),
        pytest.raises(TimeoutError, match=r"last state: 'startup'"),
    ):
        _wait_supervisor_running(ws, 20.0, _next_id())


@pytest.mark.parametrize(
    ("message", "transient"),
    [
        (
            "supervisor/api /addons/x/update failed: {'code': 'unknown_error', "
            "'message': 'Another job is running for job group addon_x'}",
            True,
        ),
        (
            "supervisor/api /addons/x/update failed: {'code': 'unknown_error', "
            "'message': 'Supervisor is not ready to perform this operation, "
            "please try again later'}",
            True,
        ),
        (
            "supervisor/api /addons/x/update failed: {'code': 'unknown_error', "
            "'message': 'No update available for app x'}",
            False,
        ),
    ],
)
def test_transient_supervisor_job_error_markers(message: str, transient: bool) -> None:
    """Only the self-clearing rejections are retried; real errors propagate."""
    assert _is_transient_supervisor_job_error(message) is transient


def test_running_wait_tolerates_setup_state_error_frame() -> None:
    """Supervisor's own not-ready middleware answer is re-polled."""
    ws = _FakeWS([_SETUP_STATE, _RUNNING])
    with (
        patch("tests.src.haos_runtime.time.monotonic", return_value=0.0),
        patch("tests.src.haos_runtime.time.sleep") as sleep,
    ):
        _wait_supervisor_running(ws, 1000.0, _next_id())
    assert ws.sent_ids == [1, 2]
    assert sleep.call_count == 1


@pytest.mark.parametrize(
    "wait",
    [_wait_supervisor_running, _wait_supervisor_update_done],
    ids=["running", "update_done"],
)
def test_permanent_error_frame_raises_without_retry(wait: Any) -> None:
    """A non-restart failure (e.g. unauthorized) surfaces at once, not at deadline."""
    ws = _FakeWS([_UNAUTHORIZED, _RUNNING])
    with (
        patch("tests.src.haos_runtime.time.monotonic", return_value=0.0),
        patch("tests.src.haos_runtime.time.sleep") as sleep,
        pytest.raises(RuntimeError, match=r"failed: .*unauthorized"),
    ):
        wait(ws, 1000.0, _next_id())
    assert ws.sent_ids == [1]
    sleep.assert_not_called()


@pytest.mark.parametrize(
    ("wait", "frames"),
    [
        (_wait_supervisor_running, [_STARTUP, _RUNNING]),
        (_wait_supervisor_update_done, [_PENDING, _SETTLED]),
    ],
    ids=["running", "update_done"],
)
def test_poll_sleep_is_capped_to_deadline(wait: Any, frames: list[dict]) -> None:
    """Near the deadline the 10s poll sleep shrinks to the remaining budget."""
    ws = _FakeWS(frames)
    with (
        patch("tests.src.haos_runtime.time.monotonic", return_value=8.0),
        patch("tests.src.haos_runtime.time.sleep") as sleep,
    ):
        wait(ws, 15.0, _next_id())
    sleep.assert_called_once_with(7.0)
