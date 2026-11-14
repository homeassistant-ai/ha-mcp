"""A service that ran but whose response Core rejected is no failed call.

Core runs a service asked for its response, then refuses the response when it
is not a mapping (``service_reponse_invalid``). The write has landed by then,
so ``ha_call_service`` must say the call ran and Core rejected its response,
never report an unapplied failure that invites a retry. The service is a
handler in Core's ``hass.services`` registered with an optional response; it
writes a state and returns nothing.
"""

from __future__ import annotations

import pytest
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.tools_service import ServiceTools


class Counter:
    """``light.turn_on`` and ``counter.bump``: each writes ``light.a`` / ``sensor.count``
    and returns no response."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls = 0
        for domain, service in (("light", "turn_on"), ("counter", "bump")):
            hass.services.async_register(
                domain,
                service,
                self._handle,
                supports_response=SupportsResponse.OPTIONAL,
            )
        hass.states.async_set("light.a", "off")
        hass.states.async_set("sensor.count", "0")

    async def _handle(self, call: ServiceCall) -> ServiceResponse:
        self.calls += 1
        if call.service == "turn_on":
            self.hass.states.async_set("light.a", "on")
        else:
            self.hass.states.async_set("sensor.count", str(self.calls))
        return None


@pytest.fixture
def counter(hass: HomeAssistant) -> Counter:
    return Counter(hass)


def _rejected(resp: dict[str, object]) -> None:
    """The call ran; Core's refusal of the response is reported as such."""
    assert resp["success"] is True
    assert resp["service_response"] is None
    error = resp["response_error"]
    assert isinstance(error, dict)
    assert error["translation_key"] == "service_reponse_invalid"


async def test_a_confirmed_call_whose_response_core_rejects_ran_once_and_says_so(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, counter: Counter
) -> None:
    """Through the component: its frame ran the service, so it is not sent again."""
    resp = await ServiceTools(
        component_core_client, DeviceControlTools(component_core_client)
    ).ha_call_service(
        domain="light", service="turn_on", entity_id="light.a", return_response=True
    )

    _rejected(resp)
    state = hass.states.get("light.a")
    assert state is not None and state.state == "on"
    assert counter.calls == 1


async def test_a_posted_call_whose_response_core_rejects_ran_once_and_says_so(
    hass: HomeAssistant, core_client: HomeAssistantClient, counter: Counter
) -> None:
    """Without the component, and for any call the component does not confirm."""
    resp = await ServiceTools(
        core_client, DeviceControlTools(core_client)
    ).ha_call_service(domain="counter", service="bump", return_response=True)

    _rejected(resp)
    state = hass.states.get("sensor.count")
    assert state is not None and state.state == "1"
    assert counter.calls == 1
