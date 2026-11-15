"""ha_get_entity / ha_set_entity without an entity, exposure of an entity
without a registry entry, and failed entity writes, on a real ``hass``.

Core's own answers for these commands are pinned in
test_entity_registry_real_core.py and test_entity_exposure_real_core.py; these tests pin what the tools send, the
order they send it in, how they shape the registry-wide replies, and how they
report a failure. A failure Core cannot be made to give is one Core function
raising Core's own exception, or the client getting no answer at all.
"""

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.homeassistant import exposed_entities as core_exposed
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import entity_registry_reads
from ha_mcp.tools.tools_entities import EntityTools

from ._entity_registry_core import assert_default_suggestions
from ._tool_errors import error_of

_LIST = "homeassistant/expose_entity/list"
_FLAG_GET = "homeassistant/expose_new_entities/get"
_FLAG_SET = "homeassistant/expose_new_entities/set"
_SETTINGS_GET = "config/entity_registry/settings/get"
_EXPOSE = "homeassistant/expose_entity"
_BAD_ID = "not an id"
_PARTS = ["device", "entity"]


@pytest.fixture(autouse=True)
async def _core(hass: HomeAssistant) -> None:
    """Core's registry and exposure commands, and ``light.kitchen`` in its registry."""
    for domain in ("config", "homeassistant"):
        assert await async_setup_component(hass, domain, {})
    er.async_get(hass).async_get_or_create(
        "light", "demo", "kitchen", suggested_object_id="kitchen"
    )


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> EntityTools:
    return EntityTools(core_client)


