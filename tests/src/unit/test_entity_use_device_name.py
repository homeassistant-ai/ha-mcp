"""Issue #2611: ``ha_set_entity``'s ``use_device_name`` on a real ``hass``.

Core reads ``name`` ``""`` as "follow the device name" and ``None`` as "use
the integration's name", so ``name=""`` keeps meaning revert-to-default and
``use_device_name`` carries the entity dialog's "Main entity" switch. What
the switch writes is in test_entity_registry_real_core.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools

from ._entity_registry_core import core_client as core_client
from ._entity_registry_core import device_entity as device_entity
from ._entity_registry_core import sent_types as sent_types
from ._tool_errors import error_of


@pytest.fixture
def named(hass: HomeAssistant, core_client: HomeAssistantClient) -> str:
    """An entity on a device with a name the user gave it over the integration's."""
    entry = device_entity(hass, "Dawn")
    er.async_get(hass).async_update_entity(entry.entity_id, name="Mine")
    return entry.entity_id


def name_of(hass: HomeAssistant, entity_id: str) -> str | None:
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    return entry.name


async def test_false_reverts_to_the_integration_name(
    hass: HomeAssistant, core_client: HomeAssistantClient, named: str
) -> None:
    result = await EntityTools(core_client).ha_set_entity(
        entity_id=named, use_device_name=False
    )

    assert name_of(hass, named) is None
    assert result["updates"] == ["name cleared"]


async def test_empty_name_reverts_to_the_integration_name(
    hass: HomeAssistant, core_client: HomeAssistantClient, named: str
) -> None:
    """``name=''`` must not become Core's ``""``, which follows the device."""
    await EntityTools(core_client).ha_set_entity(entity_id=named, name="")

    assert name_of(hass, named) is None


async def test_other_updates_keep_the_users_name(
    hass: HomeAssistant, core_client: HomeAssistantClient, named: str
) -> None:
    await EntityTools(core_client).ha_set_entity(entity_id=named, icon="mdi:weather")

    assert name_of(hass, named) == "Mine"


async def test_true_without_a_device_is_refused(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """Core would render an empty name: there is no device name to follow."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("sensor", "demo", "dawn", original_name="Dawn")
    before = registry.async_get(entry.entity_id)

    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id=entry.entity_id, use_device_name=True
        )

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "no device" in error["message"]
    assert sent_types(sent) == ["config/entity_registry/get"]
    assert registry.async_get(entry.entity_id) == before


async def test_true_with_a_name_is_refused(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    named: str,
    sent: AsyncMock,
) -> None:
    """Both write the same field; the name alone is written above."""
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id=named, name="Dawn", use_device_name=True
        )

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "use_device_name" in error["message"]
    assert sent_types(sent) == []
    assert name_of(hass, named) == "Mine"


async def test_true_on_several_entities_is_refused(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    named: str,
    sent: AsyncMock,
) -> None:
    other = er.async_get(hass).async_get_or_create("sensor", "demo", "dusk")

    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id=[named, other.entity_id], use_device_name=True
        )

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "use_device_name" in error["message"]
    assert sent_types(sent) == []
    assert name_of(hass, named) == "Mine"
