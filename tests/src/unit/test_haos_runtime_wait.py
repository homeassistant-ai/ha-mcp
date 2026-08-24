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

from tests.src import haos_runtime
from tests.src.haos_runtime import _wait_supervisor_update_done

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


class _SupervisorChannelWS(_FakeWS):
    """Context-managed WS fake with the real HA authentication prelude."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                *responses,
            ]
        )
        self.sent_frames: list[dict[str, Any]] = []

    def __enter__(self) -> _SupervisorChannelWS:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.sent_frames.append(frame)
        if "id" in frame:
            self._last_id = frame["id"]
            self.sent_ids.append(frame["id"])


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


def test_wait_returns_the_settled_supervisor_info() -> None:
    """Callers can assert the version/channel that actually settled."""
    ws = _FakeWS([_SETTLED])

    with patch("tests.src.haos_runtime.time.monotonic", return_value=0.0):
        result = _wait_supervisor_update_done(ws, 1000.0, _next_id())

    assert result == _SETTLED["result"]


def test_ensure_supervisor_channel_updates_and_verifies_beta() -> None:
    """The HAOS harness upgrades stable Supervisor before add-on tests run."""
    fake_ws = _SupervisorChannelWS(
        [
            {"success": True, "result": {}},  # /supervisor/options
            {"success": True, "result": {}},  # /supervisor/reload
            {
                "success": True,
                "result": {
                    "channel": "beta",
                    "version": "2026.07.5",
                    "version_latest": "2026.08.0",
                    "update_available": True,
                },
            },
            {"success": True, "result": {}},  # /supervisor/update
            {
                "success": True,
                "result": {
                    "channel": "beta",
                    "version": "2026.08.0",
                    "version_latest": "2026.08.0",
                    "update_available": False,
                },
            },
        ]
    )

    with patch("websockets.sync.client.connect", return_value=fake_ws):
        result = haos_runtime.ensure_supervisor_channel(
            "http://127.0.0.1:18123",
            "ha-token",
            channel="beta",
            minimum_version="2026.08.0",
        )

    commands = [frame for frame in fake_ws.sent_frames if "id" in frame]
    assert [frame["endpoint"] for frame in commands] == [
        "/supervisor/options",
        "/supervisor/reload",
        "/supervisor/info",
        "/supervisor/update",
        "/supervisor/info",
    ]
    assert commands[0]["data"] == {"channel": "beta"}
    assert result["channel"] == "beta"
    assert result["version"] == "2026.08.0"


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
