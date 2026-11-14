"""A service that ran but whose response Core rejected is no failed call.

Core runs a service asked for its response, then refuses the response when it
is not a mapping (``service_reponse_invalid``). The write has landed by then.
Through the component, ``ha_call_service`` says the call ran and Core rejected
its response. Without it, Core's REST POST answers only a server error, so the
tool's error says the service may have run and sends nothing again. The service is a
handler in Core's ``hass.services`` registered with an optional response; it
writes a state and returns nothing.
"""

from __future__ import annotations

import pytest
import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.service_response import (
    RESPONSE_CALL_FAILED_HINT,
    SERVICE_FAILED_AFTER_DISPATCH_HINT,
)
from ha_mcp.tools.tools_service import ServiceTools

from ._tool_errors import error_of


class Counter:
    """``light.turn_on`` and ``counter.bump``: each writes ``light.a`` / ``sensor.count``
    and returns no response. ``counter.fail`` and ``counter.crash`` write
    ``sensor.count``, then raise a ``HomeAssistantError`` / a ``ValueError``."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls = 0
        for domain, service in (
            ("light", "turn_on"),
            ("counter", "bump"),
            ("counter", "fail"),
            ("counter", "crash"),
        ):
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
        if call.service == "fail":
            raise HomeAssistantError("upstream failed")
        if call.service == "crash":
            raise ValueError("library bug")
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


async def test_without_the_component_a_rejected_response_is_not_called_again(
    hass: HomeAssistant, core_client: HomeAssistantClient, counter: Counter
) -> None:
    with pytest.raises(ToolError) as exc:
        await ServiceTools(
            core_client, DeviceControlTools(core_client)
        ).ha_call_service(domain="counter", service="bump", return_response=True)

    assert RESPONSE_CALL_FAILED_HINT in error_of(exc)["suggestions"]
    state = hass.states.get("sensor.count")
    assert state is not None and state.state == "1"
    assert counter.calls == 1


@pytest.mark.parametrize("service", ["fail", "crash"])
async def test_a_response_call_that_fails_after_it_ran_is_not_sent_again(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    counter: Counter,
    service: str,
) -> None:
    """The component dispatched it, so the error is reported, not re-posted."""
    with pytest.raises(ToolError) as exc:
        await ServiceTools(
            component_core_client, DeviceControlTools(component_core_client)
        ).ha_call_service(domain="counter", service=service, return_response=True)

    assert counter.calls == 1
    state = hass.states.get("sensor.count")
    assert state is not None and state.state == "1"
    assert SERVICE_FAILED_AFTER_DISPATCH_HINT in error_of(exc)["suggestions"]


async def test_a_response_call_core_refuses_by_schema_stays_a_validation_error(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Core checks the schema before the service runs, so nothing ran."""
    calls: list[ServiceCall] = []

    async def _handle(call: ServiceCall) -> ServiceResponse:
        calls.append(call)
        return {}

    hass.services.async_register(
        "counter",
        "strict",
        _handle,
        schema=vol.Schema({vol.Required("amount"): int}),
        supports_response=SupportsResponse.OPTIONAL,
    )

    with pytest.raises(ToolError) as exc:
        await ServiceTools(
            component_core_client, DeviceControlTools(component_core_client)
        ).ha_call_service(domain="counter", service="strict", return_response=True)

    assert error_of(exc)["code"] == "VALIDATION_FAILED"
    assert calls == []
