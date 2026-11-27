"""The HAOS image builder's Core client, against real Core.

``HAWebSocket`` logs in to Core, calls Supervisor through Core's
``supervisor/api`` command and reconnects after a restart. Every reply here is
Core's, with the Supervisor stand-in behind it (``_haos_build_core``).
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError

import pytest
from websockets.sync.client import ClientConnection

from tests.haos_image_build import build_image
from tests.haos_image_build.build_image import (
    HAWebSocket,
    OAuthCredentials,
    WSCommandError,
)

from ._haos_build_core import Haos, root_info, supervisor_info
from ._haos_build_core import build_supervisor as build_supervisor
from ._haos_build_core import haos as haos
from ._haos_build_core import haos_booting as haos_booting
from ._haos_build_core import target_app as target_app
from ._ws_transport import until


async def _assert_disconnected(haos: Haos, ws: HAWebSocket) -> None:
    """The client dropped its socket: the next command cannot be sent on it."""
    with pytest.raises(ConnectionError, match="not connected"):
        await haos.run(ws.supervisor_api, "/supervisor/info")


def test_a_command_before_any_login_names_itself_in_the_error() -> None:
    ws = HAWebSocket(
        "http://127.0.0.1:1", OAuthCredentials(access_token="", refresh_token="")
    )

    with pytest.raises(ConnectionError, match=r"supervisor/api get /supervisor/info"):
        ws.supervisor_api("/supervisor/info")


async def test_frames_skipped_while_waiting_spend_the_commands_receive_budget(
    haos: Haos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frame for another request does not restart the command's timeout:
    each receive gets what is left of it."""
    haos.supervisor.answer("GET", "/info", root_info("running"))
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    # The first command gives up before Supervisor answers; its result then
    # arrives while the second command waits for its own, which Supervisor
    # holds until the builder has received that late frame.
    second_asked = asyncio.Event()
    late_frame_received = threading.Event()

    async def answer_once_the_second_command_waits() -> None:
        await second_asked.wait()

    async def answer_after_the_late_frame() -> None:
        second_asked.set()
        await until(late_frame_received.is_set)

    haos.supervisor.when("GET", "/info", answer_once_the_second_command_waits)
    haos.supervisor.when("GET", "/supervisor/info", answer_after_the_late_frame)
    ws = await haos.connected()
    real_send = ClientConnection.send

    def slow_send(self: ClientConnection, *args: Any, **kwargs: Any) -> None:
        # Sending spends nearly all of the budget; Core still waits its full
        # timeout (the builder passes it on), so only the builder gives up.
        real_send(self, *args, **kwargs)
        haos.clock.advance(4.99)

    with monkeypatch.context() as slow, pytest.raises(TimeoutError):
        slow.setattr(ClientConnection, "send", slow_send)
        await haos.run(ws.supervisor_api, "/info", timeout=5.0)
    timeouts: list[float | None] = []
    real_recv = ClientConnection.recv

    def timed_recv(self: ClientConnection, *args: Any, **kwargs: Any) -> Any:
        timeouts.append(kwargs.get("timeout"))
        frame = real_recv(self, *args, **kwargs)
        haos.clock.advance(1.5)  # Receiving a frame takes time.
        late_frame_received.set()
        return frame

    monkeypatch.setattr(ClientConnection, "recv", timed_recv)

    info = await haos.run(ws.supervisor_api, "/supervisor/info", timeout=5.0)

    assert info == supervisor_info()
    assert timeouts == [5.0, 3.5]


