"""``bulk_device_control`` through the component's ``bulk_call_service`` on Core.

The server's own client runs against a real ``hass`` with the ``ha_mcp_tools/*``
commands registered, so the batch frame the server builds is validated and run
by the component and the per-op results it maps come from Core's own service
calls and state machine. The lights and the switch are devices below Core's own
``light`` and ``switch`` integrations (``_switched_states``). The legacy per-op
dispatch is spied on and must never run when the component serves the batch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.device_control import DeviceControlTools

from ._component_core import ws_call
from ._switched_states import UNAVAILABLE, set_up_devices


@contextmanager
def _no_legacy_dispatch(client: HomeAssistantClient) -> Iterator[None]:
    """The legacy per-op dispatch and its pre-validation must not run."""
    with (
        patch.object(
            client, "call_service", AsyncMock(wraps=client.call_service)
        ) as call_service,
        patch.object(
            client, "get_entity_state", AsyncMock(wraps=client.get_entity_state)
        ) as get_entity_state,
    ):
        yield
    call_service.assert_not_awaited()
    get_entity_state.assert_not_awaited()


async def _bulk(
    client: HomeAssistantClient, operations: list[dict[str, Any]]
) -> dict[str, Any]:
    with _no_legacy_dispatch(client):
        result: dict[str, Any] = await DeviceControlTools(client).bulk_device_control(
            operations=operations, parallel=True
        )
    return result


async def _unavailable_lamp(hass: HomeAssistant, behaviour: str) -> None:
    """``light.b`` out of Core's reach, doing ``behaviour`` when a call names it."""
    devices = await set_up_devices(hass, {"light.b": UNAVAILABLE})
    devices.behaviour["light.b"] = behaviour


async def test_a_confirmed_batch_maps_to_the_legacy_bulk_shape(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    devices = await set_up_devices(hass, {"light.a": "off", "switch.b": "off"})

    resp = await _bulk(
        component_core_client,
        [
            {"entity_id": "light.a", "action": "on"},
            {"entity_id": "switch.b", "action": "on"},
        ],
    )

    assert resp["total_operations"] == 2
    assert resp["successful_commands"] == 2
    assert resp["failed_commands"] == 0
    assert resp["operation_ids"] == []
    assert resp["follow_up"] is None
    light_res, switch_res = resp["results"]
    assert light_res["entity_id"] == "light.a"
    assert light_res["command_sent"] is True
    assert light_res["confirmed"] is True
    assert light_res["final_state"] == "on"
    assert light_res["service_call"] == {
        "domain": "light",
        "service": "turn_on",
        "data": {"entity_id": "light.a"},
    }
    assert switch_res["confirmed"] is True
    assert sorted(devices.targets()) == ["light.a", "switch.b"]


async def test_a_nonexistent_entity_in_a_batch_fails_without_waiting_for_it(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The missing target is left out of the shared wait: the batch returns at
    once instead of running the missing op's 10 s wait to its end."""
    devices = await set_up_devices(hass, {"light.a": "off"})

    async with asyncio.timeout(5):
        resp = await _bulk(
            component_core_client,
            [
                {"entity_id": "light.a", "action": "on"},
                {"entity_id": "light.missing", "action": "on"},
            ],
        )

    assert resp["total_operations"] == 2
    assert resp["successful_commands"] == 1
    assert resp["failed_commands"] == 1
    valid_res, missing_res = resp["results"]
    assert valid_res["entity_id"] == "light.a"
    assert valid_res["confirmed"] is True
    assert missing_res["entity_id"] == "light.missing"
    assert missing_res["error"]["code"] == "ENTITY_NOT_FOUND"
    # Both ops are dispatched; Core's light service hands the unmatched
    # target to no device.
    assert devices.targets() == ["light.a"]


async def test_an_unavailable_entity_that_never_reconnects_is_reported_unavailable(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    devices = await set_up_devices(hass, {"light.b": UNAVAILABLE})

    resp = await _bulk(
        component_core_client,
        [{"entity_id": "light.b", "action": "on", "timeout_seconds": 0.05}],
    )

    assert resp["failed_commands"] == 1
    (result,) = resp["results"]
    assert result["error"]["code"] == "ENTITY_UNAVAILABLE"
    # The caller can tell this apart from an op that never dispatched; Core
    # skipped the unavailable light itself.
    assert result.get("dispatched") is True
    assert devices.calls == []


async def test_a_comma_separated_target_is_not_reported_missing(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """``light.a,light.b`` is no literal entity, so its empty pre-state is no
    proof that an entity is missing."""
    devices = await set_up_devices(hass, {"light.a": "on", "light.b": "on"})

    resp = await _bulk(
        component_core_client, [{"entity_id": "light.a,light.b", "action": "off"}]
    )

    assert resp["failed_commands"] == 0
    (result,) = resp["results"]
    assert "error" not in result
    assert sorted(devices.targets()) == ["light.a", "light.b"]


async def test_an_unavailable_target_removed_during_the_call_is_reported_not_found(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await _unavailable_lamp(hass, "remove")

    resp = await _bulk(
        component_core_client,
        [{"entity_id": "light.b", "action": "on", "timeout_seconds": 0.05}],
    )

    assert resp["failed_commands"] == 1
    (result,) = resp["results"]
    assert result["error"]["code"] == "ENTITY_NOT_FOUND"


async def test_an_unavailable_target_back_in_another_state_is_not_unavailable(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """It reconnected to ``off``, not the ``on`` the call asked for: the op is
    dispatched but unconfirmed, not a false ENTITY_UNAVAILABLE."""
    await _unavailable_lamp(hass, "off")

    resp = await _bulk(
        component_core_client,
        [{"entity_id": "light.b", "action": "on", "timeout_seconds": 0.05}],
    )

    assert resp["failed_commands"] == 0
    (result,) = resp["results"]
    assert "error" not in result
    assert result["confirmed"] is False
    assert result["partial"] is True


async def test_an_unavailable_target_that_reconnects_and_turns_on_is_confirmed(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await _unavailable_lamp(hass, "on")

    resp = await _bulk(
        component_core_client, [{"entity_id": "light.b", "action": "on"}]
    )

    assert resp["failed_commands"] == 0
    assert resp["successful_commands"] == 1
    (result,) = resp["results"]
    assert result["confirmed"] is True
    assert result["final_state"] == "on"


async def test_a_batch_with_an_ha_mcp_tools_op_dispatches_nothing(
    hass: HomeAssistant, component_ws: Any
) -> None:
    """Every op is checked before any runs: the valid op ahead of the refused
    one never fires, and no confirmation listener is left on the bus."""
    devices = await set_up_devices(hass, {"light.a": "off"})
    listeners = hass.bus.async_listeners()

    reply = await ws_call(
        component_ws,
        "ha_mcp_tools/bulk_call_service",
        operations=[
            {
                "domain": "light",
                "service": "turn_on",
                "service_data": {"entity_id": "light.a"},
                "entity_ids": ["light.a"],
            },
            {"domain": "ha_mcp_tools", "service": "get_caller_token"},
        ],
    )

    assert reply["success"] is False
    assert "not callable" in reply["error"]["message"]
    assert devices.calls == []
    assert hass.bus.async_listeners() == listeners