@pytest.fixture
def state_read_drops(
    monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> None:
    """The REST state read gets no answer: the connection drops."""
    monkeypatch.setattr(
        core_client,
        "get_entity_state",
        AsyncMock(side_effect=HomeAssistantConnectionError("server down")),
    )


@pytest.fixture
def refetch_drops(
    monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> None:
    """Core applies the exposure, then the connection drops: the read after it
    gets no answer."""
    expose = core_exposed.async_expose_entity

    def expose_then_drop(*args: Any) -> None:
        expose(*args)
        monkeypatch.setattr(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
        )

    monkeypatch.setattr(core_exposed, "async_expose_entity", expose_then_drop)


def _messages(sent: AsyncMock) -> list[dict[str, Any]]:
    return [call.args[0] for call in sent.call_args_list]


def _types(sent: AsyncMock) -> list[str]:
    return [message["type"] for message in _messages(sent)]


def _body(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))


def _list_raises(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Core's exposure list raises, so its reply is Core's error reply."""
    monkeypatch.setattr(
        core_exposed, "async_get_entity_settings", MagicMock(side_effect=error)
    )


def _expose_raises(
    monkeypatch: pytest.MonkeyPatch, only_for: bool | None = None
) -> None:
    """Core's exposure write raises; ``only_for`` limits it to that value."""
    expose = core_exposed.async_expose_entity

    def expose_or_raise(
        hass: HomeAssistant, assistant: str, entity_id: str, value: bool
    ) -> None:
        if only_for in (None, value):
            raise HomeAssistantError("boom")
        expose(hass, assistant, entity_id, value)

    monkeypatch.setattr(core_exposed, "async_expose_entity", expose_or_raise)


def _after_settings_write(
    monkeypatch: pytest.MonkeyPatch, then: Callable[[], None]
) -> None:
    """Core writes the registry settings, then ``then`` runs: the next command
    meets what ``then`` set up, the earlier step stays applied."""
    write = er.EntityRegistry.async_update_settings

    def update(registry: er.EntityRegistry, **changes: Any) -> Any:
        result = write(registry, **changes)
        then()
        return result

    monkeypatch.setattr(er.EntityRegistry, "async_update_settings", update)


class TestRegistryWideRead:
    async def test_lists_every_exposed_entity_with_the_settings(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: EntityTools,
        sent: AsyncMock,
    ) -> None:
        registry = er.async_get(hass)
        for object_id in ("living_room", "bedroom"):
            registry.async_get_or_create(
                "light", "demo", object_id, suggested_object_id=object_id
            )
            core_exposed.async_expose_entity(
                hass, "conversation", f"light.{object_id}", True
            )
        core_exposed.async_expose_entity(hass, "cloud.alexa", "light.living_room", True)
        core_exposed.async_expose_entity(hass, "conversation", "light.kitchen", False)
        core_exposed.async_expose_entity(
            hass, "cloud.google_assistant", "light.kitchen", True
        )

        result = await tools.ha_get_entity()

        assert result["success"] is True
        assert result["exposed_entities"] == {
            "light.living_room": {"conversation": True, "cloud.alexa": True},
            "light.bedroom": {"conversation": True},
            "light.kitchen": {"cloud.google_assistant": True},
        }
        assert result["count"] == 3
        assert result["summary"] == {
            "conversation": 2,
            "cloud.alexa": 1,
            "cloud.google_assistant": 1,
        }
        assert "expose_new_entities" not in result
        # Plain Core has no names or areas to join in; the component does.
        assert "entity_info" not in result
        assert _types(sent) == [_SETTINGS_GET, _LIST]
        settings = await core_client.send_websocket_message({"type": _SETTINGS_GET})
        assert result["settings"] == settings["result"]

    async def test_the_component_list_joins_the_entity_info(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        core_exposed.async_expose_entity(hass, "conversation", "light.kitchen", True)

        result = await EntityTools(component_core_client).ha_get_entity()

        assert result["exposed_entities"] == {"light.kitchen": {"conversation": True}}
        assert result["entity_info"] == {
            "light.kitchen": {
                "domain": "light",
                "area": None,
                "floor": None,
                "labels": [],
            }
        }

    async def test_exposed_to_lists_one_assistant_with_its_flag(
        self, hass: HomeAssistant, tools: EntityTools, sent: AsyncMock
    ) -> None:
        core_exposed.async_expose_entity(hass, "cloud.alexa", "light.kitchen", True)
        # Core's default for Alexa is False, so a True here is the stored flag.
        store = hass.data[core_exposed.DATA_EXPOSED_ENTITIES]
        store.async_set_expose_new_entities("cloud.alexa", True)

        result = await tools.ha_get_entity(exposed_to="cloud.alexa")

        assert result["exposed_entities"] == {"light.kitchen": {"cloud.alexa": True}}
        assert result["count"] == 1
        assert result["assistant"] == "cloud.alexa"
        assert result["expose_new_entities"] is True
        assert "summary" not in result
        # Core judges the name first; its reply is the flag.
        assert _messages(sent)[0] == {
            "type": _FLAG_GET,
            "assistant": "cloud.alexa",
        }

    async def test_a_failed_list_read_carries_cores_message(
        self, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        _list_raises(monkeypatch, HomeAssistantError("Service unavailable"))
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity()

        body = _body(exc_info)
        assert body["error"]["message"] == (
            "Failed to get exposure settings: Service unavailable"
        )
        assert body["ha_error_code"] == "home_assistant_error"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"entity_id": "light.kitchen", "exposed_to": "conversation"},
            {"unique_id": "abc", "exposed_to": "conversation"},
        ],
    )
    async def test_a_listing_with_entity_arguments_is_refused(
        self, tools: EntityTools, sent: AsyncMock, kwargs: dict[str, Any]
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(**kwargs)

        assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
        sent.assert_not_called()

    @pytest.mark.parametrize("resolver_filter", ["domain", "platform"])
    async def test_a_resolver_filter_without_unique_id_is_refused(
        self, tools: EntityTools, sent: AsyncMock, resolver_filter: str
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(**{resolver_filter: "light"})

        error = error_of(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "only apply when unique_id is provided" in error["message"]
        sent.assert_not_called()

    async def test_an_empty_assistant_filter_goes_to_core(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(exposed_to="")

        assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
        assert _types(sent) == [_FLAG_GET]


class TestEntityWithoutRegistryEntry:
    """The failures: a Core function raises, or a read gets no answer."""

    async def test_a_failed_exposure_read_is_not_a_not_found(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        hass.states.async_set("light.porch", "on")
        _list_raises(monkeypatch, Unauthorized())
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(entity_id="light.porch")

        body = _body(exc_info)
        assert body["error"]["code"] != "ENTITY_NOT_FOUND"
        assert "Unauthorized" in body["error"]["message"]
        assert body["entity_id"] == "light.porch"
        assert body["registry_entry"] is None

    async def test_a_refused_exposure_read_is_a_failed_call(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        hass.states.async_set("light.porch", "on")
        _list_raises(monkeypatch, HomeAssistantError("boom"))
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(entity_id="light.porch")

        error = error_of(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "boom" in error["message"]
        assert error["message"].count("Command failed:") == 1

    async def test_a_failed_state_reply_is_not_a_not_found(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        hass.states.async_set("light.porch", "on")

        def unreadable(state: State) -> bytes:
            raise HomeAssistantError("state unreadable")

        # Core's state view fails while serving the state: an HTTP 500.
        monkeypatch.setattr(State, "as_dict_json", property(unreadable))
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(entity_id="light.porch")

        error = error_of(exc_info)
        assert error["code"] != "ENTITY_NOT_FOUND"
        assert "500" in error["message"]

    @pytest.mark.usefixtures("state_read_drops")
    async def test_a_dropped_state_read_is_not_a_not_found(
        self, tools: EntityTools
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(entity_id="light.porch")

        error = error_of(exc_info)
        assert error["code"].startswith("CONNECTION")
        assert "server down" in error["message"]

    async def test_another_registry_refusal_keeps_its_kind(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(entity_id=_BAD_ID)

        body = _body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["error"]["message"].startswith(
            f"Reading the registry entry of {_BAD_ID} failed: Entity ID {_BAD_ID} "
        )
        assert body["ha_error_code"] == "invalid_format"
        assert _types(sent) == ["config/entity_registry/get"]


class TestExposeToWithoutRegistryEntry:
    @pytest.mark.usefixtures("state_read_drops")
    async def test_a_failed_state_read_writes_nothing(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id="light.porch", expose_to={"conversation": True}
            )

        error = error_of(exc_info)
        assert error["code"].startswith("CONNECTION")
        assert "server down" in error["message"]
        assert _EXPOSE not in _types(sent)

    @pytest.mark.usefixtures("state_read_drops")
    async def test_a_failed_state_read_fails_its_bulk_row_only(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        result = await tools.ha_set_entity(
            entity_id=["light.kitchen", "light.porch"],
            expose_to={"cloud.alexa": True},
        )

        [failed] = result["failed"]
        assert failed["entity_id"] == "light.porch"
        assert "server down" in failed["error"]
        [write] = [m for m in _messages(sent) if m["type"] == _EXPOSE]
        assert write["entity_ids"] == ["light.kitchen"]

    async def test_a_failed_bulk_write_keeps_the_rows_failed_before_it(
        self, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        _expose_raises(monkeypatch)
        result = await tools.ha_set_entity(
            entity_id=["light.kitchen", "light.porch"],
            expose_to={"conversation": True},
        )

        failed = {row["entity_id"]: row["error"] for row in result["failed"]}
        assert failed.keys() == {"light.kitchen", "light.porch"}
        assert "boom" in failed["light.kitchen"]

    async def test_a_failed_registry_read_writes_nothing(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        # Core refuses the whole get_entries chunk for the one bad id in it.
        result = await tools.ha_set_entity(
            entity_id=["light.kitchen", _BAD_ID], expose_to={"conversation": True}
        )

        failed = {row["entity_id"]: row["error"] for row in result["failed"]}
        assert failed.keys() == {"light.kitchen", _BAD_ID}
        assert all("entity_ids" in error for error in failed.values())
        assert _EXPOSE not in _types(sent)

    async def test_a_device_rename_without_an_entry_is_skipped_not_failed(
        self, hass: HomeAssistant, tools: EntityTools
    ) -> None:
        hass.states.async_set("zone.home", "0")

        result = await tools.ha_set_entity(
            entity_id="zone.home",
            expose_to={"cloud.alexa": True},
            new_device_name="X",
        )

        assert "partial" not in result
        assert result["warnings"] == [
            "Entity has no associated device — device rename skipped"
        ]
        settings = core_exposed.async_get_entity_settings(hass, "zone.home")
        assert settings["cloud.alexa"]["should_expose"] is True

    async def test_a_device_rename_alone_leaves_the_entry_null(
        self, hass: HomeAssistant, tools: EntityTools
    ) -> None:
        hass.states.async_set("zone.home", "0")

        result = await tools.ha_set_entity(entity_id="zone.home", new_device_name="X")

        assert result["warnings"] == [
            "Entity has no associated device — device rename skipped"
        ]
        assert result["entity_entry"] is None

    async def test_a_device_rename_of_an_unknown_id_is_refused(
        self, tools: EntityTools
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id="light.does_not_exist", new_device_name="X"
            )

        assert error_of(exc_info)["code"] == "ENTITY_NOT_FOUND"


class TestBulkRead:
    async def test_a_failed_exposure_read_keeps_the_found_entries(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        hass.states.async_set("light.porch", "on")
        _list_raises(monkeypatch, Unauthorized())

        result = await tools.ha_get_entity(entity_id=["light.kitchen", "light.porch"])

        assert [e["entity_id"] for e in result["entity_entries"]] == ["light.kitchen"]
        [error] = result["errors"]
        assert error["entity_id"] == "light.porch"
        assert "no registry entry" in error["error"]
        assert "Unauthorized" in error["error"]

    async def test_a_failed_lookup_keeps_another_rows_registry_error(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        monkeypatch.setattr(entity_registry_reads, "GET_ENTRIES_CHUNK_SIZE", 1)
        hass.states.async_set("light.porch", "on")
        _list_raises(monkeypatch, Unauthorized())

        # Each id is its own chunk: Core refuses the bad one, answers for the other.
        result = await tools.ha_get_entity(entity_id=[_BAD_ID, "light.porch"])

        errors = {row["entity_id"]: row["error"] for row in result["errors"]}
        assert "entity_ids" in errors[_BAD_ID]
        assert "no registry entry" not in errors[_BAD_ID]
        assert "no registry entry" in errors["light.porch"]

    async def test_an_unreadable_unique_id_match_is_an_error_not_zero_matches(
        self, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        def unreadable(_entry: er.RegistryEntry) -> dict[str, Any]:
            raise HomeAssistantError("registry busy")

        # Core lists the match, then fails to build its full entry.
        monkeypatch.setattr(er.RegistryEntry, "extended_dict", property(unreadable))
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(unique_id="kitchen")

        body = _body(exc_info)
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert list(body["errors"]) == ["light.kitchen"]
        assert "registry busy" in body["errors"]["light.kitchen"]

    @pytest.mark.usefixtures("refetch_drops")
    async def test_a_dropped_refetch_keeps_the_exposed_rows_and_warns(
        self, hass: HomeAssistant, tools: EntityTools
    ) -> None:
        er.async_get(hass).async_get_or_create(
            "light", "demo", "desk", suggested_object_id="desk"
        )
        ids = ["light.kitchen", "light.desk"]
        result = await tools.ha_set_entity(
            entity_id=ids, expose_to={"conversation": True}
        )

        assert [row["entity_id"] for row in result["succeeded"]] == ids
        assert [row["entity_entry"]["entity_id"] for row in result["succeeded"]] == ids
        assert [w.split(":")[0] for w in result["warnings"]] == ids
        assert all("connection lost" in w for w in result["warnings"])


class TestRegistryWideWrite:
    async def test_an_unknown_assistant_writes_nothing(
        self, hass: HomeAssistant, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError):
            await tools.ha_set_entity(
                entity_id_parts=_PARTS, expose_new_entities={"bogus": True}
            )

        assert _types(sent) == [_FLAG_GET]
        assert er.async_get(hass).settings.entity_id_parts is None

    async def test_a_failed_write_says_what_was_applied(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        write = core_exposed.ExposedEntities.async_set_expose_new_entities

        def set_expose_new(store: Any, assistant: str, expose_new: bool) -> None:
            if assistant == "cloud.alexa":
                raise HomeAssistantError("boom")
            write(store, assistant, expose_new)

        monkeypatch.setattr(
            core_exposed.ExposedEntities,
            "async_set_expose_new_entities",
            set_expose_new,
        )
        store = hass.data[core_exposed.DATA_EXPOSED_ENTITIES]
        before = {
            a: store.async_get_expose_new_entities(a)
            for a in ("conversation", "cloud.alexa")
        }
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id_parts=_PARTS,
                expose_new_entities={
                    "conversation": not before["conversation"],
                    "cloud.alexa": not before["cloud.alexa"],
                },
            )

        body = _body(exc_info)
        assert body["applied"] == [
            "entity_id_parts",
            "expose_new_entities.conversation",
        ]
        assert body["previous_settings"] == {"entity_id_parts": None}
        assert body["previous_expose_new_entities"] == before
        assert er.async_get(hass).settings.entity_id_parts == tuple(_PARTS)
        assert {a: store.async_get_expose_new_entities(a) for a in before} == {
            "conversation": not before["conversation"],
            "cloud.alexa": before["cloud.alexa"],
        }

    async def test_a_dropped_connection_says_what_was_applied(
        self,
        hass: HomeAssistant,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        tools: EntityTools,
    ) -> None:
        store = hass.data[core_exposed.DATA_EXPOSED_ENTITIES]
        before = store.async_get_expose_new_entities("conversation")

        def drop() -> None:
            monkeypatch.setattr(
                core_client,
                "send_websocket_message",
                AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
            )

        _after_settings_write(monkeypatch, drop)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id_parts=_PARTS, expose_new_entities={"conversation": not before}
            )

        body = _body(exc_info)
        assert body["error"]["code"].startswith("CONNECTION")
        assert body["applied"] == ["entity_id_parts"]
        assert body["previous_settings"] == {"entity_id_parts": None}
        assert "conversation" in body["write_outcome_unknown"]
        assert er.async_get(hass).settings.entity_id_parts == tuple(_PARTS)
        assert store.async_get_expose_new_entities("conversation") is before

    async def test_a_write_never_sent_is_not_unknown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        tools: EntityTools,
    ) -> None:
        def refuse() -> None:
            monkeypatch.setattr(
                core_client,
                "send_websocket_message",
                AsyncMock(side_effect=HomeAssistantCommandNotSent("no connection")),
            )

        _after_settings_write(monkeypatch, refuse)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id_parts=_PARTS, expose_new_entities={"conversation": True}
            )

        body = _body(exc_info)
        assert body["applied"] == ["entity_id_parts"]
        assert "write_outcome_unknown" not in body

    async def test_a_failed_settings_write_applied_nothing(
        self, hass: HomeAssistant, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id_parts=["entity"], expose_new_entities={"conversation": True}
            )

        body = _body(exc_info)
        assert body["applied"] == []
        assert body["previous_settings"] == {"entity_id_parts": None}
        assert _FLAG_SET not in _types(sent)
        assert er.async_get(hass).settings.entity_id_parts is None

    @pytest.mark.parametrize("value", ["nope", "[1]"])
    async def test_a_malformed_flag_map_names_its_parameter(
        self, tools: EntityTools, value: str
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(expose_new_entities=value)

        assert "expose_new_entities" in error_of(exc_info)["message"]

    async def test_an_empty_flag_map_is_refused(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(expose_new_entities={})

        assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
        sent.assert_not_called()

    @pytest.mark.parametrize(
        "entity_param",
        [
            {"name": "Lamp"},
            {"expose_to": {"cloud.alexa": True}},
            {"labels": ["kitchen"]},
            {"categories": {"automation": "c1"}},
        ],
    )
    async def test_entity_params_without_an_entity_are_refused(
        self, tools: EntityTools, sent: AsyncMock, entity_param: dict[str, Any]
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                expose_new_entities={"cloud.alexa": True}, **entity_param
            )

        assert error_of(exc_info)["code"] == "VALIDATION_MISSING_PARAMETER"
        sent.assert_not_called()

    async def test_registry_wide_params_with_an_entity_are_refused(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id="light.a",
                name="Lamp",
                expose_new_entities={"cloud.alexa": True},
            )

        error = error_of(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "registry-wide" in error["message"]
        sent.assert_not_called()

    async def test_no_entity_and_nothing_registry_wide_is_refused(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity()

        assert error_of(exc_info)["code"] == "VALIDATION_MISSING_PARAMETER"
        sent.assert_not_called()


class TestEntityWriteFailure:
    async def test_a_failed_exposure_after_a_registry_write_is_partial(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        _expose_raises(monkeypatch)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id="light.kitchen",
                name="Updated",
                expose_to={"conversation": True},
            )

        body = _body(exc_info)
        assert "boom" in body["error"]["message"]
        assert_default_suggestions(body["error"])
        assert body["partial"] is True
        assert body["entity_entry"]["name"] == "Updated"
        assert body["exposure_succeeded"] == {}
        assert body["exposure_failed"] == {"conversation": True}
        entry = er.async_get(hass).async_get("light.kitchen")
        assert entry is not None
        assert entry.name == "Updated"

    async def test_a_failed_exposure_alone_is_not_partial(
        self, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        _expose_raises(monkeypatch)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id="light.kitchen", expose_to={"conversation": True}
            )

        body = _body(exc_info)
        assert body["success"] is False
        assert "partial" not in body
        assert body["exposure_succeeded"] == {}
        assert body["exposure_failed"] == {"conversation": True}

    async def test_a_failed_second_exposure_names_the_first_as_applied(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        _expose_raises(monkeypatch, only_for=False)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id="light.kitchen",
                expose_to={"cloud.alexa": True, "conversation": False},
            )

        body = _body(exc_info)
        assert body["partial"] is True
        assert body["exposure_succeeded"] == {"cloud.alexa": True}
        assert body["exposure_failed"] == {"conversation": False}
        store = hass.data[core_exposed.DATA_EXPOSED_ENTITIES]
        assert store.async_get_assistant_settings("cloud.alexa")["light.kitchen"] == {
            "should_expose": True
        }

    @pytest.mark.usefixtures("refetch_drops")
    async def test_a_dropped_refetch_keeps_the_written_entry_and_warns(
        self, tools: EntityTools
    ) -> None:
        result = await tools.ha_set_entity(
            entity_id="light.kitchen", name="Porch", expose_to={"conversation": True}
        )

        assert result["success"] is True
        assert result["exposure"] == {"conversation": True}
        assert result["entity_entry"]["name"] == "Porch"
        [warning] = result["warnings"]
        assert "connection lost" in warning

    async def test_a_failed_bulk_exposure_fails_every_id_and_reads_nothing_after(
        self,
        hass: HomeAssistant,
        monkeypatch: pytest.MonkeyPatch,
        tools: EntityTools,
        sent: AsyncMock,
    ) -> None:
        er.async_get(hass).async_get_or_create(
            "light", "demo", "desk", suggested_object_id="desk"
        )
        _expose_raises(monkeypatch)
        result = await tools.ha_set_entity(
            entity_id=["light.kitchen", "light.desk"],
            expose_to={"conversation": True},
        )

        assert result["success"] is False
        assert result["succeeded_count"] == 0
        # One row per id, though every id was in the failed write.
        assert [f["entity_id"] for f in result["failed"]] == [
            "light.kitchen",
            "light.desk",
        ]
        assert all("boom" in f["error"] for f in result["failed"])
        assert _types(sent).count(_EXPOSE) == 1
        assert _types(sent)[-1] == _EXPOSE

    async def test_a_refused_read_before_a_device_rename_ends_the_call(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        """Nothing is written before the read, so Core's refusal is the answer."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id=_BAD_ID, new_device_name="X")

        assert _body(exc_info)["ha_error_code"] == "invalid_format"
        assert _types(sent) == ["config/entity_registry/get"]

    async def test_a_refused_first_options_write_is_not_partial(
        self, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
    ) -> None:
        # Core answers a ValueError here with its invalid_info error.
        monkeypatch.setattr(
            er.EntityRegistry,
            "async_update_entity_options",
            MagicMock(side_effect=ValueError("Options refused")),
        )
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id="light.kitchen", options={"light": {"x": 1}}
            )

        body = _body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["error"]["message"] == (
            "Failed to update options for domain 'light': Options refused"
        )
        assert body["ha_error_code"] == "invalid_info"
        assert body["partial"] is False
        assert body["options_succeeded"] == {}
        # No write applied: an entry here would read as the entity's settings.
        assert "entity_entry" not in body
