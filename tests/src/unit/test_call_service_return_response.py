"""ha_call_service places a service's response data once (issue #2085).

Home Assistant answers ``return_response`` over REST with an envelope,
``{"changed_states": [...], "service_response": {...}}``. The legacy REST path
in ``ha_call_service`` used to put that whole envelope in ``result`` *and*
copy ``service_response`` to the top level, so the response shipped twice.
The envelope is now split: ``result`` carries the changed states only, and
``service_response`` appears exactly once, at the top level.

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


async def test_service_response_is_top_level_once_and_states_are_compacted(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """The envelope splits: response data top-level, compacted states in result."""
    _register_echo(hass, "light.x")

    response = await tools.ha_call_service(
        domain="test", service="echo", return_response=True
    )

    assert response["service_response"] == SHELL_RESPONSE
    result = response["result"]
    assert isinstance(result, list), result
    assert [record["entity_id"] for record in result] == ["light.x"]
    assert "service_response" not in result[0]
    # The changed states go through default compaction like every other path.
    assert "context" not in result[0]
    assert "last_changed" not in result[0]
    assert _occurrences(response, "stdout") == 1, response
    # Core's reply is the expected envelope, so nothing needs explaining.
    assert "warnings" not in response, response


async def test_verbose_returns_raw_states_and_one_response_copy(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """verbose=True bypasses projection but must not resurrect the duplicate."""
    _register_echo(hass, "light.x")

    response = await tools.ha_call_service(
        domain="test", service="echo", return_response=True, verbose=True
    )

    assert response["service_response"] == SHELL_RESPONSE
    (record,) = response["result"]
    state = hass.states.get("light.x")
    assert state is not None
    # Raw: Core's metadata is kept, and it is the state the call changed.
    assert record["entity_id"] == "light.x"
    assert record["context"]["id"] == state.context.id
    assert "last_changed" in record
    assert _occurrences(response, "stdout") == 1, response


async def test_result_fields_projects_the_changed_states(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """result_fields reaches the changed states on a return_response call.

    Before the split ``result`` was the envelope dict, so projection bailed on
    its list guard and the parameter was silently ignored.
    """
    _register_echo(hass, "light.x")

    response = await tools.ha_call_service(
        domain="test",
        service="echo",
        return_response=True,
        result_fields=["entity_id"],
    )

    assert response["result"] == [{"entity_id": "light.x"}]
    assert response["service_response"] == SHELL_RESPONSE


async def test_entity_id_filters_the_changed_states(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """Compaction's target filter applies to return_response changed states."""
    _register_echo(hass, "light.x", "light.parent_group")

    response = await tools.ha_call_service(
        domain="test", service="echo", entity_id="light.x", return_response=True
    )

    result = response["result"]
    assert [record["entity_id"] for record in result] == ["light.x"], result
    assert response["service_response"] == SHELL_RESPONSE


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
