"""ha_get_entity / ha_set_entity on a real ``hass``.

Every check and every refusal below is Core's own: the tools do not judge
entity ids themselves. The main-entity tests check that the fields
ha_set_entity takes from the entity dialog land in a real registry. Exposure
writes are in test_entity_exposure_real_core.py.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools
from ha_mcp.utils.registry_update_lock import registry_update_lock

from ._entity_registry_core import assert_cores_refusal as assert_cores_refusal
from ._entity_registry_core import (
    assert_default_suggestions as assert_default_suggestions,
)
from ._entity_registry_core import core_client as core_client
from ._entity_registry_core import device_entity as device_entity
from ._entity_registry_core import lamp as lamp
from ._entity_registry_core import sent_types as sent_types
from ._tool_errors import error_of


async def test_auto_takes_the_id_core_generates(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
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


async def test_auto_on_a_mixed_case_id_takes_the_id_core_generates(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    registry = er.async_get(hass)
    entry = registry.async_get(lamp)
    assert entry is not None
    generated = registry.async_regenerate_entity_id(entry)

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=lamp.title(), new_entity_id="auto"
    )

    assert result["entity_id"] == generated
    assert registry.async_get(generated) is not None


@pytest.mark.parametrize("spelling", [str.lower, str.title])
async def test_auto_on_an_automatic_id_renames_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    spelling: Callable[[str], str],
) -> None:
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("light", "demo", "plain")
    assert registry.async_regenerate_entity_id(entry) == entry.entity_id

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=spelling(entry.entity_id), new_entity_id="auto"
    )

    assert result["updates"] == ["entity_id unchanged"]
    assert result["entity_entry"]["entity_id"] == entry.entity_id
    assert "old_entity_id" not in result


async def test_auto_on_an_unknown_entity_is_not_found(
    core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id="light.nope", new_entity_id="auto"
        )

    assert error_of(exc_info)["code"] == "ENTITY_NOT_FOUND"


async def test_auto_on_an_invalid_id_is_cores_refusal(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id="not an id", new_entity_id="auto"
        )

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert error["message"].startswith("No automatic entity_id for not an id: ")
    assert "is an invalid entity ID" in error["message"]
    assert_default_suggestions(error)
    assert_cores_refusal(exc_info, "invalid_format")
    assert sent_types(sent) == ["config/entity_registry/get_automatic_entity_ids"]


@pytest.mark.parametrize("own_area", [True, False])
@pytest.mark.parametrize(
    ("original_name", "name", "expected_name"),
    [("Dawn", "Mine", ""), (None, "Mine", None), ("Dawn", None, "")],
)
async def test_main_entity_writes_what_the_dialog_writes(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    original_name: str | None,
    name: str | None,
    expected_name: str | None,
    own_area: bool,
) -> None:
    kitchen = ar.async_get(hass).async_create("Kitchen")
    registry = er.async_get(hass)
    entry = device_entity(hass, original_name)
    registry.async_update_entity(
        entry.entity_id, name=name, area_id=kitchen.id if own_area else None
    )

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=entry.entity_id, use_device_name=True
    )

    written = registry.async_get(entry.entity_id)
    assert written is not None
    assert (written.name, written.area_id) == (expected_name, None)
    assert result["updates"] == ["main entity of the device"] + (
        ["area cleared: follows the device's area"] if own_area else []
    )


@pytest.mark.parametrize(("original_name", "name"), [("Dawn", ""), (None, None)])
async def test_main_entity_is_returned_unchanged(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    original_name: str | None,
    name: str | None,
) -> None:
    registry = er.async_get(hass)
    entry = device_entity(hass, original_name)
    registry.async_update_entity(entry.entity_id, name=name)
    before = registry.async_get(entry.entity_id)

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=entry.entity_id, use_device_name=True
    )

    assert registry.async_get(entry.entity_id) == before
    assert result["entity_entry"]["name"] == name
    assert result["entity_entry"]["entity_id"] == entry.entity_id
    assert result["updates"] == ["already the main entity"]


async def test_main_entity_keeps_an_area_core_left_on_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> None:
    """Core refuses a user edit into this shape, but an integration update
    that drops the entity's name leaves it so; the switch keeps the area."""
    kitchen = ar.async_get(hass).async_create("Kitchen")
    registry = er.async_get(hass)
    entry = device_entity(hass, "Dawn")
    registry.async_update_entity(entry.entity_id, area_id=kitchen.id)
    registry.async_update_entity(entry.entity_id, original_name=None)
    before = registry.async_get(entry.entity_id)

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=entry.entity_id, use_device_name=True
    )

    assert registry.async_get(entry.entity_id) == before
    assert result["updates"] == ["already the main entity"]


