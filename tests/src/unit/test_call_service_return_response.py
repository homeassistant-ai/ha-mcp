"""ha_call_service places a service's response data once (issue #2085).

Without the component a call is Core's REST POST, whose answer lists the
changed states and, when asked for, the response data; ``service_response``
appears exactly once, at the top level. With the component, a call asking for
a response lists the states written under its context the same way.

Every reply below is Core's own: a test service registered on the real
``hass`` changes states under the call's context and returns response data.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.util.json import JsonValueType

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_service import ServiceTools

SHELL_RESPONSE: dict[str, JsonValueType] = {
    "stdout": "abc",
    "stderr": "",
    "returncode": 0,
}


def _occurrences(response: dict[str, Any], needle: str) -> int:
    """Count *needle* in the response payload, ignoring ``warnings`` prose."""
    payload = {k: v for k, v in response.items() if k != "warnings"}
    return json.dumps(payload).count(needle)


def _register_echo(hass: HomeAssistant, *changed: str) -> None:
    """``test.echo``: turns each of ``changed`` on and answers SHELL_RESPONSE.

    ``echo`` is not a state-changing service name, so ``ha_call_service``
    takes the REST path without waiting for a state change.
    """

    async def echo(call: ServiceCall) -> ServiceResponse:
        for entity_id in changed:
            hass.states.async_set(
                entity_id, "on", {"brightness": 10}, context=call.context
            )
        return dict(SHELL_RESPONSE)

    hass.services.async_register(
        "test", "echo", echo, supports_response=SupportsResponse.OPTIONAL
    )


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> ServiceTools:
    return ServiceTools(core_client, None)


@pytest.mark.parametrize("verbose", [False, True], ids=["compact", "verbose"])
async def test_without_the_component_the_post_lists_its_states_and_response_once(
    hass: HomeAssistant, tools: ServiceTools, verbose: bool
) -> None:
    """Core's REST POST answers the changed states and the response; the
    response appears once, top level, and verbose does not repeat it."""
    _register_echo(hass, "light.x")

    response = await tools.ha_call_service(
        domain="test", service="echo", return_response=True, verbose=verbose
    )

    assert response["service_response"] == SHELL_RESPONSE
    assert [record["entity_id"] for record in response["result"]] == ["light.x"]
    assert _occurrences(response, "stdout") == 1, response
    assert "warnings" not in response, response


async def test_the_component_lists_a_confirmed_calls_changed_state_once(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Where the changed states live: the component's confirmed call returns
    the target's new state, compacted, beside the response data shipped once."""

    async def turn_on(call: ServiceCall) -> ServiceResponse:
        hass.states.async_set("light.x", "on", {"brightness": 10})
        return dict(SHELL_RESPONSE)

    hass.states.async_set("light.x", "off")
    hass.services.async_register(
        "light", "turn_on", turn_on, supports_response=SupportsResponse.OPTIONAL
    )

    response = await ServiceTools(component_core_client, None).ha_call_service(
        domain="light", service="turn_on", entity_id="light.x", return_response=True
    )

    assert response["service_response"] == SHELL_RESPONSE
    (record,) = response["result"]
    assert (record["entity_id"], record["state"]) == ("light.x", "on")
    assert "context" not in record
    assert "last_changed" not in record
    assert _occurrences(response, "stdout") == 1, response
    assert "warnings" not in response, response


async def test_a_verbose_confirmed_call_keeps_every_write_of_its_target(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The confirmation is the target's verification, not a replacement for
    the writes Core recorded under the call's context."""

    async def turn_on(call: ServiceCall) -> ServiceResponse:
        for level in (10, 200):
            hass.states.async_set(
                "light.x", "on", {"brightness": level}, context=call.context
            )
        return dict(SHELL_RESPONSE)

    hass.states.async_set("light.x", "off")
    hass.services.async_register(
        "light", "turn_on", turn_on, supports_response=SupportsResponse.OPTIONAL
    )

    response = await ServiceTools(component_core_client, None).ha_call_service(
        domain="light",
        service="turn_on",
        entity_id="light.x",
        return_response=True,
        verbose=True,
    )

    levels = [r["attributes"]["brightness"] for r in response["result"]]
    assert levels == [10, 200]


@pytest.mark.parametrize("verbose", [False, True], ids=["compact", "verbose"])
async def test_the_component_lists_an_unconfirmed_calls_changed_states(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, verbose: bool
) -> None:
    """A call with no target to confirm still lists, through the component,
    the states written under its context, with no install hint."""
    _register_echo(hass, "light.x", "light.y")

    response = await ServiceTools(component_core_client, None).ha_call_service(
        domain="test", service="echo", return_response=True, verbose=verbose
    )

    assert response["service_response"] == SHELL_RESPONSE
    assert sorted(r["entity_id"] for r in response["result"]) == [
        "light.x",
        "light.y",
    ]
    assert _occurrences(response, "stdout") == 1, response
    assert "warnings" not in response, response


async def test_return_response_false_has_no_service_response_key(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """Without return_response Core answers a plain changed-states list."""
    _register_echo(hass, "light.x")

    response = await tools.ha_call_service(domain="test", service="echo")

    assert "service_response" not in response
    assert [record["entity_id"] for record in response["result"]] == ["light.x"]


@pytest.fixture
async def hanging_service(hass: HomeAssistant) -> Any:
    """``test.hang``: answers only once the test releases it."""
    release = asyncio.Event()

    async def hang(call: ServiceCall) -> ServiceResponse:
        await release.wait()
        return dict(SHELL_RESPONSE)

    hass.services.async_register(
        "test", "hang", hang, supports_response=SupportsResponse.OPTIONAL
    )
    try:
        yield release
    finally:
        release.set()
        await hass.async_block_till_done()


async def test_timed_out_call_still_emits_the_requested_key(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hanging_service: asyncio.Event,
) -> None:
    """A timed-out call must not be the one shape missing service_response.

    The reply never arrived, so the value is necessarily null, and that null
    proves nothing: the response carries the ambiguity warning.
    """
    # The server's client with a one-second request timeout, so Core's
    # unanswered call times out in the client as it would on a slow install.
    impatient = HomeAssistantClient(
        base_url=core_client.base_url,
        token=core_client.token,
        timeout=1,
        verify_ssl=False,
    )
    try:
        response = await ServiceTools(impatient, None).ha_call_service(
            domain="test", service="hang", return_response=True
        )
    finally:
        await impatient.close()

    assert response["partial"] is True
    assert "service_response" in response
    assert response["service_response"] is None
    assert any("does NOT prove" in w for w in response["warnings"]), response
