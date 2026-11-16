"""``subscribe_events`` on Core's ``websocket_api``, and releasing what it leaves.

Core identifies a subscription by the id of the ``subscribe_events`` command
and answers it with ``result: null`` (#1375). A caller that goes away before
the answer never learns that id, so the client releases the subscription
itself: within a deadline, finished before the cancellation reaches the
caller, and logged at a level that says whether anything was left open.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import patch

import anyio
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import async_fire_time_changed

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

from ._client_spies import sent_commands
from ._held_command import held_command
from ._websocket_client_core import (
    EVENT,
    WS_LOGGER,
    client_log,
    collect,
    registered,
    released,
    settled,
    short_cleanup,
    stalled_send,
    subscribed,
)
from ._ws_transport import answers_unread, connected_ws, spawned_by, until


async def test_the_returned_id_is_the_subscription_core_releases(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core answers ``result: null``; the id is the command's own. Looking for
    it in the result raised on every call and left every async operation
    pending until it timed out."""
    async with connected_ws(core_client) as client:
        subscription = await client.subscribe_events(EVENT)
        assert subscribed(hass) == 1

        await client.unsubscribe_events(subscription)

        assert subscribed(hass) == 0


async def test_core_subscribes_to_the_requested_event_type_only(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    every_event = subscribed(hass, "*")
    async with connected_ws(core_client) as client:
        await client.subscribe_events(EVENT)

        assert subscribed(hass) == 1
        assert subscribed(hass, "*") == every_event


async def test_no_event_type_subscribes_to_every_event(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    every_event = subscribed(hass, "*")
    async with connected_ws(core_client) as client:
        await client.subscribe_events()

        assert subscribed(hass, "*") == every_event + 1


async def test_cores_refusal_raises_with_its_reason(
    read_only_client: HomeAssistantClient,
) -> None:
    """A non-admin may not subscribe to an arbitrary event type."""
    async with connected_ws(read_only_client) as client:
        with pytest.raises(HomeAssistantCommandError) as exc:
            await client.subscribe_events(EVENT)

    assert str(exc.value) == "subscribe_events failed: Unauthorized"
    assert exc.value.code == "unauthorized"


async def test_subscribing_on_a_closed_socket_raises_a_connection_error(
    core_client: HomeAssistantClient,
) -> None:
    async with connected_ws(core_client) as client:
        pass

    with pytest.raises(HomeAssistantConnectionError):
        await client.subscribe_events(EVENT)


async def test_an_unanswered_subscribe_gives_up_and_leaves_no_pending_future(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without the deadline a stuck subscribe would hang its caller, and a
    leftover entry would stay in the client for the life of the socket."""
    # A captured DEBUG record of the cleanup's own timeout keeps that error,
    # and through its traceback the subscribe's frames and future.
    caplog.set_level(logging.INFO, logger=WS_LOGGER)
    short_cleanup(monkeypatch)
    async with connected_ws(core_client) as client:
        with registered() as seen, ExitStack() as unread:
            unread.enter_context(answers_unread(client))
            subscribe = asyncio.create_task(client.subscribe_events(EVENT))
            # Entered after: the failed subscribe must not be kept here.
            with spawned_by(client) as spawned:
                await until(lambda: subscribed(hass) == 1)
                # The subscribe waits 30 s for its answer.
                async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
                with pytest.raises(TimeoutError):
                    _ = await subscribe
                del subscribe

                # Only the subscribe's own: the release is a command of its own.
                await released(seen.pending[0])
                unread.close()
                await settled(client, spawned)


async def _cancel_awaiting_the_ack(
    hass: HomeAssistant, client: HomeAssistantWebSocketClient
) -> None:
    """Cancel a subscribe Core has registered but whose answer is unread."""
    with answers_unread(client):
        subscribe = asyncio.create_task(client.subscribe_events(EVENT))
        await until(lambda: subscribed(hass) == 1)
        subscribe.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await subscribe


async def _cancel_in_the_send(
    hass: HomeAssistant, client: HomeAssistantWebSocketClient
) -> None:
    """Cancel a subscribe whose frame is written but whose send has not returned."""
    with stalled_send(client) as written:
        subscribe = asyncio.create_task(client.subscribe_events(EVENT))
        await written.wait()
        await until(lambda: subscribed(hass) == 1)
        subscribe.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await subscribe


CANCELLATIONS = {
    "awaiting_ack": _cancel_awaiting_the_ack,
    "in_send": _cancel_in_the_send,
}


@pytest.mark.parametrize("cancelled", list(CANCELLATIONS))
async def test_a_cancelled_subscribe_leaves_no_pending_future(
    cancelled: str,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled caller (a tool leg cancelled by its parent) must not leave
    an entry nothing will ever resolve."""
    short_cleanup(monkeypatch)
    async with connected_ws(core_client) as client:
        with spawned_by(client) as spawned:
            with registered() as seen:
                await CANCELLATIONS[cancelled](hass, client)
                subscribe_future = seen.pending[0]

            # Only the subscribe's own: the release is a command of its own.
            await collect()
            assert subscribe_future() is None
            await settled(client, spawned)


@pytest.mark.parametrize("cancelled", list(CANCELLATIONS))
async def test_a_cancelled_subscribe_releases_cores_subscription(
    cancelled: str,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frame was on the wire, so Core may hold the subscription; only the
    client still knows its id. Left open, every later event arrives once per
    orphan on a socket this process keeps using."""
    short_cleanup(monkeypatch)
    async with connected_ws(core_client) as client:
        with spawned_by(client) as spawned:
            await CANCELLATIONS[cancelled](hass, client)

            await until(lambda: subscribed(hass) == 0)
            await settled(client, spawned)


@pytest.mark.parametrize("cancellation", ["task", "cancel_scope"])
async def test_a_cancelled_subscribe_finishes_its_release_first(
    cancellation: str,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The release has to wait for Core's answer, and the caller's
    cancellation must not cut it off there; the cancellation still reaches
    the caller once the release is done.

    A cancel scope (the approval listener's setup budget) re-delivers its
    cancellation on every tick, which a plain task cancel does not.
    """
    caplog.set_level(logging.DEBUG, logger=WS_LOGGER)
    async with connected_ws(core_client) as client:
        with spawned_by(client) as spawned:
            async with held_command(hass, "subscribe_events", first_only=True) as held:
                if cancellation == "task":
                    subscribe = asyncio.create_task(client.subscribe_events(EVENT))
                    await held.arrived.wait()
                    subscribe.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        _ = await subscribe
                else:
                    with anyio.CancelScope() as scope:
                        canceller = asyncio.create_task(held.arrived.wait())
                        canceller.add_done_callback(lambda _task: scope.cancel())
                        await client.subscribe_events(EVENT)
                    assert scope.cancelled_caught

                # Core never registered it, and said so.
                assert client_log(caplog, "has no such subscription") == [logging.DEBUG]
            await settled(client, spawned)


async def test_a_cancel_scope_cannot_cut_short_stopping_a_late_release(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release past its deadline is stopped and collected before the call
    returns; a reasserting cancel scope must not interrupt that collection,
    or the release goes on touching the client after the call is over."""
    short_cleanup(monkeypatch)
    async with connected_ws(core_client) as client:
        with spawned_by(client) as spawned:
            with answers_unread(client):
                with anyio.CancelScope() as scope:
                    canceller = asyncio.create_task(
                        until(lambda: subscribed(hass) == 1)
                    )
                    canceller.add_done_callback(lambda _task: scope.cancel())
                    await client.subscribe_events(EVENT)

                release = spawned.tasks[0]
                assert release.done()
            await settled(client, spawned)


async def test_a_release_past_its_deadline_is_stopped_before_returning(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Left running, it would read the client's state whenever it got there,
    not the state it was cleaning up."""
    async with connected_ws(core_client) as client:
        subscription, _queue = await client.subscribe_command(
            "subscribe_events", event_type=EVENT
        )
        short_cleanup(monkeypatch)
        with spawned_by(client) as spawned:
            with answers_unread(client):
                await client.release_subscription(subscription)

                release = spawned.tasks[0]
                assert release.done()
            await settled(client, spawned)


async def test_a_release_after_the_socket_is_gone_sends_nothing(
    core_client: HomeAssistantClient,
) -> None:
    """The subscription went with the connection, and sending would raise in
    a cleanup nobody reads."""
    async with connected_ws(core_client) as client:
        subscription, _queue = await client.subscribe_command(
            "subscribe_events", event_type=EVENT
        )

    with sent_commands(client) as sent:
        await client.release_subscription(subscription)

    sent.assert_not_awaited()


async def test_a_lost_transport_during_a_release_is_not_a_warning(
    core_client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The connection going away is the cleanup."""
    caplog.set_level(logging.DEBUG, logger=WS_LOGGER)
    async with connected_ws(core_client) as client:
        subscription, _queue = await client.subscribe_command(
            "subscribe_events", event_type=EVENT
        )
        assert client.websocket is not None
        with patch.object(
            client.websocket, "send", side_effect=OSError("connection reset")
        ):
            await client.release_subscription(subscription)

    assert client_log(caplog, f"abandoned subscribe_events({subscription})") == [
        logging.DEBUG
    ]


async def test_any_other_refusal_of_a_release_is_a_warning(
    core_client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Only ``not_found`` means "already gone"; a refused release leaves the
    subscription open, and at debug level that leak would go unnoticed."""
    caplog.set_level(logging.DEBUG, logger=WS_LOGGER)
    async with connected_ws(core_client) as client:
        # Core refuses a negative id as malformed.
        await client.release_subscription(-1)

    assert client_log(caplog, "abandoned subscribe_events(-1)") == [logging.WARNING]
