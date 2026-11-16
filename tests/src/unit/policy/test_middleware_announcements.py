"""A held call is announced on Home Assistant's event bus, once per request.

The gate fires through the server's own client into a real ``hass``, whose
bus is where an automation reads the request. ``ensure_decisions_listener``
is the server's hook that opens the channel an automation answers on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import async_capture_events

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.events import APPROVAL_REQUESTED_EVENT
from ha_mcp.policy.middleware import PolicyMiddleware
from ha_mcp.policy.model import Policy, Rule

from ._middleware_core import (
    START,
    QueueWatch,
    error_body,
    expire_on_arrival,
    gate,
    set_policy_clock,
    tool_call,
)

LOCK = {"domain": "lock"}
GATED = Policy(rules=[Rule(tool_name="ha_call_service")])
BULK = Policy(rules=[Rule(tool_name="ha_bulk_control")])


async def _held(
    mw: PolicyMiddleware,
    name: str = "ha_call_service",
    args: dict[str, Any] | None = None,
) -> str:
    """Make one call the gate holds; return the token it reports."""
    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(tool_call(name, args or LOCK), AsyncMock())
    token: str = error_body(ei.value)["token"]
    return token


def _client_after_one_failed_lookup(
    client: HomeAssistantClient,
) -> Callable[[], HomeAssistantClient]:
    """The server's client lookup, failing the first time as before startup ends."""
    lookups: list[int] = []

    def lookup() -> HomeAssistantClient:
        lookups.append(1)
        if len(lookups) == 1:
            raise RuntimeError("client not ready")
        return client

    return lookup


