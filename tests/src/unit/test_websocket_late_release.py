"""Subscription cleanup must survive the connection going away under it.

Background cleanup collects failures on its original socket; a direct
``unsubscribe_events`` / ``unsubscribe_command`` treats a socket closed during
the send or before the answer as the transport loss it is.

The client is the server's own, connected to Core's ``websocket_api``. Core
acknowledges a command late when ``execute_script`` waits on a state, and a
subscription is released for real unless the test takes Core's answer away.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.websockets.exceptions import ConnectionClosed
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

from ._ws_transport import (
    HELD_SCRIPT,
    abort_transport,
    answers_unread,
    close_gate,
    connected_ws,
    open_gate,
    spawned_by,
    until,
)

WS_LOGGER = "ha_mcp.client.websocket_client"
EVENT = "b11_late_release"


def _subscribed(hass: HomeAssistant) -> int:
    return hass.bus.async_listeners().get(EVENT, 0)


def _inject(
    failure: str,
    client: HomeAssistantWebSocketClient,
    stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncMock:
    """Make the release meet ``failure``; return the ``send_command`` it calls.

    A dropped connection and a socket that closed under the send are no
    answer, so ``send_command`` raises them. A timeout and a cancellation are
    Core's answer left unread. ``unexpected`` is a bug in the client's own
    transport, below ``send_command``.
    """
    if failure == "disconnect":
        return stack.enter_context(
            patch.object(
                client,
                "send_command",
                side_effect=HomeAssistantConnectionError("test connection dropped"),
            )
        )
    if failure == "send_closed":
        return stack.enter_context(
            patch.object(
                client, "send_command", side_effect=ConnectionClosed(None, None)
            )
        )
    if failure == "timeout":
        stack.enter_context(answers_unread(client))
        monkeypatch.setattr(
            "ha_mcp.client.websocket_client.DEFAULT_COMMAND_WAIT_TIMEOUT", 0.05
        )
    elif failure == "cancel":
        stack.enter_context(answers_unread(client))
    elif failure == "unexpected":
        assert client.websocket is not None
        stack.enter_context(
            patch.object(
                client.websocket,
                "send",
                side_effect=RuntimeError("unexpected cleanup bug"),
            )
        )
    return stack.enter_context(
        patch.object(client, "send_command", wraps=client.send_command)
    )


async def _abandon_a_late_acknowledgement(
    hass: HomeAssistant, client: HomeAssistantWebSocketClient
) -> int:
    """A command whose caller gave up before Core acknowledged it; its id."""
    close_gate(hass)
    with (
        patch.object(
            client, "send_json_message", wraps=client.send_json_message
        ) as sent,
        pytest.raises(TimeoutError),
    ):
        await client.subscribe_command(
            "execute_script", wait_timeout=0, sequence=HELD_SCRIPT
        )
    message_id: int = sent.await_args_list[0].args[0]["id"]
    return message_id


async def _release_in_background(
    release_path: str,
    failure: str,
    hass: HomeAssistant,
    client: HomeAssistantWebSocketClient,
    released: int,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncMock:
    """Start the background release, meet ``failure``, wait for its end.

    Returns the ``send_command`` the background release called.
    """
    with ExitStack() as stack:
        injected: list[AsyncMock] = []

        def inject() -> None:
            injected.append(_inject(failure, client, stack, monkeypatch))

        if release_path == "late_ack":
            # Only the background release meets the failure: Core's
            # acknowledgement itself is still read.
            with spawned_by(client, on_spawn=inject) as spawned:
                await open_gate(hass)
                task = await spawned.last()
        else:
            # The deadline cut the inline release short after it
            # went out; the failure meets the one finished later.
            with spawned_by(client) as spawned:
                await client.release_subscription(released)
            inject()
            task = await spawned.last()
        (release,) = injected
        completed = asyncio.Event()
        task.add_done_callback(lambda _task: completed.set())
        if failure == "cancel":
            await until(lambda: release.await_count == 1)
            task.cancel()
        async with asyncio.timeout(5):
            await completed.wait()
        del task, spawned
        await asyncio.sleep(0)
        gc.collect()

        return release


@pytest.mark.parametrize("release_path", ["late_ack", "cleanup_deadline"])
@pytest.mark.parametrize(
    "failure",
    ["answered", "disconnect", "send_closed", "timeout", "cancel", "unexpected"],
)
async def test_background_release_collects_outcome(
    release_path: str,
    failure: str,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lost connection or timeout must not become an unhandled task error.

    ``late_ack``: Core acknowledges a command after its caller gave up, and
    the dispatch releases it in the background. ``cleanup_deadline``: a
    release from a ``finally`` runs out of time and finishes in the
    background. Either way exactly one ``unsubscribe_events`` goes out for
    that id, a timeout Core never answered stays visible at WARNING and a bug
    at ERROR, and nothing reaches the loop's unhandled-exception handler.
    Never await the background task or read its exception here: that would
    hide the bug.
    """
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    caplog.set_level(logging.DEBUG, logger=WS_LOGGER)
    try:
        async with connected_ws(core_client) as client:
            if release_path == "late_ack":
                released = await _abandon_a_late_acknowledgement(hass, client)
            else:
                released, _queue = await client.subscribe_command(
                    "subscribe_events", event_type=EVENT
                )
                assert _subscribed(hass) == 1
                monkeypatch.setattr(
                    "ha_mcp.client.websocket_client.CLEANUP_TIMEOUT_SECONDS", 0
                )
            caplog.clear()

            release = await _release_in_background(
                release_path, failure, hass, client, released, monkeypatch
            )

            assert release.await_args_list == [
                call("unsubscribe_events", subscription=released)
            ]
    finally:
        loop.set_exception_handler(previous_handler)
        await open_gate(hass)

    assert not unhandled, [context["message"] for context in unhandled]
    errors = [record for record in caplog.records if record.levelno >= logging.WARNING]
    if failure == "timeout":
        assert [record.levelno for record in errors] == [logging.WARNING]
        assert "timed out" in errors[0].getMessage()
    elif failure == "unexpected":
        assert [record.levelno for record in errors] == [logging.ERROR]
        assert errors[0].exc_info is not None
        assert isinstance(errors[0].exc_info[1], RuntimeError)
    else:
        assert not errors