async def test_time_spent_serializing_counts_before_a_command_is_sent(
    haos: Haos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline spent while the command is built sends nothing."""
    ws = await haos.connected()

    def slow_dumps(value: Any) -> str:
        haos.clock.advance(2.0)
        return json.dumps(value)

    monkeypatch.setattr(
        build_image, "json", SimpleNamespace(dumps=slow_dumps, loads=json.loads)
    )

    with pytest.raises(
        TimeoutError, match=r"supervisor/api post /core/update send exceeded"
    ):
        await haos.run(
            ws.supervisor_api, "/core/update", method="post", data={}, timeout=1.0
        )

    assert haos.supervisor.requests == []


async def test_a_stalled_send_is_abandoned_at_its_deadline_and_the_socket_dropped(
    haos: Haos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write the socket never finishes may still reach Core, so the client
    reports an unknown outcome and drops the socket instead of hanging."""
    ws = await haos.connected()
    release = threading.Event()
    real_close = ClientConnection.close_socket

    def stalled_send(self: ClientConnection, *args: Any, **kwargs: Any) -> None:
        release.wait(5)

    def close_socket(self: ClientConnection) -> None:
        release.set()
        real_close(self)

    monkeypatch.setattr(ClientConnection, "send", stalled_send)
    monkeypatch.setattr(ClientConnection, "close_socket", close_socket)

    with pytest.raises(TimeoutError, match="command outcome is unknown"):
        await haos.run(
            ws.supervisor_api, "/core/update", method="post", data={}, timeout=0.01
        )

    assert release.is_set()
    await _assert_disconnected(haos, ws)
    assert haos.supervisor.requests == []


async def test_a_send_that_ends_after_its_deadline_has_an_unknown_outcome(
    haos: Haos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command was written, but too late to wait for its answer."""
    haos.supervisor.answer("POST", "/core/update")
    ws = await haos.connected()
    real_send = ClientConnection.send

    def late_send(self: ClientConnection, *args: Any, **kwargs: Any) -> None:
        real_send(self, *args, **kwargs)
        haos.clock.advance(10.0)

    monkeypatch.setattr(ClientConnection, "send", late_send)

    with pytest.raises(TimeoutError, match="command outcome is unknown"):
        await haos.run(
            ws.supervisor_api, "/core/update", method="post", data={}, timeout=5.0
        )

    await _assert_disconnected(haos, ws)


async def test_a_send_not_started_by_its_deadline_is_cancelled_and_the_socket_kept(
    haos: Haos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that never began cannot have reached Core: nothing is sent, and
    the socket stays usable for the next command."""
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    ws = await haos.connected()
    held: list[threading.Thread] = []

    class LateThread(threading.Thread):
        """A worker the scheduler starts only after the caller gave up."""

        def start(self) -> None:
            held.append(self)

        def join(self, timeout: float | None = None) -> None:
            if self not in held:
                super().join(timeout)

        def is_alive(self) -> bool:
            return self in held or super().is_alive()

    with monkeypatch.context() as late:
        late.setattr(
            build_image,
            "threading",
            SimpleNamespace(Thread=LateThread, Lock=threading.Lock),
        )
        with pytest.raises(TimeoutError, match="before dispatch"):
            await haos.run(
                ws.supervisor_api, "/core/update", method="post", data={}, timeout=1.0
            )

    (worker,) = held
    held.clear()
    threading.Thread.start(worker)
    worker.join(5)
    assert haos.supervisor.requests == []
    assert await haos.run(ws.supervisor_api, "/supervisor/info") == supervisor_info()


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        pytest.param({"status": 502, "text": ""}, "", id="supervisor-restarting"),
        pytest.param(
            {
                "status": 400,
                "payload": {"result": "error", "message": "update rejected"},
            },
            "update rejected",
            id="supervisor-refusal",
        ),
    ],
)
async def test_a_supervisor_failure_keeps_cores_code_and_supervisors_message(
    haos: Haos, answer: dict[str, Any], message: str
) -> None:
    """Callers tell a restart (blank) from a refusal by the message Core passes on."""
    haos.supervisor.reply("POST", "/core/update", **answer)
    ws = await haos.connected()

    with pytest.raises(WSCommandError) as raised:
        await haos.run(ws.supervisor_api, "/core/update", method="post", data={})

    assert raised.value.code == "unknown_error"
    assert raised.value.supervisor_message == message
    assert haos.supervisor.requests == [("POST", "/core/update")]


