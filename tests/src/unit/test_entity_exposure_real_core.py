"""ha_set_entity's exposure writes and the reads of Core's exposure map on a
real ``hass``.

Core judges every assistant name; the tests check what the tools send it and
what they return from its answers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.homeassistant import exposed_entities as core_exposed
from homeassistant.components.homeassistant.const import DATA_EXPOSED_ENTITIES
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools
from ha_mcp.utils.registry_update_lock import registry_update_lock

from ._entity_registry_core import assert_cores_refusal as assert_cores_refusal
from ._entity_registry_core import core_client as core_client
from ._entity_registry_core import device_entity as device_entity
from ._entity_registry_core import lamp as lamp
from ._entity_registry_core import sent_types as sent_types
from ._tool_errors import error_of


@pytest.fixture
def desk(hass: HomeAssistant) -> str:
    """A second light entry, for the bulk writes."""
    return er.async_get(hass).async_get_or_create("light", "demo", "desk").entity_id


def expose_writes(sent: AsyncMock) -> list[dict[str, Any]]:
    return [
        call.args[0]
        for call in sent.call_args_list
        if call.args[0]["type"] == "homeassistant/expose_entity"
    ]


async def test_an_unknown_assistant_is_refused_before_any_write(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id=lamp, name="Renamed", expose_to={"bogus": True}
        )

    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    assert_cores_refusal(exc_info, "invalid_format")
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
    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"


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
    assert error_of(exc_info)["code"] == "ENTITY_NOT_FOUND"


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

    assert error_of(exc_info)["code"] == "ENTITY_NOT_FOUND"
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
    sent_writes = expose_writes(sent)
    assert [(w["assistants"], w["should_expose"]) for w in sent_writes] == writes
    assert all(w["entity_ids"] == [lamp] for w in sent_writes)
    assert sent_types(sent)[-1] == "config/entity_registry/get"


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
    entry = device_entity(hass, "Dawn")

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=entry.entity_id, expose_to={"conversation": True}, **other_write
    )

    types = sent_types(sent)
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
    writes = expose_writes(sent)
    assert [(w["entity_ids"], w["should_expose"]) for w in writes] == [
        (ids, True),
        (ids, False),
    ]
    # One read of the ids to send, one read of the entries after the writes.
    reads = [t for t in sent_types(sent) if t == "config/entity_registry/get_entries"]
    assert len(reads) == 2
    assert sent_types(sent)[-1] == "config/entity_registry/get_entries"
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
        for t in sent_types(sent)
        if t.startswith(("config/entity_registry/", "homeassistant/expose_entity"))
    ]
    assert writes_and_reads == [
        "config/entity_registry/update",
        "config/entity_registry/update",
        "homeassistant/expose_entity",
        "config/entity_registry/get_entries",
    ]
    assert expose_writes(sent)[0]["entity_ids"] == ids
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
    entry = device_entity(hass, "Dawn")
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


async def test_a_rename_with_expose_to_exposes_the_new_id(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    """The old ID has no entry after the rename; an exposure written to it
    would land in Core's store for entities without one."""
    await EntityTools(core_client).ha_set_entity(
        entity_id=lamp, new_entity_id="light.porch", expose_to={"cloud.alexa": True}
    )

    assert core_exposed.async_get_assistant_settings(hass, "cloud.alexa") == {
        "light.porch": {"should_expose": True}
    }