@pytest.mark.parametrize("closed", ["during_send", "before_ack"])
@pytest.mark.parametrize("method", ["unsubscribe_events", "unsubscribe_command"])
async def test_unsubscribe_treats_a_closed_socket_as_transport_loss(
    method: str,
    closed: str,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The subscription went with the connection.

    ``send_command`` re-raises a ``ConnectionClosed`` from ``send`` unchanged;
    out of a waiter's cleanup it replaced the result the wait already had, e.g.
    turned a confirmed removal into a failed check. A socket closing before the
    answer fails the pending future with a connection error, which replaced
    the repository the HACS registration wait had found.
    """
    caplog.set_level(logging.DEBUG, logger=WS_LOGGER)
    async with connected_ws(core_client) as client:
        if method == "unsubscribe_events":
            subscription = await client.subscribe_events(EVENT)
        else:
            subscription, _queue = await client.subscribe_command(
                "subscribe_events", event_type=EVENT
            )
        assert _subscribed(hass) == 1

        if closed == "during_send":
            assert client.websocket is not None
            with patch.object(
                client.websocket, "send", side_effect=ConnectionClosed(None, None)
            ):
                await getattr(client, method)(subscription)
        else:
            with answers_unread(client):
                unsubscribe = asyncio.create_task(getattr(client, method)(subscription))
                # Sent: Core has dropped the subscription, its answer unread.
                await until(lambda: _subscribed(hass) == 0)
                abort_transport(client)
                async with asyncio.timeout(5):
                    _ = await unsubscribe

    assert [
        record.levelno
        for record in caplog.records
        if f"{method}({subscription})" in record.getMessage()
    ] == [logging.DEBUG]
