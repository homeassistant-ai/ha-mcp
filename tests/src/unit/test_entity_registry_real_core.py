"""ha_get_entity / ha_set_entity on a real ``hass``.

Every check and every refusal below is Core's own: the tools do not judge
entity ids or assistant names themselves. The main-entity tests check that the
fields ha_set_entity takes from the entity dialog land in a real registry.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.homeassistant import exposed_entities as core_exposed
from homeassistant.components.homeassistant.const import DATA_EXPOSED_ENTITIES
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools
from ha_mcp.utils.registry_update_lock import registry_update_lock


@pytest.fixture(params=["component", "core"])
async def core_client(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> HomeAssistantClient:
    """The shared ``core_client``, with and without the component's commands."""
    # ``config`` serves the registry commands the tools send.
    assert await async_setup_component(hass, "config", {})
    if request.param == "component":
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    return core_client


@pytest.fixture
async def lamp(hass: HomeAssistant, core_client: HomeAssistantClient) -> str:
    """A registry entry whose entity_id the user changed."""
    assert await async_setup_component(hass, "homeassistant", {})
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("light", "demo", "lamp")
    registry.async_update_entity(entry.entity_id, new_entity_id="light.my_lamp")
    return "light.my_lamp"


@pytest.fixture
def desk(hass: HomeAssistant) -> str:
    """A second light entry, for the bulk writes."""
    return er.async_get(hass).async_get_or_create("light", "demo", "desk").entity_id


def _types(sent: AsyncMock) -> list[str]:
    return [call.args[0]["type"] for call in sent.call_args_list]


def _expose_writes(sent: AsyncMock) -> list[dict[str, Any]]:
    return [
        call.args[0]
        for call in sent.call_args_list
        if call.args[0]["type"] == "homeassistant/expose_entity"
    ]


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))["error"]


def _assert_cores_refusal(
    exc_info: pytest.ExceptionInfo[ToolError], ha_error_code: str
) -> None:
    """Core's refusal keeps its code as ``ha_error_code`` and its message
    without the client's ``Command failed:`` prefix."""
    payload = json.loads(str(exc_info.value))
    assert "Command failed" not in payload["error"]["message"]
    assert payload["ha_error_code"] == ha_error_code


def _device_entity(hass: HomeAssistant, original_name: str | None) -> er.RegistryEntry:
    """A sensor on a device, the shape the main-entity switch applies to."""
    config_entry = MockConfigEntry(domain="demo")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("demo", "sun")}
    )
    return er.async_get(hass).async_get_or_create(
        "sensor", "demo", "dawn", device_id=device.id, original_name=original_name
    )


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

    assert _error(exc_info)["code"] == "ENTITY_NOT_FOUND"


