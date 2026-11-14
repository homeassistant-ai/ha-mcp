"""``ha_bulk_control`` through the component's ``bulk_call_service``, or per op.

When the component has ``bulk_call_service`` the server resolves every op's
service itself and sends one batch frame that confirms each op inline: no
operation ids to poll. Without it, or whenever the frame provably never
reached the component, each op is one REST POST. A batch frame that was sent
but never answered is reported partial and never dispatched again.

The services are handlers in Core's ``hass.services`` that switch entity
states as a device would (``_switched_states``); the states, events and
transitions are Core's.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools import device_control
from ha_mcp.tools.component_api import get_component_caps, invalidate_caps
from ha_mcp.tools.device_control import DeviceControlTools

from ._command_faults import dropped_on, pooled_socket, received
from ._operations import operations_without_listener  # noqa: F401
from ._switched_states import SwitchedStates

BULK = "ha_mcp_tools/bulk_call_service"
TWO_OPS = [
    {"entity_id": "light.a", "action": "on"},
    {"entity_id": "switch.b", "action": "on"},
]


@pytest.fixture
def devices(hass: HomeAssistant) -> SwitchedStates:
    hass.states.async_set("light.a", "off")
    hass.states.async_set("switch.b", "off")
    return SwitchedStates(hass)


def _posts(client: HomeAssistantClient) -> Any:
    return patch.object(client, "call_service", wraps=client.call_service)


async def _bulk(
    client: HomeAssistantClient, operations: list[dict[str, Any]]
) -> dict[str, Any]:
    return await DeviceControlTools(client).bulk_device_control(
        operations=[dict(op) for op in operations], parallel=True
    )


def _assert_ambiguous(resp: dict[str, Any]) -> None:
    """Every op dispatched but unconfirmed: the batch may have landed."""
    assert (resp["partial"], resp["total_operations"]) == (True, 2)
    assert [(r["partial"], r["status"]) for r in resp["results"]] == [
        (True, "dispatched_unconfirmed"),
        (True, "dispatched_unconfirmed"),
    ]


# --- the component serves the batch --------------------------------------------------


@pytest.mark.usefixtures("devices")
async def test_a_batch_is_one_frame_of_server_resolved_rows_confirmed_inline(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    with received(hass, BULK) as seen, _posts(component_core_client) as post:
        resp = await _bulk(component_core_client, TWO_OPS)

    assert (resp["successful_commands"], resp["failed_commands"]) == (2, 0)
    assert (resp["operation_ids"], resp["follow_up"]) == ([], None)
    first = resp["results"][0]
    assert (first["entity_id"], first["action"]) == ("light.a", "on")
    assert (first["command_sent"], first["confirmed"], first["final_state"]) == (
        True,
        True,
        "on",
    )
    assert first["verification_method"] == "component_state_change"
    post.assert_not_awaited()
    (frame,) = seen[BULK]
    assert (frame["parallel"], frame["wait"]) == (True, True)
    assert frame["operations"] == [
        {
            "domain": "light",
            "service": "turn_on",
            "service_data": {"entity_id": "light.a"},
            "entity_ids": ["light.a"],
            "expected_state": "on",
        },
        {
            "domain": "switch",
            "service": "turn_on",
            "service_data": {"entity_id": "switch.b"},
            "entity_ids": ["switch.b"],
            "expected_state": "on",
        },
    ]


@pytest.mark.usefixtures("devices")
async def test_an_explicit_zero_timeout_reaches_the_frame(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """0 is a timeout the caller chose, not a missing one."""
    ops = [{**op, "timeout_seconds": 0} for op in TWO_OPS]

    with received(hass, BULK) as seen:
        await _bulk(component_core_client, ops)
        await _bulk(component_core_client, [TWO_OPS[0], ops[1]])

    timeouts = [frame["timeout"] for frame in seen[BULK]]
    assert timeouts[0] == 0
    assert timeouts[1] > 0


@pytest.mark.usefixtures("devices")
async def test_a_target_core_does_not_have_fails_alone_and_is_not_sent_again(
    component_core_client: HomeAssistantClient,
) -> None:
    with _posts(component_core_client) as post:
        resp = await _bulk(
            component_core_client,
            [
                {"entity_id": "light.a", "action": "on"},
                {"entity_id": "light.ghost", "action": "on"},
            ],
        )

    assert (resp["successful_commands"], resp["failed_commands"]) == (1, 1)
    good, ghost = resp["results"]
    assert good["command_sent"] is True
    assert ghost.get("command_sent") is not True
    assert ghost["error"]["code"] == "ENTITY_NOT_FOUND"
    post.assert_not_awaited()


@pytest.mark.usefixtures("devices")
async def test_without_validate_first_a_missing_target_is_an_unconfirmed_command(
    component_core_client: HomeAssistantClient,
) -> None:
    resp = await _bulk(
        component_core_client,
        [{"entity_id": "light.ghost", "action": "on", "validate_first": False}],
    )

    assert (resp["successful_commands"], resp["failed_commands"]) == (1, 0)
    assert (resp["results"][0]["command_sent"], resp["results"][0]["partial"]) == (
        True,
        True,
    )


async def test_an_op_that_never_confirms_is_partial_and_not_sent_again(
    component_core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    devices.behaviour["switch.b"] = "stay"
    ops = [{**op, "timeout_seconds": 0.5} for op in TWO_OPS]

    with _posts(component_core_client) as post:
        resp = await _bulk(component_core_client, ops)

    assert resp["successful_commands"] == 2
    assert resp["results"][1]["partial"] is True
    post.assert_not_awaited()
    assert len(devices.calls) == 2


async def test_an_op_core_refused_in_the_batch_is_reported_not_sent_again(
    component_core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    devices.behaviour["switch.b"] = "fail_once"

    with _posts(component_core_client) as post:
        resp = await _bulk(component_core_client, TWO_OPS)

    assert (resp["successful_commands"], resp["failed_commands"]) == (1, 1)
    post.assert_not_awaited()


# --- nothing reached the component: one REST POST per op --------------------------------


@pytest.mark.usefixtures("devices")
async def test_without_the_component_each_op_is_one_rest_post(
    core_client: HomeAssistantClient,
) -> None:
    with _posts(core_client) as post:
        resp = await _bulk(core_client, TWO_OPS)

    assert resp["total_operations"] == 2
    assert post.await_count == 2


@pytest.mark.usefixtures("devices")
@pytest.mark.parametrize(
    "operations",
    [
        [
            {"entity_id": "light.a", "action": "on"},
            {"entity_id": "light.a", "action": "off"},
        ],
        [{"entity_id": "light.a", "action": "frobnicate"}],
    ],
    ids=["same-entity-twice", "unresolvable-action"],
)
async def test_a_batch_the_component_cannot_take_never_reaches_it(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    operations: list[dict[str, Any]],
) -> None:
    """One waiter per entity cannot confirm two ops on it; an action the server
    cannot resolve is the per-op path's error to report."""
    with received(hass, BULK) as seen:
        resp = await _bulk(component_core_client, operations)

    assert seen[BULK] == []
    assert resp["total_operations"] == len(operations)
    if operations[0]["action"] == "frobnicate":
        assert (resp["successful_commands"], resp["failed_commands"]) == (0, 1)


