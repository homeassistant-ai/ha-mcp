"""The component's other state readers serve the live ``last_reported`` (#3086).

An entity that reports an unchanged state moves ``last_reported`` on Core's live
``State`` only; Core's cached serialization keeps the first value it rendered
(home-assistant/core#181392). The system snapshot and the ``call_service`` /
``bulk_call_service`` transitions read states through the same serializer as
``ha_get_state``.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.websocket_api.constants import (
    WS_BULK_CALL_SERVICE,
    WS_CALL_SERVICE,
    WS_SYSTEM_SNAPSHOT,
)

from ._component_core import ws_result

ENTITY_ID = "sensor.plug_power"


def _re_reported(hass: HomeAssistant) -> str:
    """Report ``0`` twice a minute apart, after Core rendered the first report.
    Returns the live ``last_reported``."""
    hass.states.async_set(ENTITY_ID, "0", timestamp=1_800_000_000.0)
    first = hass.states.get(ENTITY_ID)
    assert first is not None
    first.as_dict()
    hass.states.async_set(ENTITY_ID, "0", timestamp=1_800_000_060.0)
    live = hass.states.get(ENTITY_ID)
    assert live is not None
    assert live.as_dict()["last_reported"] != live.last_reported.isoformat()
    return live.last_reported.isoformat()


@pytest.fixture
def noop(hass: HomeAssistant) -> None:
    """``test.noop``: a service that changes nothing."""

    async def handle(call: ServiceCall) -> None:
        return None

    hass.services.async_register(
        "test", "noop", handle, supports_response=SupportsResponse.NONE
    )


async def test_the_snapshot_serves_the_live_last_reported(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    live = _re_reported(hass)

    result = await ws_result(component_ws, WS_SYSTEM_SNAPSHOT)

    (state,) = [s for s in result["states"] if s["entity_id"] == ENTITY_ID]
    assert state["last_reported"] == live


@pytest.mark.usefixtures("noop")
async def test_a_call_transition_carries_the_live_last_reported(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    live = _re_reported(hass)

    result = await ws_result(
        component_ws,
        WS_CALL_SERVICE,
        domain="test",
        service="noop",
        entity_ids=[ENTITY_ID],
        wait=False,
    )

    (transition,) = result["transitions"]
    assert transition["old_state"]["last_reported"] == live
    assert transition["new_state"]["last_reported"] == live


@pytest.mark.usefixtures("noop")
async def test_a_bulk_transition_carries_the_live_last_reported(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    live = _re_reported(hass)

    result: dict[str, Any] = await ws_result(
        component_ws,
        WS_BULK_CALL_SERVICE,
        operations=[{"domain": "test", "service": "noop", "entity_ids": [ENTITY_ID]}],
        wait=False,
    )

    (op,) = result["operations"]
    (transition,) = op["transitions"]
    assert transition["old_state"]["last_reported"] == live
    assert transition["new_state"]["last_reported"] == live