async def test_auto_on_an_invalid_id_is_cores_refusal(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id="not an id", new_entity_id="auto"
        )

    error = _error(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "is an invalid entity ID" in error["message"]
    _assert_cores_refusal(exc_info, "invalid_format")
    assert _types(sent) == ["config/entity_registry/get_automatic_entity_ids"]


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
    entry = _device_entity(hass, original_name)
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
    entry = _device_entity(hass, original_name)
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
    entry = _device_entity(hass, "Dawn")
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

    assert _error(exc_info)["code"] == "ENTITY_NOT_FOUND"
    assert _types(sent) == ["config/entity_registry/get"]


async def test_main_entity_reads_the_entry_under_the_update_lock(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write is built from the entry, so no update may land between."""
    entry = _device_entity(hass, "Dawn")
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
    entry = _device_entity(hass, "Dawn")
    registry.async_update_entity(entry.entity_id, name="Mine", area_id=office.id)
    before = registry.async_get(entry.entity_id)

    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id=entry.entity_id, use_device_name=True, area_id=kitchen.id
        )

    error = _error(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "cannot have an area of its own" in error["message"]
    assert registry.async_get(entry.entity_id) == before


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
        assert entry.keys() - core["result"].keys() <= {"area", "floor", "label_names"}
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
    assert _types(sent) == [
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

    error = _error(exc_info)
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

    error = _error(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "same domain" in error["message"]
    _assert_cores_refusal(exc_info, "invalid_info")


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


async def test_an_unknown_assistant_is_refused_before_any_write(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id=lamp, name="Renamed", expose_to={"bogus": True}
        )

    assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    _assert_cores_refusal(exc_info, "invalid_format")
    entry = er.async_get(hass).async_get(lamp)
    assert entry is not None
    assert entry.name is None


async def test_exposure_is_cores_map(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    tools = EntityTools(core_client)
    await tools.ha_set_entity(entity_id=lamp, expose_to={"conversation": True})

    single = await tools.ha_get_entity(entity_id=lamp)
    listed = await tools.ha_get_entity(exposed_to="conversation")
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_get_entity(exposed_to="bogus")

    options = single["entity_entry"]["options"]
    assert options["conversation"]["should_expose"] is True
    assert listed["exposed_entities"][lamp] == {"conversation": True}
    store = hass.data[DATA_EXPOSED_ENTITIES]
    assert listed["expose_new_entities"] is store.async_get_expose_new_entities(
        "conversation"
    )
    assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"


async def test_an_entity_without_registry_entry_reads_cores_store(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    hass.states.async_set("light.loose", "on")
    hass.states.async_set("light.quiet", "off")
    async_expose_entity(hass, "conversation", "light.loose", True)
    async_expose_entity(hass, "conversation", "light.gone", True)
    tools = EntityTools(core_client)

    loose = await tools.ha_get_entity(entity_id="light.loose")
    mixed = await tools.ha_get_entity(entity_id="Light.Loose")
    quiet = await tools.ha_get_entity(entity_id="light.quiet")
    gone = await tools.ha_get_entity(entity_id="light.gone")
    bulk = await tools.ha_get_entity(entity_id=[lamp, "light.loose"])
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_get_entity(entity_id="light.nowhere")

    assert loose["success"] is True
    assert loose["entity_entry"] is None
    assert loose["exposed_to"] == {"conversation": True}
    assert mixed["exposed_to"] == {"conversation": True}
    assert quiet["exposed_to"] == {}
    assert quiet["is_exposed_anywhere"] is False
    assert gone["exposed_to"] == {"conversation": True}
    assert bulk["count"] == 1
    assert [e["entity_id"] for e in bulk["entity_entries"]] == [lamp]
    assert bulk["unregistered_entities"] == [
        {
            "entity_id": "light.loose",
            "entity_entry": None,
            "exposed_to": {"conversation": True},
            "is_exposed_anywhere": True,
        }
    ]
    assert "errors" not in bulk
    assert _error(exc_info)["code"] == "ENTITY_NOT_FOUND"


async def test_a_mixed_case_list_read_finds_the_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    result = await EntityTools(core_client).ha_get_entity(entity_id=["Light.My_Lamp"])

    [entry] = result["entity_entries"]
    assert entry["entity_id"] == lamp


async def test_expose_new_entities_writes_cores_flag(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    store = hass.data[DATA_EXPOSED_ENTITIES]
    before = store.async_get_expose_new_entities("conversation")

    result = await EntityTools(core_client).ha_set_entity(
        expose_new_entities={"conversation": not before}
    )

    assert store.async_get_expose_new_entities("conversation") is (not before)
    assert result["expose_new_entities"] == {"conversation": not before}
    assert result["previous_expose_new_entities"] == {"conversation": before}


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
    assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    _assert_cores_refusal(exc_info, "invalid_format")
    assert registry.settings.entity_id_parts == ("device", "entity")

    await tools.ha_set_entity(entity_id_parts=[])
    assert registry.settings.entity_id_parts is None


async def test_expose_to_on_an_entity_without_registry_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    hass.states.async_set("light.loose", "on")
    tools = EntityTools(core_client)

    written = await tools.ha_set_entity(
        entity_id="Light.Loose", expose_to={"conversation": True}
    )
    read = await tools.ha_get_entity(entity_id="light.loose")

    assert written["exposure"] == {"conversation": True}
    assert written["entity_entry"] is None
    assert "warnings" not in written
    assert read["exposed_to"] == {"conversation": True}


async def test_expose_to_hides_a_stored_entity_without_a_state(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    async_expose_entity(hass, "conversation", "light.gone", True)

    written = await EntityTools(core_client).ha_set_entity(
        entity_id="light.gone", expose_to={"conversation": False}
    )

    store = hass.data[DATA_EXPOSED_ENTITIES].entities
    assert store["light.gone"].assistants["conversation"]["should_expose"] is False
    assert written["entity_entry"] is None


async def test_expose_to_on_a_mixed_case_id_exposes_the_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    # Core exposes a light to Assist by default, so hiding it is a change.
    written = await EntityTools(core_client).ha_set_entity(
        entity_id="Light.My_Lamp", expose_to={"conversation": False}
    )

    entry = er.async_get(hass).async_get(lamp)
    assert entry is not None
    assert entry.options["conversation"]["should_expose"] is False
    assert written["updates"] == ["expose_to={'conversation': False}"]


async def test_an_empty_expose_to_changes_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    written = await EntityTools(core_client).ha_set_entity(entity_id=lamp, expose_to={})

    assert written["updates"] == ["expose_to={} (already set, nothing changed)"]
    entry = er.async_get(hass).async_get(lamp)
    assert entry is not None
    assert "conversation" not in entry.options


async def test_expose_to_after_a_registry_update_exposes_the_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    """The registry update's reply is the entry the exposure is sent for."""
    await EntityTools(core_client).ha_set_entity(
        entity_id="Light.My_Lamp", name="Desk", expose_to={"conversation": True}
    )

    entry = er.async_get(hass).async_get(lamp)
    assert entry is not None
    assert entry.name == "Desk"
    assert entry.options["conversation"]["should_expose"] is True


async def test_expose_to_on_an_unknown_entity_writes_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id="light.typo", expose_to={"conversation": True}
        )

    assert _error(exc_info)["code"] == "ENTITY_NOT_FOUND"
    assert "light.typo" not in hass.data[DATA_EXPOSED_ENTITIES].entities


async def test_bulk_expose_to_writes_under_the_ids_core_reads(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    hass.states.async_set("light.loose", "on")

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=["Light.My_Lamp", "Light.Loose", "light.typo"],
        expose_to={"conversation": False},
    )

    entry = er.async_get(hass).async_get(lamp)
    assert entry is not None
    assert entry.options["conversation"]["should_expose"] is False
    store = hass.data[DATA_EXPOSED_ENTITIES].entities
    assert store["light.loose"].assistants["conversation"]["should_expose"] is False
    assert "light.typo" not in store
    lamp_row, loose_row = result["succeeded"]
    assert lamp_row["entity_id"] == "Light.My_Lamp"
    assert lamp_row["entity_entry"]["options"]["conversation"]["should_expose"] is False
    assert lamp_row["updates"] == ["expose_to={'conversation': False}"]
    assert loose_row["entity_id"] == "Light.Loose"
    assert loose_row["entity_entry"] is None
    [typo_row] = result["failed"]
    assert typo_row == {"entity_id": "light.typo", "error": "Entity not found"}
    assert "warnings" not in result


@pytest.mark.parametrize(
    ("expose_to", "writes"),
    [
        ({"conversation": True}, [(["conversation"], True)]),
        # Read again after the write, also when the write only hides.
        ({"conversation": False}, [(["conversation"], False)]),
        (
            {
                "conversation": True,
                "cloud.alexa": True,
                "cloud.google_assistant": False,
            },
            [
                (["conversation", "cloud.alexa"], True),
                (["cloud.google_assistant"], False),
            ],
        ),
    ],
)
async def test_expose_to_writes_once_per_value_and_returns_the_entry_after_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    lamp: str,
    expose_to: dict[str, bool],
    writes: list[tuple[list[str], bool]],
) -> None:
    result = await EntityTools(core_client).ha_set_entity(
        entity_id=lamp, expose_to=expose_to
    )

    assert result["exposure"] == expose_to
    entry = er.async_get(hass).async_get(lamp)
    assert entry is not None
    for assistant, should_expose in expose_to.items():
        assert entry.options[assistant]["should_expose"] is should_expose
        returned = result["entity_entry"]["options"][assistant]
        assert returned["should_expose"] is should_expose
    sent_writes = _expose_writes(sent)
    assert [(w["assistants"], w["should_expose"]) for w in sent_writes] == writes
    assert all(w["entity_ids"] == [lamp] for w in sent_writes)
    assert _types(sent)[-1] == "config/entity_registry/get"