async def test_a_downgraded_component_is_dropped_and_each_op_posted(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    devices: SwitchedStates,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await get_component_caps(component_core_client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], BULK)

    with (
        patch.object(device_control, "invalidate_caps", wraps=invalidate_caps) as drop,
        _posts(component_core_client) as post,
    ):
        await _bulk(component_core_client, TWO_OPS)

    assert post.await_count == 2
    drop.assert_called_once_with(component_core_client)


async def test_a_batch_core_refuses_before_any_dispatch_is_posted_per_op(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Core has no ``switch.turn_on``: the component refuses the whole batch
    before it fires anything, so each op is posted once."""
    hass.states.async_set("light.a", "off")
    hass.states.async_set("switch.b", "off")
    devices = SwitchedStates(hass)
    hass.services.async_remove("switch", "turn_on")

    with _posts(component_core_client) as post:
        await _bulk(component_core_client, TWO_OPS)

    assert post.await_count == 2
    assert len(devices.calls) == 1


async def test_a_socket_that_cannot_connect_posts_each_op(
    component_core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    assert await get_component_caps(component_core_client) is not None
    await websocket_manager.disconnect()

    with (
        patch.object(
            HomeAssistantWebSocketClient,
            "connect",
            AsyncMock(side_effect=HomeAssistantConnectionError("refused")),
        ),
        _posts(component_core_client) as post,
    ):
        await _bulk(component_core_client, TWO_OPS)

    assert post.await_count == 2
    assert len(devices.calls) == 2


async def test_a_frame_never_sent_posts_each_op(
    component_core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    assert await get_component_caps(component_core_client) is not None
    socket = await pooled_socket(component_core_client)

    with (
        patch.object(
            socket,
            "send_command",
            side_effect=HomeAssistantCommandNotSent("WebSocket not authenticated"),
        ),
        _posts(component_core_client) as post,
    ):
        await _bulk(component_core_client, TWO_OPS)

    assert post.await_count == 2
    assert len(devices.calls) == 2


# --- the frame was sent: never dispatched again ------------------------------------------


async def test_a_socket_that_drops_after_the_send_is_partial_and_never_posted(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    devices: SwitchedStates,
) -> None:
    """Re-dispatching a batch that may have landed would fire every op twice."""
    assert await get_component_caps(component_core_client) is not None
    socket = await pooled_socket(component_core_client)

    with dropped_on(hass, BULK, socket), _posts(component_core_client) as post:
        resp = await _bulk(component_core_client, TWO_OPS)

    _assert_ambiguous(resp)
    post.assert_not_awaited()
    assert devices.calls == []
