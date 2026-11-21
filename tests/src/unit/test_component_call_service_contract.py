"""``ha_call_service`` served by the component's ``call_service`` command.

The tool runs on the server's own client against a real ``hass`` with the
``ha_mcp_tools/*`` commands registered, so a drift between the transition the
component reports and what the tool makes of it fails here. The target is a
state set in Core and the service is the test's own ``test.*`` service, which
changes that state as a device would. What the component sees while it waits
is read off Core's bus: a confirmable target has a ``state_changed`` listener
registered before the dispatch, a nonexistent one never gets one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant.common import MockConfigEntry, async_capture_events
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, TOOLS_ENTRY_TITLE
from custom_components.ha_mcp_tools.websocket_api.constants import WS_CALL_SERVICE
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.tools import tools_service
from ha_mcp.tools.tools_service import ServiceTools

from ._component_core import ws_call, ws_result

# One dispatch as the service saw it: its data, and how many state_changed
# listeners Core had while it ran.
Dispatch = tuple[dict[str, Any], int]


def _state_listeners(hass: HomeAssistant) -> int:
    return hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)


def _device_service(
    hass: HomeAssistant, service: str, device: Callable[[], object] | None = None
) -> list[Dispatch]:
    """``test.<service>``: records each dispatch, then lets ``device`` react."""
    dispatches: list[Dispatch] = []

    async def handle(call: ServiceCall) -> None:
        dispatches.append((dict(call.data), _state_listeners(hass)))
        if device is not None:
            device()

    hass.services.async_register("test", service, handle)
    return dispatches


@pytest.fixture
def tools(component_core_client: HomeAssistantClient) -> ServiceTools:
    return ServiceTools(component_core_client, None)


@pytest.fixture
def short_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A confirmation that never comes lapses at once instead of after 10 s."""
    monkeypatch.setattr(tools_service, "CONFIRMATION_TIMEOUT_S", 0.05)