@pytest.mark.parametrize(
    ("other_write", "applied"),
    [
        ({"name": "Porch"}, lambda entry, device: entry.name == "Porch"),
        (
            {"name": "Porch", "labels": ["outdoor"]},
            lambda entry, device: (entry.name, entry.labels) == ("Porch", {"outdoor"}),
        ),
        (
            {"options": {"sensor": {"display_precision": 2}}},
            lambda entry, device: entry.options["sensor"] == {"display_precision": 2},
        ),
        (
            {"new_device_name": "Living Room"},
            lambda entry, device: device.name_by_user == "Living Room",
        ),
    ],
)
async def test_expose_to_after_another_write_returns_the_entry_after_both(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    other_write: dict[str, Any],
    applied: Callable[[er.RegistryEntry, dr.DeviceEntry | dr.ChildDeviceEntry], bool],
) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    lr.async_get(hass).async_create("outdoor")
    entry = _device_entity(hass, "Dawn")

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=entry.entity_id, expose_to={"conversation": True}, **other_write
    )

    types = _types(sent)
    written = er.async_get(hass).async_get(entry.entity_id)
    assert written is not None
    device = dr.async_get(hass).async_get(str(entry.device_id))
    assert device is not None
    assert applied(written, device)
    assert written.options["conversation"]["should_expose"] is True
    assert types[-2:] == ["homeassistant/expose_entity", "config/entity_registry/get"]
    core = await core_client.send_websocket_message(
        {"type": "config/entity_registry/get", "entity_id": entry.entity_id}
    )
    assert result["entity_entry"] == core["result"]


