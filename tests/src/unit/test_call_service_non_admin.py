"""``ha_call_service`` for a token whose user is not an administrator.

The client calls services over Core's WebSocket ``call_service`` for such a
token (#2546). That answer lists no changed states, a reply that fails after
the frame went out is the service's own answer and is not sent again, and a
reply that never comes is reported like a REST timeout: dispatched, not failed.
Core, its WebSocket API and the user are real.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandTimeout
from ha_mcp.tools.service_response import STATES_NOT_LISTED_WARNING
from ha_mcp.tools.tools_service import ServiceTools

from ._command_faults import pooled_socket


class Bumps:
    """``counter.bump`` writes ``sensor.count``; ``counter.fail`` writes it and
    then fails with text that reads like a proxy's 403."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls = 0
        for service in ("bump", "fail"):
            hass.services.async_register(
                "counter",
                service,
                self._handle,
                supports_response=SupportsResponse.OPTIONAL,
            )
        hass.states.async_set("sensor.count", "0")

    async def _handle(self, call: ServiceCall) -> ServiceResponse:
        self.calls += 1
        self.hass.states.async_set("sensor.count", str(self.calls))
        if call.service == "fail":
            raise HomeAssistantError("upstream answered 403 Forbidden")
        return {"calls": self.calls}


@pytest.fixture
def bumps(hass: HomeAssistant) -> Bumps:
    return Bumps(hass)


@pytest.fixture
def tools(read_only_client: HomeAssistantClient) -> ServiceTools:
    return ServiceTools(read_only_client, None)


async def test_the_reply_says_its_changed_states_are_not_listed(
    tools: ServiceTools, bumps: Bumps
) -> None:
    resp = await tools.ha_call_service(
        domain="counter", service="bump", return_response=True
    )

    assert resp["service_response"] == {"calls": 1}
    assert resp["result"] == []
    assert STATES_NOT_LISTED_WARNING in resp["warnings"]


async def test_a_service_that_fails_after_it_ran_is_not_sent_again(
    hass: HomeAssistant, tools: ServiceTools, bumps: Bumps
) -> None:
    with pytest.raises(ToolError):
        await tools.ha_call_service(domain="counter", service="fail")

    assert bumps.calls == 1
    state = hass.states.get("sensor.count")
    assert state is not None and state.state == "1"


async def test_an_unanswered_call_is_reported_as_dispatched(
    read_only_client: HomeAssistantClient, tools: ServiceTools
) -> None:
    socket = await pooled_socket(read_only_client)
    socket.send_command = AsyncMock(  # type: ignore[method-assign]
        side_effect=HomeAssistantCommandTimeout("no answer in time")
    )

    resp = await tools.ha_call_service(domain="counter", service="bump")

    assert resp["success"] is True
    assert resp["partial"] is True
