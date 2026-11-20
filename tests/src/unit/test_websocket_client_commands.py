"""``send_command`` and ``send_command_with_event`` against Core's ``websocket_api``.

Core's answer, rejection or silence reaches the caller as what it is, and a
caller that goes away leaves nothing registered behind it.
"""

from __future__ import annotations

import asyncio
import gc
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_state import WebSocketConnectionState

from ._websocket_client_core import EVENT, registered, stalled_send, subscribed
from ._ws_transport import (
    HELD_SCRIPT,
    answers_unread,
    close_gate,
    connected_ws,
    open_gate,
    until,
)


async def test_a_rejected_command_raises_with_cores_message_and_code(
    core_client: HomeAssistantClient,
) -> None:
    """Routing keys off Core's stable code, the user reads Core's message."""
    async with connected_ws(core_client) as client:
        with pytest.raises(HomeAssistantCommandError) as exc:
            await client.send_command("no_such/command")

    assert str(exc.value) == "Command failed: Unknown command."
    assert exc.value.code == "unknown_command"


async def test_a_rejected_command_with_event_raises_with_cores_message(
    core_client: HomeAssistantClient,
) -> None:
    async with connected_ws(core_client) as client:
        with pytest.raises(HomeAssistantCommandError) as exc:
            await client.send_command_with_event("no_such/command")

    assert str(exc.value) == "Command failed: Unknown command."
    assert exc.value.code == "unknown_command"


async def test_an_event_before_the_result_is_paired_as_the_event(
    core_client: HomeAssistantClient,
) -> None:
    """``render_template`` reports a template warning before its result (#2522).

    Taken for the result, it made the command read as failed with an empty
    error, and the warning was lost.
    """
    async with connected_ws(core_client) as client:
        result, event = await client.send_command_with_event(
            "render_template", template="{{ no_such_var }}", report_errors=True
        )

    assert (result["type"], result["success"]) == ("result", True)
    assert event["event"]["level"] == "WARNING"
    assert "no_such_var" in event["event"]["error"]


async def test_a_cancelled_command_leaves_no_pending_future(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A cancelled caller (a search leg cancelled with its siblings, #2291)
    must not leave an entry that only Core's answer would ever remove."""
    close_gate(hass)
    try:
        async with connected_ws(core_client) as client:
            with registered() as seen:
                command = asyncio.create_task(
                    client.send_command("execute_script", sequence=HELD_SCRIPT)
                )
                # Core answers in order: once ``ping`` is back, the script is held.
                assert await client.ping() is True
            assert await seen.alive() == ["pending"]

            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                _ = await command
            del command

            assert await seen.alive() == []
    finally:
        await open_gate(hass)


async def test_a_command_with_event_cancelled_in_its_send_leaves_nothing(
    core_client: HomeAssistantClient,
) -> None:
    """Both the result and the event registration go with the caller."""
    async with connected_ws(core_client) as client:
        with registered() as seen, answers_unread(client):
            with stalled_send(client) as written:
                command = asyncio.create_task(
                    client.send_command_with_event("subscribe_events", event_type=EVENT)
                )
                await written.wait()
                assert await seen.alive() == ["pending", "event"]
                command.cancel()
                with pytest.raises(asyncio.CancelledError):
                    _ = await command
            del command

            assert await seen.alive() == []


async def test_a_command_with_event_cancelled_awaiting_its_event_leaves_nothing(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Once the caller is gone no event will ever claim the registration."""
    async with connected_ws(core_client) as client:
        with registered() as seen:
            command = asyncio.create_task(
                client.send_command_with_event("subscribe_events", event_type=EVENT)
            )
            await until(lambda: subscribed(hass) == 1)
            # Answered in order: the command has its result and waits for
            # the event.
            assert await client.ping() is True
            result = seen.pending[0]()
            assert result is not None and result.done()
            del result

            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                _ = await command
            del command

            assert await seen.alive() == []


async def test_a_failed_send_raises_the_original_error_not_never_sent(
    core_client: HomeAssistantClient,
) -> None:
    """Bytes may be on the wire when the send fails, so a write consumer must
    treat it as ambiguous, never as safely retryable."""
    boom = ConnectionResetError("frame write dropped mid-send")
    async with connected_ws(core_client) as client:
        assert client.websocket is not None
        with (
            registered() as seen,
            patch.object(client.websocket, "send", side_effect=boom),
            pytest.raises(ConnectionResetError) as exc,
        ):
            await client.send_command("ping")
        raised = exc.value
        del exc

        assert raised is boom
        assert not isinstance(raised, HomeAssistantCommandNotSent)
        del raised, boom
        assert await seen.alive() == []


async def test_a_command_on_a_closed_socket_is_never_sent(
    core_client: HomeAssistantClient,
) -> None:
    """The one provably-never-sent case, which a write consumer may retry."""
    async with connected_ws(core_client) as client:
        pass

    with pytest.raises(HomeAssistantCommandNotSent):
        await client.send_command("ping")


async def test_an_explicit_wait_replaces_the_default(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long add-on install passes its own wait; capped at the default, the
    client gave up on an install that was still running.

    Core refuses unknown fields, so the ping also shows ``_wait_timeout``
    stays out of the command.
    """
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 0)
    async with connected_ws(core_client) as client:
        answer = await client.send_command("ping", _wait_timeout=5)

    assert answer == {"success": True, "type": "pong"}


async def test_a_command_without_a_wait_gives_up_at_the_default(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 0)
    async with connected_ws(core_client) as client:
        with pytest.raises(HomeAssistantCommandTimeout):
            await client.send_command("ping")


async def test_a_command_field_named_wait_timeout_is_sent(
    core_client: HomeAssistantClient,
) -> None:
    """Only the underscored control keyword is the client's; Core refuses the
    field here, which shows it arrived."""
    async with connected_ws(core_client) as client:
        with pytest.raises(HomeAssistantCommandError) as exc:
            await client.send_command("ping", wait_timeout=99)

    assert exc.value.code == "invalid_format"
    assert "'wait_timeout': 99" in str(exc.value)


@pytest.mark.parametrize("kind", ["pending_request", "event_response"])
async def test_dropping_a_failed_future_retrieves_its_exception(kind: str) -> None:
    """A connection drop fails every registered future; dropped unread, each
    logs an ERROR "Future exception was never retrieved" at collection."""
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        state = WebSocketConnectionState()
        failed = getattr(state, f"register_{kind}")(7)
        failed.set_exception(HomeAssistantConnectionError("connection went away"))
        del failed
        getattr(state, f"cancel_{kind}")(7)
        gc.collect()
    finally:
        loop.set_exception_handler(previous_handler)

    assert getattr(state, f"resolve_{kind}")(7) is None
    assert not [c for c in reported if "never retrieved" in str(c.get("message"))]


async def test_dropping_an_already_cancelled_future_does_not_raise() -> None:
    """The cleanup runs while a cancellation unwinds; reading a cancelled
    future's exception there would raise out of it."""
    state = WebSocketConnectionState()
    state.register_pending_request(7).cancel()
    state.register_event_response(11).cancel()

    state.cancel_pending_request(7)
    state.cancel_event_response(11)

    assert state.resolve_pending_request(7) is None
    assert state.resolve_event_response(11) is None