async def test_bulk_expose_to_writes_once_per_value_for_all_ids(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    lamp: str,
    desk: str,
) -> None:
    ids = [lamp, desk]

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=ids, expose_to={"conversation": True, "cloud.alexa": False}
    )

    assert result["succeeded_count"] == 2
    writes = _expose_writes(sent)
    assert [(w["entity_ids"], w["should_expose"]) for w in writes] == [
        (ids, True),
        (ids, False),
    ]
    # One read of the ids to send, one read of the entries after the writes.
    reads = [t for t in _types(sent) if t == "config/entity_registry/get_entries"]
    assert len(reads) == 2
    assert _types(sent)[-1] == "config/entity_registry/get_entries"
    for row in result["succeeded"]:
        options = row["entity_entry"]["options"]
        assert options["conversation"]["should_expose"] is True
        assert options["cloud.alexa"]["should_expose"] is False


async def test_bulk_labels_and_expose_to_write_each_entry_then_expose_once(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    lamp: str,
    desk: str,
) -> None:
    lr.async_get(hass).async_create("outdoor")
    ids = [lamp, desk]

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=ids, labels=["outdoor"], expose_to={"conversation": True}
    )

    assert result["succeeded_count"] == 2
    writes_and_reads = [
        t
        for t in _types(sent)
        if t.startswith(("config/entity_registry/", "homeassistant/expose_entity"))
    ]
    assert writes_and_reads == [
        "config/entity_registry/update",
        "config/entity_registry/update",
        "homeassistant/expose_entity",
        "config/entity_registry/get_entries",
    ]
    assert _expose_writes(sent)[0]["entity_ids"] == ids
    registry = er.async_get(hass)
    for row in result["succeeded"]:
        entry = registry.async_get(row["entity_id"])
        assert entry is not None
        assert entry.labels == {"outdoor"}
        updates = " ".join(row["updates"])
        assert "labels=['outdoor']" in updates
        assert "expose_to=" in updates
        # Read again after the exposure, not the entry the label write returned.
        options = row["entity_entry"]["options"]
        assert options["conversation"]["should_expose"] is True


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


