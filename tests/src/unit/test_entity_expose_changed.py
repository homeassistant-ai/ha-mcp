"""ha_set_entity reports an exposure that changed nothing (Core 2026.11+).

Core 2026.11 answers ``homeassistant/expose_entity`` with the entity ids it
actually changed (core#188809). The entities live in Core's entity registry,
their exposure is set through Core beforehand, and Core's own reply decides
what the tool reports.
"""

from unittest.mock import patch

import pytest
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.entity_expose_batch import apply_bulk_expose
from ha_mcp.tools.tools_entities import EntityTools


@pytest.fixture
async def tools(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> EntityTools:
    """Entity tools on a ``hass`` with light.test, light.a, light.b, light.c."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "config", {})
    for object_id in ("test", "a", "b", "c"):
        entity_registry.async_get_or_create(
            "light", "demo", object_id, suggested_object_id=object_id
        )
    return EntityTools(core_client)


def _expose(hass: HomeAssistant, entity_id: str, **exposure: bool) -> None:
    """Set the entity's exposure in Core before the tool runs."""
    for assistant, should_expose in exposure.items():
        async_expose_entity(hass, assistant.replace("_", "."), entity_id, should_expose)


@pytest.mark.parametrize(
    ("exposed_before", "already_set"), [(True, True), (False, False)]
)
async def test_single_entity_update_says_when_nothing_changed(
    hass: HomeAssistant, tools: EntityTools, exposed_before: bool, already_set: bool
) -> None:
    _expose(hass, "light.test", conversation=exposed_before)

    result = await tools.ha_set_entity(
        entity_id="light.test", expose_to={"conversation": True}
    )

    assert ("already set" in " ".join(result["updates"])) is already_set


async def test_empty_expose_map_still_returns_the_entry(tools: EntityTools) -> None:
    result = await tools.ha_set_entity(entity_id="light.test", expose_to={})
    assert result["entity_entry"]["entity_id"] == "light.test"


async def test_empty_expose_map_reuses_an_entry_already_read(
    tools: EntityTools, core_client: HomeAssistantClient
) -> None:
    with patch.object(
        core_client,
        "send_websocket_message",
        wraps=core_client.send_websocket_message,
    ) as ws:
        result = await tools.ha_set_entity(
            entity_id="light.test", name="Test", expose_to={}
        )
    assert result["entity_entry"]["entity_id"] == "light.test"
    ws.assert_awaited_once()


async def test_a_change_in_an_earlier_assistant_group_counts(
    hass: HomeAssistant, tools: EntityTools
) -> None:
    # The first group (conversation on) changes; the second (alexa off) does not.
    _expose(hass, "light.test", conversation=False, cloud_alexa=False)

    result = await tools.ha_set_entity(
        entity_id="light.test", expose_to={"conversation": True, "cloud.alexa": False}
    )

    assert "already set" not in " ".join(result["updates"])


async def test_bulk_update_marks_only_the_unchanged_entities(
    hass: HomeAssistant, tools: EntityTools
) -> None:
    _expose(hass, "light.a", conversation=False)
    _expose(hass, "light.b", conversation=True)

    result = await tools.ha_set_entity(
        entity_id=["light.a", "light.b"], expose_to={"conversation": True}
    )

    updates = {r["entity_id"]: " ".join(r["updates"]) for r in result["succeeded"]}
    assert "already set" not in updates["light.a"]
    assert "already set" in updates["light.b"]


async def test_bulk_exposure_collects_what_core_changed(
    hass: HomeAssistant, tools: EntityTools, core_client: HomeAssistantClient
) -> None:
    # light.a changes in the first group (conversation on), light.b in the
    # second (alexa off); light.c is already set for both.
    _expose(hass, "light.a", conversation=False, cloud_alexa=False)
    _expose(hass, "light.b", conversation=True, cloud_alexa=True)
    _expose(hass, "light.c", conversation=True, cloud_alexa=False)

    error, changed = await apply_bulk_expose(
        core_client,
        ["light.a", "light.b", "light.c"],
        {"conversation": True, "cloud.alexa": False},
        str,
    )

    assert error is None
    assert changed == {"light.a", "light.b"}
