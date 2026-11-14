"""ha_remove_entity classifies a failure on Core's error code (#2632).

The entities live in Core's entity registry and every reply is Core's. A lost
connection is a real one: Core drops the WebSocket when the client's token is
revoked, refuses a rejected token on the next connect, and an unreachable
address refuses the connection.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools

from ._receipt_faults import revoke

# Nothing listens on port 1, so a connection to it is refused.
UNREACHABLE_URL = "http://127.0.0.1:1"


@pytest.fixture
async def tools(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> EntityTools:
    """Entity tools on a ``hass`` with sensor.real, sensor.a, sensor.b, sensor.c."""
    assert await async_setup_component(hass, "config", {})
    for object_id in ("real", "a", "b", "c"):
        entity_registry.async_get_or_create(
            "sensor", "demo", object_id, suggested_object_id=object_id
        )
    return EntityTools(core_client)


@pytest.fixture
async def on_removal(
    hass: HomeAssistant,
) -> AsyncIterator[Callable[[str, Callable[[], object]], None]]:
    """Run an action the moment Core removes ``entity_id`` from its registry,
    before the reply leaves Core."""
    unsubscribes: list[Callable[[], None]] = []

    def register(entity_id: str, action: Callable[[], object]) -> None:
        @callback
        def removed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
            data = event.data
            if data["action"] == "remove" and data["entity_id"] == entity_id:
                action()

        unsubscribes.append(
            hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, removed)
        )

    yield register
    for unsubscribe in unsubscribes:
        unsubscribe()


async def test_failure_mentioning_not_found_is_not_an_absent_entity(
    tools: EntityTools, entity_registry: er.EntityRegistry
) -> None:
    """A refusal whose text says 'not found' but whose code is not not_found
    is a failed removal, not an already-absent entity."""
    with (
        patch.object(
            entity_registry,
            "async_remove",
            side_effect=HomeAssistantError("Config entry not found"),
        ),
        pytest.raises(ToolError) as exc_info,
    ):
        await tools.ha_remove_entity(entity_id="sensor.real")

    assert json.loads(str(exc_info.value))["error"]["code"] == "SERVICE_CALL_FAILED"


async def test_bulk_failure_mentioning_not_found_is_an_error_not_a_skip(
    tools: EntityTools, entity_registry: er.EntityRegistry
) -> None:
    with patch.object(
        entity_registry,
        "async_remove",
        side_effect=HomeAssistantError("Config entry not found"),
    ):
        result = await tools.ha_remove_entity(entity_id=["sensor.a"])

    assert result["skipped"] == []
    assert [e["entity_id"] for e in result["errors"]] == ["sensor.a"]


# Core closes the socket itself (a revoked or refused token); the client's
# close reply then re-arms aiohttp's heartbeat timer on that closed server
# socket (web_ws.py _on_data_received -> _reset_heartbeat), which nothing cancels.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_lost_connection_keeps_what_the_bulk_run_already_removed(
    hass: HomeAssistant,
    tools: EntityTools,
    hass_access_token: str,
    on_removal: Callable[[str, Callable[[], object]], None],
) -> None:
    """The removals that went through are reported, and the ids never reached
    are named so the caller can retry them."""
    # Core drops the connection while removing sensor.b, before it replies.
    on_removal("sensor.b", lambda: revoke(hass, hass_access_token))

    result = await tools.ha_remove_entity(
        entity_id=["sensor.a", "sensor.b", "sensor.c"]
    )

    assert result["success"] is False
    assert result["removed"] == ["sensor.a"]
    assert result["errors"][0]["entity_id"] == "sensor.b"
    assert result["errors"][0]["code"] == "CONNECTION_FAILED"
    assert result["not_attempted"] == ["sensor.c"]


# The same closed server socket as the test above.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_token_rejected_on_reconnect_is_reported_as_auth(
    hass: HomeAssistant,
    tools: EntityTools,
    hass_access_token: str,
) -> None:
    """A token rejected on connect arrives as a command that was never sent,
    raised from the auth error: the per-id error names the auth failure, and
    the failing id is listed as not attempted too."""
    revoke(hass, hass_access_token)

    result = await tools.ha_remove_entity(entity_id=["sensor.a", "sensor.b"])

    assert result["errors"][0]["code"] == "AUTH_INVALID_TOKEN"
    assert result["not_attempted"] == ["sensor.a", "sensor.b"]


# The same closed server socket for a rejected token.
@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.parametrize(
    ("attribute", "value", "code"),
    [
        ("token", "rejected-token", "AUTH_INVALID_TOKEN"),
        ("base_url", UNREACHABLE_URL, "CONNECTION_FAILED"),
    ],
    ids=["rejected_token", "refused"],
)
async def test_unsent_removal_mid_run_lists_only_the_ids_not_removed(
    tools: EntityTools,
    core_client: HomeAssistantClient,
    on_removal: Callable[[str, Callable[[], object]], None],
    attribute: str,
    value: str,
    code: str,
) -> None:
    """After sensor.a is removed the client needs a new connection, which Core
    refuses (a rejected token) or nothing accepts (a refused address)."""
    with ExitStack() as stack:
        on_removal(
            "sensor.a",
            lambda: stack.enter_context(patch.object(core_client, attribute, value)),
        )
        result = await tools.ha_remove_entity(
            entity_id=["sensor.a", "sensor.b", "sensor.c"]
        )

    assert result["removed"] == ["sensor.a"]
    assert result["errors"][0]["entity_id"] == "sensor.b"
    assert result["errors"][0]["code"] == code
    assert result["not_attempted"] == ["sensor.b", "sensor.c"]


async def test_timed_out_removal_is_reported_as_a_timeout(tools: EntityTools) -> None:
    """send_websocket_message raises a connection error from the command
    timeout; the per-id error names it as a timeout by that cause, whatever
    the timeout's text says."""
    # No wait at all: Core's reply cannot arrive before the deadline.
    with patch.object(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 0):
        result = await tools.ha_remove_entity(entity_id=["sensor.a", "sensor.b"])

    assert result["errors"][0]["code"] == "CONNECTION_TIMEOUT"
    assert result["not_attempted"] == ["sensor.b"]