@pytest.mark.parametrize(
    ("expose_to", "already_set"),
    [
        # Core's default for Alexa is False: nothing to change.
        ({"cloud.alexa": False}, True),
        ({"cloud.alexa": True}, False),
        # The change in the first write counts though the second changes nothing.
        ({"cloud.alexa": True, "cloud.google_assistant": False}, False),
    ],
)
async def test_expose_to_says_when_core_changed_nothing(
    core_client: HomeAssistantClient,
    lamp: str,
    expose_to: dict[str, bool],
    already_set: bool,
) -> None:
    result = await EntityTools(core_client).ha_set_entity(
        entity_id=lamp, expose_to=expose_to
    )

    assert ("already set" in " ".join(result["updates"])) is already_set


async def test_bulk_expose_to_marks_only_the_unchanged_entities(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
    desk: str,
) -> None:
    async_expose_entity(hass, "cloud.alexa", desk, True)

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=[lamp, desk], expose_to={"cloud.alexa": True}
    )

    updates = {r["entity_id"]: " ".join(r["updates"]) for r in result["succeeded"]}
    assert "already set" not in updates[lamp]
    assert "already set" in updates[desk]


async def test_expose_to_on_a_core_without_a_change_report_assumes_a_change(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core before 2026.11 answers ``homeassistant/expose_entity`` with no result."""

    # wraps copies the command, schema and callback mark Core registers it by.
    @functools.wraps(core_exposed.ws_expose_entity)
    def without_change_report(
        hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
    ) -> None:
        for entity_id in msg["entity_ids"]:
            for assistant in msg["assistants"]:
                async_expose_entity(hass, assistant, entity_id, msg["should_expose"])
        connection.send_result(msg["id"])

    # Registered when the component sets up, so replaced before that.
    monkeypatch.setattr(core_exposed, "ws_expose_entity", without_change_report)
    assert await async_setup_component(hass, "homeassistant", {})
    lamp = er.async_get(hass).async_get_or_create("light", "demo", "lamp").entity_id

    # Nothing changes, but this Core does not say so.
    result = await EntityTools(core_client).ha_set_entity(
        entity_id=lamp, expose_to={"cloud.alexa": False}
    )

    assert result["updates"] == ["expose_to={'cloud.alexa': False}"]


@pytest.mark.parametrize("new_entity_id", [None, "sensor.renamed"])
async def test_every_write_of_a_set_holds_the_entity_lock(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    new_entity_id: str | None,
) -> None:
    """A restore takes the lock of the entity's id, either id on a rename, so
    it waits for the options, device-rename and expose writes too."""
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _device_entity(hass, "Dawn")
    locks = [
        registry_update_lock("entity", entity_id)
        for entity_id in (entry.entity_id, new_entity_id)
        if entity_id is not None
    ]
    held: list[tuple[str, bool]] = []

    def record(owner: Any, name: str) -> None:
        write = getattr(owner, name)

        def under_lock(*args: Any, **kwargs: Any) -> Any:
            held.append((name, all(lock.locked() for lock in locks)))
            return write(*args, **kwargs)

        monkeypatch.setattr(owner, name, under_lock)

    # The Core functions behind each command the set sends.
    writes: list[tuple[Any, str]] = [
        (er.EntityRegistry, "async_update_entity"),
        (er.EntityRegistry, "async_update_entity_options"),
        (dr.DeviceRegistry, "async_update_device"),
        (core_exposed, "async_expose_entity"),
    ]
    for owner, name in writes:
        record(owner, name)
    await EntityTools(core_client).ha_set_entity(
        entity_id=entry.entity_id,
        name="N",
        new_entity_id=new_entity_id,
        options={"sensor": {"display_precision": 2}},
        new_device_name="Dev X",
        expose_to={"conversation": True},
    )

    assert {name for name, _ in held} == {name for _, name in writes}
    assert all(locked for _, locked in held), held