async def test_main_entity_on_an_unknown_entity_is_not_found(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id="sensor.nope", use_device_name=True
        )

    assert error_of(exc_info)["code"] == "ENTITY_NOT_FOUND"
    assert sent_types(sent) == ["config/entity_registry/get"]


async def test_main_entity_reads_the_entry_under_the_update_lock(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write is built from the entry, so no update may land between."""
    entry = device_entity(hass, "Dawn")
    lock = registry_update_lock("entity", entry.entity_id)
    read = er.RegistryEntry.extended_dict
    held: list[bool] = []

    def extended_dict(registry_entry: er.RegistryEntry) -> dict[str, Any]:
        # Core builds its reply to config/entity_registry/get from this.
        held.append(lock.locked())
        return read.__get__(registry_entry)

    monkeypatch.setattr(er.RegistryEntry, "extended_dict", property(extended_dict))
    await EntityTools(core_client).ha_set_entity(
        entity_id=entry.entity_id, use_device_name=True
    )

    assert held
    assert all(held)


async def test_main_entity_with_own_area_is_cores_refusal(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> None:
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")
    office = areas.async_create("Office")
    registry = er.async_get(hass)
    entry = device_entity(hass, "Dawn")
    registry.async_update_entity(entry.entity_id, name="Mine", area_id=office.id)
    before = registry.async_get(entry.entity_id)

    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id=entry.entity_id, use_device_name=True, area_id=kitchen.id
        )

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "cannot have an area of its own" in error["message"]
    assert_default_suggestions(error)
    assert registry.async_get(entry.entity_id) == before


async def test_an_update_answers_with_data_not_a_sentence_restating_it(
    core_client: HomeAssistantClient, lamp: str
) -> None:
    result = await EntityTools(core_client).ha_set_entity(
        entity_id=lamp, icon="mdi:lamp"
    )

    assert result["success"] is True
    assert "message" not in result


async def test_get_entity_serves_cores_full_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    er.async_get(hass).async_update_entity(lamp, aliases=["Desk"])
    tools = EntityTools(core_client)

    single = (await tools.ha_get_entity(entity_id=lamp))["entity_entry"]
    bulk = (await tools.ha_get_entity(entity_id=[lamp, lamp]))["entity_entries"]
    resolved = (await tools.ha_get_entity(unique_id="lamp"))["entity_entries"]

    core = await core_client.send_websocket_message(
        {"type": "config/entity_registry/get", "entity_id": lamp}
    )
    for entry in (single, bulk[0], resolved[0]):
        # Every field of Core's entry, value for value; enrichment is additive.
        assert entry.items() >= core["result"].items()
        assert entry.keys() - core["result"].keys() <= {
            "area",
            "floor",
            "label_names",
            "full_name",
        }
        assert entry["aliases"] == ["Desk"]


@pytest.fixture
def unique_ids(hass: HomeAssistant, core_client: HomeAssistantClient) -> dict[str, str]:
    """One unique_id under two platforms, and an entry with a unique_id of its own."""
    registry = er.async_get(hass)
    return {
        "hue": registry.async_get_or_create("sensor", "hue", "shared").entity_id,
        "zwave_js": registry.async_get_or_create(
            "light", "zwave_js", "shared"
        ).entity_id,
        "other": registry.async_get_or_create("sensor", "hue", "abc123").entity_id,
    }


async def test_unique_id_reads_the_list_then_the_entry(
    core_client: HomeAssistantClient, sent: AsyncMock, unique_ids: dict[str, str]
) -> None:
    result = await EntityTools(core_client).ha_get_entity(unique_id="abc123")

    assert result["success"] is True
    assert result["matches"] == 1
    assert result["unique_id"] == "abc123"
    entry = result["entity_entries"][0]
    assert entry["entity_id"] == unique_ids["other"]
    assert entry["unique_id"] == "abc123"
    assert entry["platform"] == "hue"
    assert sent_types(sent) == [
        "config/entity_registry/list",
        "config/entity_registry/get_entries",
    ]
    assert sent.call_args_list[0].args[0] == {"type": "config/entity_registry/list"}
    assert sent.call_args_list[1].args[0]["entity_ids"] == [unique_ids["other"]]


async def test_unique_id_on_two_platforms_returns_both(
    core_client: HomeAssistantClient, unique_ids: dict[str, str]
) -> None:
    result = await EntityTools(core_client).ha_get_entity(unique_id="shared")

    assert result["matches"] == 2
    assert {e["entity_id"] for e in result["entity_entries"]} == {
        unique_ids["hue"],
        unique_ids["zwave_js"],
    }


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [("platform", "zwave_js", "zwave_js"), ("domain", "sensor", "hue")],
)
async def test_unique_id_narrowed_by_platform_or_domain(
    core_client: HomeAssistantClient,
    unique_ids: dict[str, str],
    key: str,
    value: str,
    expected: str,
) -> None:
    result = await EntityTools(core_client).ha_get_entity(
        unique_id="shared", **{key: value}
    )

    assert result["matches"] == 1
    assert result[key] == value
    assert result["entity_entries"][0]["entity_id"] == unique_ids[expected]


async def test_an_unknown_unique_id_is_not_found(
    core_client: HomeAssistantClient, unique_ids: dict[str, str]
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_get_entity(unique_id="nope")

    error = error_of(exc_info)
    assert error["code"] == "ENTITY_NOT_FOUND"
    assert "nope" in error["message"]


async def test_a_rename_into_another_domain_is_cores_refusal(
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id=lamp, new_entity_id="switch.my_lamp"
        )

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "same domain" in error["message"]
    assert_default_suggestions(error)
    assert_cores_refusal(exc_info, "invalid_info")


async def test_enabling_returns_cores_restart_answer(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    er.async_get(hass).async_update_entity(
        lamp, disabled_by=er.RegistryEntryDisabler.USER
    )

    result = await EntityTools(core_client).ha_set_entity(entity_id=lamp, enabled=True)

    # An entry without a config entry: Core asks for a restart.
    assert result["require_restart"] is True


async def test_a_mixed_case_list_read_finds_the_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    result = await EntityTools(core_client).ha_get_entity(entity_id=["Light.My_Lamp"])

    [entry] = result["entity_entries"]
    assert entry["entity_id"] == lamp


async def test_entity_id_parts_are_cores_settings(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    tools = EntityTools(core_client)
    registry = er.async_get(hass)

    written = await tools.ha_set_entity(entity_id_parts=["device", "entity"])
    assert registry.settings.entity_id_parts == ("device", "entity")
    assert written["settings"] == {"entity_id_parts": ["device", "entity"]}
    assert written["previous_settings"] == {"entity_id_parts": None}

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_set_entity(entity_id_parts=["entity"])
    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    assert_cores_refusal(exc_info, "invalid_format")
    assert registry.settings.entity_id_parts == ("device", "entity")

    await tools.ha_set_entity(entity_id_parts=[])
    assert registry.settings.entity_id_parts is None


async def test_a_bulk_read_reports_an_unknown_id_as_not_found(
    core_client: HomeAssistantClient, lamp: str
) -> None:
    result = await EntityTools(core_client).ha_get_entity(
        entity_id=[lamp, "light.missing"]
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert [e["entity_id"] for e in result["entity_entries"]] == [lamp]
    assert result["errors"] == [
        {"entity_id": "light.missing", "error": "Entity not found"}
    ]
    assert "suggestions" in result