async def test_gated_call_announces_the_pending_request(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The whole point of #2502: the queue must be visible outside the UI."""
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    queue = ApprovalQueue()

    token = await _held(gate(GATED, queue, get_client=lambda: core_client))
    await hass.async_block_till_done()

    (event,) = fired
    assert event.data["tool_name"] == "ha_call_service"
    assert event.data["args"] == LOCK
    assert event.data["matched_rule"]["tool_name"] == "ha_call_service"
    assert event.data["token"] == token
    assert queue.get(token) is not None


async def test_a_shared_pending_row_is_announced_once(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A later identical call joins the existing row (``find_or_create``).

    The user was already told about that request; a second event would
    duplicate the notification with the same token.
    """
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    queue = ApprovalQueue()
    mw = gate(GATED, queue, get_client=lambda: core_client)

    first = await _held(mw)
    assert await _held(mw) == first
    await hass.async_block_till_done()

    assert len(fired) == 1


async def test_a_failing_announcement_still_gates_the_call(
    unreachable_client: HomeAssistantClient,
) -> None:
    """Delivery is best effort; the gate is not."""
    call_next = AsyncMock()
    mw = gate(GATED, ApprovalQueue(), get_client=lambda: unreachable_client)

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(tool_call("ha_call_service", LOCK), call_next)

    assert error_body(ei.value)["error"]["code"] == "USER_APPROVAL_REQUIRED"
    call_next.assert_not_called()


async def test_a_client_that_cannot_be_resolved_does_not_burn_the_announcement(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A failed client lookup must leave the entry announceable.

    ``mark_notified`` is one-shot, so consuming it before the client is in
    hand would spend the entry's only announcement on a client that never
    materialised.
    """
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    mw = gate(
        GATED,
        ApprovalQueue(),
        get_client=_client_after_one_failed_lookup(core_client),
    )

    token = await _held(mw)
    await _held(mw)
    await hass.async_block_till_done()

    (event,) = fired
    assert event.data["token"] == token


async def test_a_reissued_entry_is_announced_with_its_new_token(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry whose TTL ran out during the wait is reissued under a new token.

    The token in the first event is dead by then, so an automation holding it
    has nothing to act on; the replacement has to be announced too.
    """
    set_policy_clock(monkeypatch, START)
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    queue = ApprovalQueue()
    QueueWatch(queue, monkeypatch, after=expire_on_arrival(monkeypatch))

    token = await _held(gate(GATED, queue, get_client=lambda: core_client))
    await hass.async_block_till_done()

    first, second = (event.data["token"] for event in fired)
    assert first != second
    assert second == token
    assert queue.get(token) is not None


async def test_each_replacement_row_in_a_burst_is_announced(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waiter that loses the claim mints a new row, and announces it.

    Otherwise the replacement row exists in the settings UI but never reaches
    the bus, and a user approving from a notification sees only the first.
    """
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch)
    mw = gate(GATED, queue, wait_seconds=5, get_client=lambda: core_client)
    args = {"domain": "lock", "service": "unlock"}
    call_next = AsyncMock(return_value="ok")

    async def call() -> None:
        try:
            await mw.on_call_tool(tool_call("ha_call_service", args), call_next)
        except ToolError:
            pass  # the caller that loses the claim is told to get a new approval

    async def approve_the_shared_row() -> None:
        (entry, _) = await watch.until(2)
        queue.approve(entry.token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)
        tg.start_soon(approve_the_shared_row)
    await hass.async_block_till_done()

    announced = [event.data["token"] for event in fired]
    assert len(set(announced)) == 2, "the replacement row was never announced"
    (leftover,) = queue.list_pending()
    assert announced[1] == leftover.token


async def test_a_selector_call_is_announced_as_single_use(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A dynamic-selector gate announces no expiry it cannot honour."""
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)

    await _held(
        gate(BULK, ApprovalQueue(), get_client=lambda: core_client),
        "ha_bulk_control",
        {"selector": {"domain": "light"}},
    )
    await hass.async_block_till_done()

    (event,) = fired
    assert event.data["single_use"] is True
    assert "expires_at" not in event.data


async def test_a_removed_selector_entry_is_never_announced(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The single-use entry is gone by the time the retry branch runs.

    Announcing there would hand an automation a token the queue no longer
    knows, reachable whenever the first announcement found no client.
    """
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    queue = ApprovalQueue()
    mw = gate(
        BULK,
        queue,
        get_client=_client_after_one_failed_lookup(core_client),
    )

    await _held(mw, "ha_bulk_control", {"selector": {"domain": "light"}})
    await hass.async_block_till_done()

    assert fired == []
    assert queue.list_pending() == []


async def test_the_decision_channel_opens_before_the_request_is_announced(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A response to an event nobody is subscribed to is lost.

    An automation that answers the moment the notification lands would
    otherwise race the subscription.
    """
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    announced_when_opened: list[int] = []

    async def ensure() -> None:
        await hass.async_block_till_done()
        announced_when_opened.append(len(fired))

    mw = gate(
        GATED,
        ApprovalQueue(),
        get_client=lambda: core_client,
        ensure_decisions_listener=ensure,
    )

    await _held(mw)
    await hass.async_block_till_done()

    assert announced_when_opened[0] == 0
    assert len(fired) == 1


async def test_a_failing_decision_channel_still_announces(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A request answerable only in the settings UI beats no request at all."""
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)

    async def ensure() -> None:
        raise RuntimeError("no websocket")

    mw = gate(
        GATED,
        ApprovalQueue(),
        get_client=lambda: core_client,
        ensure_decisions_listener=ensure,
    )

    await _held(mw)
    await hass.async_block_till_done()

    assert len(fired) == 1
    assert "approval-response channel" in caplog.text


async def test_an_identical_retry_tries_the_channel_again(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A failed or dropped subscription is recoverable by the retry that follows.

    Identical calls share one row, announced once. Were channel recovery tied
    to that one-shot, the retry would find the row announced and never try,
    leaving recovery to some future, different request.
    """
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    attempts: list[str] = []

    async def ensure() -> None:
        attempts.append("tried")
        if len(attempts) == 1:
            raise RuntimeError("no websocket")

    mw = gate(
        GATED,
        ApprovalQueue(),
        get_client=lambda: core_client,
        ensure_decisions_listener=ensure,
    )

    await _held(mw)
    # Counted per call: one call already passes the announce path twice.
    after_first_call = len(attempts)
    await _held(mw)
    await hass.async_block_till_done()

    assert len(attempts) > after_first_call
    assert len(fired) == 1


async def test_a_call_cancelled_while_the_channel_opens_leaves_its_request_announceable(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The channel is opened before the one-shot announcement latch is taken.

    Opening it can take seconds (connect, authenticate, wait for the lock).
    A call cancelled in that window after taking the latch would leave its
    row marked announced with nothing fired, and every identical retry
    reuses that row and stays silent.
    """
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    first_call = anyio.CancelScope()

    async def ensure() -> None:
        if not first_call.cancel_called:
            first_call.cancel()
            await anyio.lowlevel.checkpoint()

    mw = gate(
        GATED,
        ApprovalQueue(),
        get_client=lambda: core_client,
        ensure_decisions_listener=ensure,
    )

    with first_call:
        await mw.on_call_tool(tool_call("ha_call_service", LOCK), AsyncMock())
    assert first_call.cancelled_caught
    token = await _held(mw)
    await hass.async_block_till_done()

    (event,) = fired
    assert event.data["token"] == token
