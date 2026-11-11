"""ha_get_entity / ha_set_entity without an entity, and exposure of an entity
without a registry entry, on a real ``hass``.

Core's own answers for these commands are pinned in
test_entity_registry_real_core.py; these tests pin what the tools send, the
order they send it in, how they shape the registry-wide replies, and how they
report a failure. A failure Core cannot be made to give is one Core function
raising Core's own exception, or the client getting no answer at all.
"""

import json
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
def sent(core_client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """A spy on the client's WebSocket send: it calls through, so Core answers."""
    with patch.object(
        core_client,
        "send_websocket_message",
        wraps=core_client.send_websocket_message,
    ) as spy:
        yield spy


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


def _messages(sent: AsyncMock) -> list[dict[str, Any]]:
    return [call.args[0] for call in sent.call_args_list]


def _types(sent: AsyncMock) -> list[str]:
    return [message["type"] for message in _messages(sent)]


def _body(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return _body(exc_info)["error"]


def _list_raises(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Core's exposure list raises, so its reply is Core's error reply."""
    monkeypatch.setattr(
        core_exposed, "async_get_entity_settings", MagicMock(side_effect=error)
    )


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
        self, hass: HomeAssistant, tools: EntityTools, sent: AsyncMock
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
        assert result["settings"] == {"entity_id_parts": None}
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
        assert _types(sent) == [_SETTINGS_GET, _LIST]

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

        assert "Service unavailable" in _error(exc_info)["message"]

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

        assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
        sent.assert_not_called()

    async def test_an_empty_assistant_filter_goes_to_core(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(exposed_to="")

        assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
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

        error = _error(exc_info)
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

        error = _error(exc_info)
        assert error["code"] != "ENTITY_NOT_FOUND"
        assert "500" in error["message"]

    @pytest.mark.usefixtures("state_read_drops")
    async def test_a_dropped_state_read_is_not_a_not_found(
        self, tools: EntityTools
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(entity_id="light.porch")

        error = _error(exc_info)
        assert error["code"].startswith("CONNECTION")
        assert "server down" in error["message"]

    async def test_another_registry_refusal_keeps_its_kind(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(entity_id=_BAD_ID)

        assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
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

        error = _error(exc_info)
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
        monkeypatch.setattr(
            core_exposed,
            "async_expose_entity",
            MagicMock(side_effect=HomeAssistantError("boom")),
        )
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
        monkeypatch.setattr(entity_registry_reads, "_GET_ENTRIES_CHUNK_SIZE", 1)
        hass.states.async_set("light.porch", "on")
        _list_raises(monkeypatch, Unauthorized())

        # Each id is its own chunk: Core refuses the bad one, answers for the other.
        result = await tools.ha_get_entity(entity_id=[_BAD_ID, "light.porch"])

        errors = {row["entity_id"]: row["error"] for row in result["errors"]}
        assert "entity_ids" in errors[_BAD_ID]
        assert "no registry entry" not in errors[_BAD_ID]
        assert "no registry entry" in errors["light.porch"]


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

        assert "expose_new_entities" in _error(exc_info)["message"]

    async def test_an_empty_flag_map_is_refused(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(expose_new_entities={})

        assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
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

        assert _error(exc_info)["code"] == "VALIDATION_MISSING_PARAMETER"
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

        error = _error(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "registry-wide" in error["message"]
        sent.assert_not_called()

    async def test_no_entity_and_nothing_registry_wide_is_refused(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity()

        assert _error(exc_info)["code"] == "VALIDATION_MISSING_PARAMETER"
        sent.assert_not_called()