async def test_a_confirmed_call_reports_the_real_transition(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    hass.states.async_set("light.a", "off", {"brightness": 100})
    dispatches = _device_service(
        hass,
        "turn_on",
        lambda: hass.states.async_set("light.a", "on", {"brightness": 255}),
    )
    listening = _state_listeners(hass)

    resp = await tools.ha_call_service(
        domain="test", service="turn_on", entity_id="light.a", data={"brightness": 255}
    )

    assert resp["success"] is True
    assert (resp["domain"], resp["service"], resp["entity_id"]) == (
        "test",
        "turn_on",
        "light.a",
    )
    assert resp["message"] == "Successfully executed test.turn_on"
    assert resp["verified_state"] == "on"
    assert "partial" not in resp
    (record,) = resp["result"]
    assert (record["entity_id"], record["state"]) == ("light.a", "on")
    assert record["attributes"]["brightness"] == 255
    assert dispatches == [({"brightness": 255, "entity_id": "light.a"}, listening + 1)]


async def test_the_reserved_domain_is_refused_before_any_frame(
    hass: HomeAssistant,
    tools: ServiceTools,
    component_core_client: HomeAssistantClient,
) -> None:
    """The server refuses ``ha_mcp_tools`` before the component sees a frame."""
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    ws = await get_websocket_client(
        url=component_core_client.base_url,
        token=component_core_client.token,
        verify_ssl=component_core_client.verify_ssl,
    )

    with (
        patch.object(ws, "send_command", wraps=ws.send_command) as sent,
        pytest.raises(ToolError) as exc,
    ):
        await tools.ha_call_service(domain="ha_mcp_tools", service="get_caller_token")

    assert "ha_mcp_tools" in str(exc.value)
    assert WS_CALL_SERVICE not in [c.args[0] for c in sent.await_args_list]
    assert events == []


async def test_a_nonexistent_target_fails_at_once_without_a_wait(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """An invented entity ID (``light.cuisine`` for a kitchen light) used to wait
    the full 10 s for an event that cannot come. Home Assistant still runs the
    service (it no-ops an unmatched target); no listener is registered for it."""
    dispatches = _device_service(hass, "turn_on")
    listening = _state_listeners(hass)

    with pytest.raises(ToolError) as exc:
        await tools.ha_call_service(
            domain="test", service="turn_on", entity_id="light.cuisine"
        )

    assert "ENTITY_NOT_FOUND" in str(exc.value)
    assert "light.cuisine" in str(exc.value)
    assert [seen for _, seen in dispatches] == [listening]


async def test_an_unavailable_target_that_reconnects_is_a_plain_success(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """A Zigbee light that wakes on the command it is sent must confirm, never
    read as ENTITY_UNAVAILABLE."""
    hass.states.async_set("light.b", "unavailable")
    dispatches = _device_service(
        hass, "turn_on", lambda: hass.states.async_set("light.b", "on")
    )
    listening = _state_listeners(hass)

    resp = await tools.ha_call_service(
        domain="test", service="turn_on", entity_id="light.b"
    )

    assert resp["success"] is True
    assert resp["verified_state"] == "on"
    assert "partial" not in resp
    assert [seen for _, seen in dispatches] == [listening + 1]


@pytest.mark.usefixtures("short_wait")
async def test_an_unavailable_target_that_stays_down_reports_unavailable(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """Not the generic "could not be verified" wording, and the error says the
    command went out, so a caller does not blindly retry a non-idempotent one."""
    hass.states.async_set("light.b", "unavailable")
    dispatches = _device_service(hass, "turn_on")
    listening = _state_listeners(hass)

    with pytest.raises(ToolError) as exc:
        await tools.ha_call_service(
            domain="test", service="turn_on", entity_id="light.b"
        )

    assert "ENTITY_UNAVAILABLE" in str(exc.value)
    assert "light.b" in str(exc.value)
    assert '"dispatched": true' in str(exc.value)
    assert [seen for _, seen in dispatches] == [listening + 1]


@pytest.mark.usefixtures("short_wait")
async def test_the_broadcast_target_all_is_never_reported_missing(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """``all`` is no entity in the state machine, but the broadcast it names may
    well land: an unconfirmed call is partial, not ENTITY_NOT_FOUND."""
    dispatches = _device_service(hass, "turn_off")
    listening = _state_listeners(hass)

    resp = await tools.ha_call_service(
        domain="test", service="turn_off", entity_id="all"
    )

    assert resp["success"] is True
    assert resp["partial"] is True
    assert [seen for _, seen in dispatches] == [listening]


@pytest.mark.usefixtures("short_wait")
async def test_a_target_removed_during_the_call_is_reported_not_found(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """Reporting it as still unavailable would claim it still exists."""
    hass.states.async_set("light.b", "unavailable")
    _device_service(hass, "turn_on", lambda: hass.states.async_remove("light.b"))

    with pytest.raises(ToolError) as exc:
        await tools.ha_call_service(
            domain="test", service="turn_on", entity_id="light.b"
        )

    assert "ENTITY_NOT_FOUND" in str(exc.value)
    assert "ENTITY_UNAVAILABLE" not in str(exc.value)


@pytest.mark.usefixtures("short_wait")
async def test_a_target_that_reconnects_to_another_state_is_not_unavailable(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """It came back as ``off`` after a ``turn_on``: the wait lapses, but the
    live state shows it is no longer unavailable."""
    hass.states.async_set("light.b", "unavailable")
    _device_service(hass, "turn_on", lambda: hass.states.async_set("light.b", "off"))

    resp = await tools.ha_call_service(
        domain="test", service="turn_on", entity_id="light.b"
    )

    assert resp["success"] is True
    assert resp["partial"] is True
    assert any(
        "state change could not be verified" in w for w in resp.get("warnings", [])
    )


async def test_an_already_on_target_beside_a_missing_one_does_not_wait(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The command takes several targets from any WebSocket caller. The one
    already at its expected state confirms at once and the missing one is not
    waited on, so the call returns long before its timeout."""
    hass.states.async_set("light.a", "on")
    dispatches = _device_service(hass, "turn_on")
    listening = _state_listeners(hass)

    start = time.monotonic()
    result = await ws_result(
        component_ws,
        WS_CALL_SERVICE,
        domain="test",
        service="turn_on",
        entity_ids=["light.a", "light.missing"],
        wait=True,
        timeout=5.0,
        expected_state="on",
    )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert (result["dispatched"], result["confirmed"], result["partial"]) == (
        True,
        False,
        True,
    )
    old = {t["entity_id"]: t["old_state"] for t in result["transitions"]}
    assert old["light.a"]["state"] == "on"
    assert old["light.missing"] is None
    assert [seen for _, seen in dispatches] == [listening + 1]


@pytest.mark.usefixtures("short_wait")
async def test_an_available_target_that_never_confirms_is_partial(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """The fast failures above must not swallow a real lapse on a real target."""
    hass.states.async_set("light.c", "off")
    dispatches = _device_service(hass, "turn_on")
    listening = _state_listeners(hass)

    resp = await tools.ha_call_service(
        domain="test", service="turn_on", entity_id="light.c"
    )

    assert resp["success"] is True
    assert resp["partial"] is True
    assert "verified_state" not in resp
    assert [seen for _, seen in dispatches] == [listening + 1]


async def test_the_component_itself_refuses_its_own_domain(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The block that holds for any caller of the command, not only the server:
    ``get_caller_token`` exists, and is never run."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert hass.services.has_service(DOMAIN, "get_caller_token")
    events = async_capture_events(hass, EVENT_CALL_SERVICE)

    reply = await ws_call(
        component_ws, WS_CALL_SERVICE, domain=DOMAIN, service="get_caller_token"
    )

    assert reply["success"] is False
    assert "not callable" in reply["error"]["message"]
    assert events == []
