"""``subscribe_command``: a subscription whose every event lands in one queue.

Used for streams such as ``render_template``, ``backup/subscribe_events`` and
HACS' ``hacs/subscribe``, where many events share the command's id. The queue
is registered before the command goes out, and a call that fails or is given
up leaves neither a queue nor a subscription behind.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

from ._client_spies import sent_commands
from ._command_faults import received
from ._held_command import held_command
from ._websocket_client_core import (
    EVENT,
    collect,
    registered,
    settled,
    short_cleanup,
    stalled_send,
    subscribed,
)
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


def _queued(queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    return [queue.get_nowait() for _ in range(queue.qsize())]


async def test_the_returned_id_streams_into_an_empty_queue(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The command's own fields reach Core, and Core's events for that
    subscription reach the returned queue."""
    async with connected_ws(core_client) as client:
        subscription, queue = await client.subscribe_command(
            "subscribe_events", event_type=EVENT
        )
        assert subscribed(hass) == 1
        assert queue.empty()

        hass.bus.async_fire(EVENT, {"n": 1})
        async with asyncio.timeout(5):
            event = await queue.get()

    assert event["id"] == subscription
    assert event["event"]["data"] == {"n": 1}


async def test_every_event_of_the_subscription_reaches_the_queue(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Not just the first: a one-shot event future would keep only that."""
    async with connected_ws(core_client) as client:
        _subscription, queue = await client.subscribe_command(
            "subscribe_events", event_type=EVENT
        )
        hass.bus.async_fire(EVENT, {"action": "registration"})
        hass.bus.async_fire(EVENT, {"action": "install"})
        async with asyncio.timeout(5):
            first, second = await queue.get(), await queue.get()

    assert first["event"]["data"] == {"action": "registration"}
    assert second["event"]["data"] == {"action": "install"}


async def test_an_event_before_the_ack_is_queued_not_taken_for_the_ack(
    core_client: HomeAssistantClient,
) -> None:
    """``render_template`` reports a template warning before its ack (#2522).

    Taken for the ack, the call failed with ``Command failed: {}``; with the
    queue registered only after the ack, the warning was lost.
    """
    async with connected_ws(core_client) as client:
        _subscription, queue = await client.subscribe_command(
            "render_template", template="{{ no_such_var }}", report_errors=True
        )
        # Answered in order: once the ping is back, so is the first render.
        assert await client.ping() is True

    # Core warns before the ack and again with the first render.
    assert [event["event"].get("level") for event in _queued(queue)] == [
        "WARNING",
        "WARNING",
        None,
    ]


async def test_a_refused_subscribe_raises_and_drops_its_queue(
    core_client: HomeAssistantClient,
) -> None:
    """A queue left behind would hold every later event with that id."""
    async with connected_ws(core_client) as client:
        with registered() as seen, pytest.raises(HomeAssistantCommandError):
            await client.subscribe_command("no_such/subscribe")

        assert await seen.alive() == []


async def test_the_ack_wait_is_wait_timeout_not_the_commands_own_timeout(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """``render_template`` has a ``timeout`` field of its own, which must not
    become how long the client waits for the ack."""
    async with connected_ws(core_client) as client:
        with spawned_by(client) as spawned:
            async with held_command(hass, "render_template"):
                call = asyncio.create_task(
                    client.subscribe_command(
                        "render_template",
                        template="{{ 1 }}",
                        timeout=30,
                        wait_timeout=0,
                    )
                )
                done, _ = await asyncio.wait({call}, timeout=5)

                assert call in done
                assert isinstance(call.exception(), TimeoutError)
            await settled(client, spawned)


async def _time_out_awaiting_the_ack(
    _hass: HomeAssistant, client: HomeAssistantWebSocketClient
) -> None:
    with pytest.raises(TimeoutError):
        await client.subscribe_command(
            "subscribe_events", event_type=EVENT, wait_timeout=0
        )


async def _cancel_awaiting_the_ack(
    hass: HomeAssistant, client: HomeAssistantWebSocketClient
) -> None:
    subscribe = asyncio.create_task(
        client.subscribe_command("subscribe_events", event_type=EVENT)
    )
    await until(lambda: subscribed(hass) == 1)
    subscribe.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await subscribe


async def _cancel_in_the_send(
    hass: HomeAssistant, client: HomeAssistantWebSocketClient
) -> None:
    with stalled_send(client) as written:
        subscribe = asyncio.create_task(
            client.subscribe_command("subscribe_events", event_type=EVENT)
        )
        await written.wait()
        await until(lambda: subscribed(hass) == 1)
        subscribe.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await subscribe


ABANDONS: dict[
    str, Callable[[HomeAssistant, HomeAssistantWebSocketClient], Awaitable[None]]
] = {
    "ack_timeout": _time_out_awaiting_the_ack,
    "cancelled_awaiting_ack": _cancel_awaiting_the_ack,
    "cancelled_in_send": _cancel_in_the_send,
}


@pytest.mark.parametrize("abandoned", list(ABANDONS))
async def test_an_abandoned_subscribe_leaves_no_queue_or_pending_future(
    abandoned: str,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue would buffer every later event with no reader, and the
    future would wait for an answer nobody reads."""
    short_cleanup(monkeypatch)
    async with connected_ws(core_client) as client:
        with spawned_by(client) as spawned:
            with registered() as seen, answers_unread(client):
                await ABANDONS[abandoned](hass, client)
                subscribe = (seen.pending[0], seen.queues[0])

                await collect()
                assert [ref() for ref in subscribe] == [None, None]
            await settled(client, spawned)


@pytest.mark.parametrize("abandoned", list(ABANDONS))
async def test_an_abandoned_subscribe_is_released_in_core(
    abandoned: str,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core may hold the subscription, and the caller never got its id.

    Its answers are left unread, so only the release made at once, not one
    on a late ack, can have reached Core.
    """
    short_cleanup(monkeypatch)
    async with connected_ws(core_client) as client:
        with spawned_by(client) as spawned:
            with (
                received(hass, "subscribe_events", "unsubscribe_events") as seen,
                answers_unread(client),
            ):
                await ABANDONS[abandoned](hass, client)

                await until(lambda: len(seen["unsubscribe_events"]) == 1)
                [subscribe] = seen["subscribe_events"]
                [release] = seen["unsubscribe_events"]
                assert release["subscription"] == subscribe["id"]
                assert subscribed(hass) == 0
            await settled(client, spawned)


async def test_a_late_refusal_of_an_abandoned_subscribe_is_not_released(
    read_only_client: HomeAssistantClient,
) -> None:
    """Core refused it, so there is nothing to release; a late ack, by
    contrast, is (``test_websocket_late_release.py``)."""
    async with connected_ws(read_only_client) as client:
        with spawned_by(client) as spawned, sent_commands(client) as sent:
            with pytest.raises(TimeoutError):
                await client.subscribe_command(
                    "subscribe_events", event_type=EVENT, wait_timeout=0
                )
            await settled(client, spawned)

    assert [call.args[0] for call in sent.await_args_list] == [
        "unsubscribe_events",
        "ping",
    ]


async def test_a_dropped_connection_forgets_subscribes_abandoned_on_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ids restart on the next socket: a late answer to an unrelated command
    with the same id must not release anything there."""
    short_cleanup(monkeypatch)
    close_gate(hass)
    try:
        async with connected_ws(core_client) as client:
            with answers_unread(client):
                with pytest.raises(TimeoutError):
                    await client.subscribe_command(
                        "subscribe_events", event_type=EVENT, wait_timeout=0
                    )
                abort_transport(client)
            await until(lambda: not client.is_connected)
            await client.disconnect()
            assert await client.connect() is True

            with spawned_by(client) as spawned, sent_commands(client) as sent:
                # The same id as the abandoned subscribe, answered after its
                # caller gave up.
                with pytest.raises(HomeAssistantCommandTimeout):
                    await client.send_command(
                        "execute_script", sequence=HELD_SCRIPT, _wait_timeout=0
                    )
                await open_gate(hass)
                await settled(client, spawned)

            assert [call.args[0] for call in sent.await_args_list] == [
                "execute_script",
                "ping",
            ]
    finally:
        await open_gate(hass)


async def test_a_failed_release_does_not_raise_and_drops_the_queue(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Released from a ``finally``: an error there would replace the result
    the caller already has."""
    async with connected_ws(core_client) as client:
        subscription, queue = await client.subscribe_command(
            "subscribe_events", event_type=EVENT
        )
        # Core's answer to the release comes too late.
        monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 0)

        await client.release_subscription(subscription)

        with pytest.raises(asyncio.QueueShutDown):
            queue.get_nowait()
        await until(lambda: subscribed(hass) == 0)


async def test_unsubscribe_command_releases_the_queue_and_cores_subscription(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    async with connected_ws(core_client) as client:
        subscription, queue = await client.subscribe_command(
            "subscribe_events", event_type=EVENT
        )

        await client.unsubscribe_command(subscription)

        assert subscribed(hass) == 0
        with pytest.raises(asyncio.QueueShutDown):
            queue.get_nowait()


async def test_a_dropped_connection_wakes_a_reader_waiting_on_the_queue(
    core_client: HomeAssistantClient,
) -> None:
    """Without it the reader hangs on a queue nothing will fill again."""
    async with connected_ws(core_client) as client:
        _subscription, queue = await client.subscribe_command(
            "subscribe_events", event_type=EVENT
        )
        reader = asyncio.create_task(queue.get())
        await asyncio.sleep(0)

        abort_transport(client)

        async with asyncio.timeout(5):
            with pytest.raises(asyncio.QueueShutDown):
                _ = await reader
