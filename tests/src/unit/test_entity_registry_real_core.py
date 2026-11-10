"""ha_set_entity / ha_get_entity_exposure on a real ``hass``.

Every check and every refusal below is Core's own: the tools do not judge
entity ids or assistant names themselves.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools
from ha_mcp.tools.tools_voice_assistant import VoiceAssistantTools

from .test_zone_icons_real_core import core_client  # noqa: F401 - fixture


@pytest.fixture
async def lamp(hass: HomeAssistant, core_client: HomeAssistantClient) -> str:  # noqa: F811
    """A registry entry whose entity_id the user changed."""
    assert await async_setup_component(hass, "homeassistant", {})
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("light", "demo", "lamp")
    registry.async_update_entity(entry.entity_id, new_entity_id="light.my_lamp")
    return "light.my_lamp"


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))["error"]


async def test_auto_takes_the_id_core_generates(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,  # noqa: F811
    lamp: str,
) -> None:
    registry = er.async_get(hass)
    entry = registry.async_get(lamp)
    assert entry is not None
    generated = registry.async_regenerate_entity_id(entry)
    assert generated != lamp

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=lamp, new_entity_id="auto"
    )

    assert result["entity_id"] == generated
    assert registry.async_get(generated) is not None


async def test_auto_on_an_automatic_id_renames_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,  # noqa: F811
) -> None:
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("light", "demo", "plain")
    assert registry.async_regenerate_entity_id(entry) == entry.entity_id

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=entry.entity_id, new_entity_id="auto"
    )

    assert result["updates"] == ["entity_id unchanged"]
    assert result["entity_entry"]["entity_id"] == entry.entity_id
    assert "old_entity_id" not in result


async def test_auto_on_an_unknown_entity_is_not_found(
    core_client: HomeAssistantClient,  # noqa: F811
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id="light.nope", new_entity_id="auto"
        )

    assert _error(exc_info)["code"] == "ENTITY_NOT_FOUND"


async def test_get_entity_serves_cores_full_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,  # noqa: F811
    lamp: str,
) -> None:
    er.async_get(hass).async_update_entity(lamp, aliases=["Desk"])
    tools = EntityTools(core_client)

    single = (await tools.ha_get_entity(entity_id=lamp))["entity_entry"]
    bulk = (await tools.ha_get_entity(entity_id=[lamp, lamp]))["entity_entries"]
    resolved = (await tools.ha_get_entity(unique_id="lamp"))["entity_entries"]

    for entry in (single, bulk[0], resolved[0]):
        assert {"capabilities", "entity_category", "id"} <= entry.keys()
        assert entry["aliases"] == ["Desk"]


async def test_a_rename_into_another_domain_is_cores_refusal(
    core_client: HomeAssistantClient,  # noqa: F811
    lamp: str,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id=lamp, new_entity_id="switch.my_lamp"
        )

    error = _error(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "same domain" in error["message"]


async def test_enabling_returns_cores_restart_answer(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,  # noqa: F811
    lamp: str,
) -> None:
    er.async_get(hass).async_update_entity(
        lamp, disabled_by=er.RegistryEntryDisabler.USER
    )

    result = await EntityTools(core_client).ha_set_entity(entity_id=lamp, enabled=True)

    # An entry without a config entry: Core asks for a restart.
    assert result["require_restart"] is True


async def test_an_unknown_assistant_is_refused_before_any_write(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,  # noqa: F811
    lamp: str,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id=lamp, name="Renamed", expose_to={"bogus": True}
        )

    assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    entry = er.async_get(hass).async_get(lamp)
    assert entry is not None
    assert entry.name is None


async def test_exposure_is_cores_map(
    core_client: HomeAssistantClient,  # noqa: F811
    lamp: str,
) -> None:
    await EntityTools(core_client).ha_set_entity(
        entity_id=lamp, expose_to={"conversation": True}
    )
    tools = VoiceAssistantTools(core_client)

    single = await tools.ha_get_entity_exposure(entity_id=lamp)
    listed = await tools.ha_get_entity_exposure(assistant="conversation")
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_get_entity_exposure(assistant="bogus")

    assert single["exposed_to"] == {"conversation": True}
    assert listed["exposed_entities"][lamp] == {"conversation": True}
    assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
