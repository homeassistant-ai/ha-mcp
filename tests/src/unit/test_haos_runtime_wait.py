"""Supervisor readiness waits against Core's supervisor/api WebSocket handler."""

from __future__ import annotations

from collections.abc import Callable
from itertools import count
from unittest.mock import patch

import pytest
from websockets.sync.client import ClientConnection, connect

from tests.src import haos_runtime
from tests.src.haos_runtime import (
    _authenticate_ws_supervisor,
    _is_transient_supervisor_job_error,
    _wait_supervisor_running,
    _wait_supervisor_update_done,
)

from ._haos_build_core import Haos, root_info, supervisor_info
from ._haos_build_core import build_supervisor as build_supervisor
from ._haos_build_core import haos as haos
from ._haos_build_core import target_app as target_app

Wait = Callable[[ClientConnection, float, Callable[[], int]], None]
SETTLED = supervisor_info()
PENDING = supervisor_info(version="2026.05.1", update_available=True)
RUNNING = root_info("running")
STARTUP = root_info("startup")


async def _wait(
    haos: Haos,
    wait: Wait,
    *,
    budget: float = 30,
    token: str | None = None,
    corrupt: bool = False,
) -> None:
    def run() -> None:
        url = haos.base_url.replace("http://", "ws://") + "/api/websocket"
        with connect(url) as connection:
            _authenticate_ws_supervisor(
                connection, token or haos.credentials.access_token
            )
            deadline = haos.clock.monotonic() + budget
            ids = count(1)
            if corrupt:
                recv = connection.recv

                def damaged_frame(*args, **kwargs) -> str:
                    # A transport fault corrupts the frame after Core answers.
                    recv(*args, **kwargs)
                    return "{"

                with patch.object(connection, "recv", damaged_frame):
                    wait(connection, deadline, lambda: next(ids))
            else:
                wait(connection, deadline, lambda: next(ids))

    with patch.object(haos_runtime, "time", haos.clock):
        await haos.run(run)


@pytest.mark.parametrize(
    ("wait", "path", "first", "last"),
    [
        (_wait_supervisor_update_done, "/supervisor/info", PENDING, SETTLED),
        (_wait_supervisor_update_done, "/supervisor/info", None, SETTLED),
        (_wait_supervisor_running, "/info", STARTUP, RUNNING),
        (_wait_supervisor_running, "/info", None, RUNNING),
        (
            _wait_supervisor_running,
            "/info",
            "System is not ready with state: setup",
            RUNNING,
        ),
    ],
    ids=["pending", "update-restart", "startup", "running-restart", "setup-error"],
)
async def test_wait_polls_the_correct_endpoint_until_ready(
    haos: Haos, wait, path, first, last
):
    if first is None:
        haos.supervisor.restarting("GET", path)
    elif isinstance(first, str):
        haos.supervisor.refuse("GET", path, first)
    else:
        haos.supervisor.answer("GET", path, first)
    haos.supervisor.answer("GET", path, last)

    await _wait(haos, wait)

    assert haos.supervisor.requests == [("GET", path)] * 2
    assert haos.clock.sleeps == [10.0]


@pytest.mark.parametrize(
    ("wait", "path", "last", "reason"),
    [
        (_wait_supervisor_running, "/info", STARTUP, "last state: 'startup'"),
        (
            _wait_supervisor_update_done,
            "/supervisor/info",
            None,
            "last error.*unknown_error",
        ),
    ],
)
async def test_timeout_preserves_the_reason_and_caps_the_last_sleep(
    haos: Haos, wait, path, last, reason
) -> None:
    if last is None:
        haos.supervisor.restarting("GET", path)
    else:
        haos.supervisor.answer("GET", path, last)

    with pytest.raises(TimeoutError, match=reason):
        await _wait(haos, wait, budget=17)

    assert haos.clock.sleeps == [10.0, 7.0]


async def test_malformed_frame_raises_descriptive_error(haos: Haos) -> None:
    haos.supervisor.answer("GET", "/supervisor/info", SETTLED)
    with pytest.raises(RuntimeError, match="malformed WS frame"):
        await _wait(haos, _wait_supervisor_update_done, corrupt=True)


@pytest.mark.parametrize(
    "wait", [_wait_supervisor_running, _wait_supervisor_update_done]
)
async def test_core_refuses_an_unprivileged_user_without_retry(
    haos: Haos, wait: Wait
) -> None:
    member = await haos.member_credentials()
    with pytest.raises(RuntimeError, match=r"failed: .*unauthorized"):
        await _wait(haos, wait, token=member.access_token)
    assert haos.clock.sleeps == []


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