async def test_a_login_that_is_not_an_administrator_is_refused_by_core(
    haos: Haos,
) -> None:
    ws = await haos.connected(await haos.member_credentials())

    with pytest.raises(WSCommandError) as raised:
        await haos.run(ws.supervisor_api, "/core/update", method="post", data={})

    assert raised.value.code == "unauthorized"
    assert haos.supervisor.requests == []


async def test_reconnect_logs_in_again_with_a_refreshed_token(haos: Haos) -> None:
    """A long build outlives its first access token: every reconnect refreshes."""
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    ws = await haos.connected()
    logins = list(haos.relay.requests)

    await haos.run(ws.reconnect)

    # Onboarding exchanged the code; the reconnect refreshed, then logged in.
    assert haos.relay.requests[len(logins) :] == [
        "POST /auth/token",
        "GET /api/websocket",
    ]
    assert await haos.run(ws.supervisor_api, "/supervisor/info") == supervisor_info()


async def test_reconnect_with_a_revoked_login_fails_at_the_token_refresh(
    haos: Haos,
) -> None:
    ws = HAWebSocket(haos.base_url, haos.credentials)
    haos.revoke_login()

    with pytest.raises(HTTPError) as raised:
        await haos.run(ws.reconnect)

    assert raised.value.code == 400
    assert haos.relay.websockets == 0


async def test_the_login_send_is_bounded_by_the_connect_deadline(
    haos: Haos, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def stalled_send(self: ClientConnection, *args: Any, **kwargs: Any) -> None:
        release.wait(5)

    monkeypatch.setattr(ClientConnection, "send", stalled_send)
    ws = HAWebSocket(haos.base_url, haos.credentials)

    with pytest.raises(TimeoutError, match="WebSocket authentication send"):
        await haos.run(ws.connect, deadline=haos.clock.now + 0.3)

    release.set()


async def test_reconnect_stops_when_its_deadline_passes_during_the_readiness_probe(
    haos: Haos,
) -> None:
    """Token refresh, login and the readiness probe share one deadline."""
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    haos.supervisor.when("GET", "/supervisor/info", lambda: haos.clock.advance(20.0))
    ws = await haos.connected()

    with pytest.raises(TimeoutError, match="exceeded its deadline"):
        await haos.run(ws.reconnect, deadline=haos.clock.now + 10.0)


async def test_readiness_waits_for_hassio_to_register_supervisor_api(
    haos_booting: Haos,
) -> None:
    """Right after a Core restart ``supervisor/api`` is an unknown command
    until ``hassio`` loads; the probe backs off and retries."""
    haos = haos_booting
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    haos.clock.on_sleep[1] = lambda: haos.in_core(haos.load_supervisor_api)
    ws = await haos.connected()

    await haos.run(ws.wait_supervisor_api_ready, timeout=60.0)

    assert haos.clock.sleeps == [1.0, 1.5]
    assert haos.supervisor.requests == [("GET", "/supervisor/info")]


async def test_readiness_never_sleeps_past_its_budget(haos_booting: Haos) -> None:
    ws = await haos_booting.connected()

    with pytest.raises(TimeoutError, match="within 3s"):
        await haos_booting.run(ws.wait_supervisor_api_ready, timeout=3.0)

    assert haos_booting.clock.sleeps == [1.0, 1.5, 0.5]


async def test_readiness_does_not_retry_a_supervisor_failure(haos: Haos) -> None:
    """Only the unknown command means "still booting"; a Supervisor error is real."""
    haos.supervisor.refuse("GET", "/supervisor/info", "renamed endpoint")
    ws = await haos.connected()

    with pytest.raises(WSCommandError, match="renamed endpoint"):
        await haos.run(ws.wait_supervisor_api_ready, timeout=60.0)

    assert haos.clock.sleeps == []
