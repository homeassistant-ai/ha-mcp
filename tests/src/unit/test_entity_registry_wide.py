"""ha_get_entity / ha_set_entity without an entity, and exposure of an entity
without a registry entry.

Core's own behaviour for these commands is pinned on a real ``hass`` in
test_entity_registry_real_core.py; these tests pin what the tools send, the
order they send it in, how they shape the registry-wide replies, and how they
report a failure.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import entity_exposure, entity_registry_reads, tools_entities
from ha_mcp.tools.tools_entities import EntityTools

_EXPOSED = {
    "light.living_room": {"conversation": True, "cloud.alexa": True},
    "light.bedroom": {"conversation": True},
    "light.kitchen": {"cloud.google_assistant": True},
}
_LIST = "homeassistant/expose_entity/list"
_FLAG_GET = "homeassistant/expose_new_entities/get"
_FLAG_SET = "homeassistant/expose_new_entities/set"
_SETTINGS_GET = "config/entity_registry/settings/get"
_SETTINGS_UPDATE = "config/entity_registry/settings/update"
_DENIED = {"success": False, "error": "Unauthorized"}
_INVALID = {"success": False, "error": "boom", "error_code": "invalid_format"}


@pytest.fixture(autouse=True)
def _no_component(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core's own list answers; the component path has its routing tests."""
    for module in (entity_exposure, tools_entities):
        monkeypatch.setattr(module, "get_component_caps", AsyncMock(return_value=None))


class FakeCore:
    """Answers each WS command type from a table and records what was sent."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.replies: dict[str, Any] = {
            _SETTINGS_GET: {"success": True, "result": {"entity_id_parts": None}},
            _LIST: {"success": True, "result": {"exposed_entities": _EXPOSED}},
            _FLAG_GET: {"success": True, "result": {"expose_new": True}},
            _FLAG_SET: {"success": True, "result": None},
            _SETTINGS_UPDATE: {
                "success": True,
                "result": {"entity_id_parts": ["device", "entity"]},
            },
            "config/entity_registry/get": {
                "success": False,
                "error": "Entity not found",
                "error_code": "not_found",
            },
            "homeassistant/expose_entity": {"success": True, "result": None},
            "config/entity_registry/get_entries": {
                "success": True,
                "result": {
                    "light.kitchen": {"entity_id": "light.kitchen"},
                    "light.porch": None,
                },
            },
        }
        self.replies.update(replies or {})
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any], **_kwargs: Any) -> Any:
        self.sent.append(message)
        reply = self.replies[message["type"]]
        return reply(message) if callable(reply) else reply

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]


def _tools(
    core: FakeCore,
    has_state: bool = False,
    state_error: HomeAssistantAPIError | None = None,
) -> EntityTools:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=core.__call__)
    if has_state:
        client.get_entity_state = AsyncMock(
            side_effect=lambda entity_id: {"entity_id": entity_id, "state": "on"}
        )
    else:
        client.get_entity_state = AsyncMock(
            side_effect=state_error or HomeAssistantAPIError("missing", status_code=404)
        )
    return EntityTools(client)


def _body(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return _body(exc_info)["error"]


class TestRegistryWideRead:
    @pytest.mark.asyncio
    async def test_lists_every_exposed_entity_with_the_settings(self) -> None:
        core = FakeCore()
        result = await _tools(core).ha_get_entity()

        assert result["success"] is True
        assert result["settings"] == {"entity_id_parts": None}
        assert result["exposed_entities"] == _EXPOSED
        assert result["count"] == 3
        assert result["summary"] == {
            "conversation": 2,
            "cloud.alexa": 1,
            "cloud.google_assistant": 1,
        }
        assert "expose_new_entities" not in result
        assert core.types() == [_SETTINGS_GET, _LIST]

    @pytest.mark.asyncio
    async def test_exposed_to_lists_one_assistant_with_its_flag(self) -> None:
        core = FakeCore()
        result = await _tools(core).ha_get_entity(exposed_to="cloud.alexa")

        assert result["exposed_entities"] == {
            "light.living_room": _EXPOSED["light.living_room"]
        }
        assert result["count"] == 1
        assert result["assistant"] == "cloud.alexa"
        assert result["expose_new_entities"] is True
        assert "summary" not in result
        # Core judges the name first; its reply is the flag.
        assert core.sent[0] == {"type": _FLAG_GET, "assistant": "cloud.alexa"}

    @pytest.mark.asyncio
    async def test_a_failed_list_read_carries_cores_message(self) -> None:
        core = FakeCore({_LIST: {"success": False, "error": "Service unavailable"}})
        with pytest.raises(ToolError) as exc_info:
            await _tools(core).ha_get_entity()

        assert "Service unavailable" in _error(exc_info)["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"entity_id": "light.bedroom", "exposed_to": "conversation"},
            {"unique_id": "abc", "exposed_to": "conversation"},
        ],
    )
    async def test_a_listing_with_entity_arguments_is_refused(
        self, kwargs: dict[str, Any]
    ) -> None:
        core = FakeCore()
        with pytest.raises(ToolError) as exc_info:
            await _tools(core).ha_get_entity(**kwargs)

        assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
        assert core.sent == []

    @pytest.mark.asyncio
    async def test_an_empty_assistant_filter_goes_to_core(self) -> None:
        core = FakeCore({_FLAG_GET: _INVALID})
        with pytest.raises(ToolError) as exc_info:
            await _tools(core).ha_get_entity(exposed_to="")

        assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
        assert core.types() == [_FLAG_GET]


class TestEntityWithoutRegistryEntry:
    """Core's answers are pinned in test_entity_registry_real_core.py; these
    are the failures the real-Core fixture cannot easily stage."""

    @pytest.mark.asyncio
    async def test_a_failed_exposure_read_is_not_a_not_found(self) -> None:
        core = FakeCore({_LIST: _DENIED})
        with pytest.raises(ToolError) as exc_info:
            await _tools(core, has_state=True).ha_get_entity(entity_id="light.porch")

        body = _body(exc_info)
        assert body["error"]["code"] != "ENTITY_NOT_FOUND"
        assert "Unauthorized" in body["error"]["message"]
        assert body["entity_id"] == "light.porch"
        assert body["registry_entry"] is None

    @pytest.mark.asyncio
    async def test_a_refused_exposure_read_is_a_failed_call(self) -> None:
        # The client words Core's refusal with this prefix.
        core = FakeCore({_LIST: {"success": False, "error": "Command failed: boom"}})
        with pytest.raises(ToolError) as exc_info:
            await _tools(core, has_state=True).ha_get_entity(entity_id="light.porch")

        error = _error(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "boom" in error["message"]
        assert error["message"].count("Command failed:") == 1

    @pytest.mark.asyncio
    async def test_a_failed_state_read_is_not_a_not_found(self) -> None:
        core = FakeCore()
        down = HomeAssistantAPIError("server down", status_code=500)
        with pytest.raises(ToolError) as exc_info:
            await _tools(core, state_error=down).ha_get_entity(entity_id="light.porch")

        error = _error(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "server down" in error["message"]

    @pytest.mark.asyncio
    async def test_another_registry_refusal_keeps_its_kind(self) -> None:
        core = FakeCore({"config/entity_registry/get": _INVALID})
        with pytest.raises(ToolError) as exc_info:
            await _tools(core, has_state=True).ha_get_entity(entity_id="light.bedroom")

        error = _error(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "boom" in error["message"]
        assert core.types() == ["config/entity_registry/get"]


class TestStateReply:
    @pytest.mark.asyncio
    async def test_a_state_reply_without_an_entity_id_is_a_failure(self) -> None:
        tools = _tools(FakeCore())
        tools._client.get_entity_state = AsyncMock(return_value={"state": "on"})
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity(entity_id="light.porch")

        error = _error(exc_info)
        assert error["code"] != "ENTITY_NOT_FOUND"
        assert "no entity_id" in str(error)


class TestExposeToWithoutRegistryEntry:
    @pytest.mark.asyncio
    async def test_a_failed_state_read_writes_nothing(self) -> None:
        core = FakeCore()
        down = HomeAssistantAPIError("server down", status_code=500)
        with pytest.raises(ToolError) as exc_info:
            await _tools(core, state_error=down).ha_set_entity(
                entity_id="light.porch", expose_to={"conversation": True}
            )

        error = _error(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "server down" in error["message"]
        assert "homeassistant/expose_entity" not in core.types()

    @pytest.mark.asyncio
    async def test_a_failed_state_read_fails_its_bulk_row_only(self) -> None:
        core = FakeCore()
        down = HomeAssistantAPIError("server down", status_code=500)
        result = await _tools(core, state_error=down).ha_set_entity(
            entity_id=["light.kitchen", "light.porch"],
            expose_to={"conversation": True},
        )

        [failed] = result["failed"]
        assert failed["entity_id"] == "light.porch"
        assert "server down" in failed["error"]
        [write] = [m for m in core.sent if m["type"] == "homeassistant/expose_entity"]
        assert write["entity_ids"] == ["light.kitchen"]

    @pytest.mark.asyncio
    async def test_a_failed_bulk_write_keeps_the_rows_failed_before_it(self) -> None:
        core = FakeCore(
            {"homeassistant/expose_entity": {"success": False, "error": "boom"}}
        )
        result = await _tools(core).ha_set_entity(
            entity_id=["light.kitchen", "light.porch"],
            expose_to={"conversation": True},
        )

        failed = {row["entity_id"]: row["error"] for row in result["failed"]}
        assert failed.keys() == {"light.kitchen", "light.porch"}
        assert "boom" in failed["light.kitchen"]

    @pytest.mark.asyncio
    async def test_a_failed_registry_read_writes_nothing(self) -> None:
        core = FakeCore(
            {
                "config/entity_registry/get_entries": {
                    "success": False,
                    "error": "registry offline",
                }
            }
        )
        result = await _tools(core, has_state=True).ha_set_entity(
            entity_id=["light.kitchen", "light.porch"],
            expose_to={"conversation": True},
        )

        failed = {row["entity_id"]: row["error"] for row in result["failed"]}
        assert failed == {
            "light.kitchen": "registry offline",
            "light.porch": "registry offline",
        }
        assert "homeassistant/expose_entity" not in core.types()


class TestBulkRead:
    @pytest.mark.asyncio
    async def test_a_failed_exposure_read_keeps_the_found_entries(self) -> None:
        core = FakeCore({_LIST: _DENIED})
        result = await _tools(core, has_state=True).ha_get_entity(
            entity_id=["light.kitchen", "light.porch"]
        )

        assert result["entity_entries"] == [{"entity_id": "light.kitchen"}]
        [error] = result["errors"]
        assert error["entity_id"] == "light.porch"
        assert "no registry entry" in error["error"]
        assert "Unauthorized" in error["error"]

    @pytest.mark.asyncio
    async def test_a_failed_lookup_keeps_another_rows_registry_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(entity_registry_reads, "_GET_ENTRIES_CHUNK_SIZE", 1)

        def get_entries(message: dict[str, Any]) -> dict[str, Any]:
            if message["entity_ids"] == ["light.bad"]:
                return {"success": False, "error": "registry offline"}
            return {"success": True, "result": {"light.porch": None}}

        core = FakeCore(
            {_LIST: _DENIED, "config/entity_registry/get_entries": get_entries}
        )
        result = await _tools(core, has_state=True).ha_get_entity(
            entity_id=["light.bad", "light.porch"]
        )

        errors = {row["entity_id"]: row["error"] for row in result["errors"]}
        assert errors["light.bad"] == "registry offline"
        assert "no registry entry" in errors["light.porch"]


class TestRegistryWideWrite:
    @pytest.mark.asyncio
    async def test_an_unknown_assistant_writes_nothing(self) -> None:
        core = FakeCore({_FLAG_GET: _INVALID})
        with pytest.raises(ToolError):
            await _tools(core).ha_set_entity(
                entity_id_parts=["device", "entity"],
                expose_new_entities={"bogus": True},
            )

        assert core.types() == [_FLAG_GET]

    @pytest.mark.asyncio
    async def test_a_failed_write_says_what_was_applied(self) -> None:
        def flag_set(message: dict[str, Any]) -> dict[str, Any]:
            if message["assistant"] == "cloud.alexa":
                return {"success": False, "error": "boom"}
            return {"success": True, "result": None}

        core = FakeCore({_FLAG_SET: flag_set})
        with pytest.raises(ToolError) as exc_info:
            await _tools(core).ha_set_entity(
                entity_id_parts=["device", "entity"],
                expose_new_entities={"conversation": True, "cloud.alexa": False},
            )

        body = _body(exc_info)
        assert body["applied"] == [
            "entity_id_parts",
            "expose_new_entities.conversation",
        ]
        assert body["previous_settings"] == {"entity_id_parts": None}
        assert body["previous_expose_new_entities"] == {
            "conversation": True,
            "cloud.alexa": True,
        }

    @pytest.mark.asyncio
    async def test_a_dropped_connection_says_what_was_applied(self) -> None:
        def flag_set(message: dict[str, Any]) -> dict[str, Any]:
            raise HomeAssistantConnectionError("connection lost")

        core = FakeCore({_FLAG_SET: flag_set})
        with pytest.raises(ToolError) as exc_info:
            await _tools(core).ha_set_entity(
                entity_id_parts=["device", "entity"],
                expose_new_entities={"conversation": True},
            )

        body = _body(exc_info)
        assert body["error"]["code"].startswith("CONNECTION")
        assert body["applied"] == ["entity_id_parts"]
        assert body["previous_settings"] == {"entity_id_parts": None}
        assert "conversation" in body["write_outcome_unknown"]

    @pytest.mark.asyncio
    async def test_a_write_never_sent_is_not_unknown(self) -> None:
        def flag_set(message: dict[str, Any]) -> dict[str, Any]:
            raise HomeAssistantCommandNotSent("no connection")

        core = FakeCore({_FLAG_SET: flag_set})
        with pytest.raises(ToolError) as exc_info:
            await _tools(core).ha_set_entity(
                entity_id_parts=["device", "entity"],
                expose_new_entities={"conversation": True},
            )

        body = _body(exc_info)
        assert body["applied"] == ["entity_id_parts"]
        assert "write_outcome_unknown" not in body

    @pytest.mark.asyncio
    async def test_a_failed_settings_write_applied_nothing(self) -> None:
        core = FakeCore({_SETTINGS_UPDATE: {"success": False, "error": "boom"}})
        with pytest.raises(ToolError) as exc_info:
            await _tools(core).ha_set_entity(
                entity_id_parts=["device", "entity"],
                expose_new_entities={"conversation": True},
            )

        body = _body(exc_info)
        assert body["applied"] == []
        assert body["previous_settings"] == {"entity_id_parts": None}
        assert _FLAG_SET not in core.types()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["nope", "[1]"])
    async def test_a_malformed_flag_map_names_its_parameter(self, value: str) -> None:
        with pytest.raises(ToolError) as exc_info:
            await _tools(FakeCore()).ha_set_entity(expose_new_entities=value)  # type: ignore[arg-type]

        assert "expose_new_entities" in _error(exc_info)["message"]

    @pytest.mark.asyncio
    async def test_an_empty_flag_map_is_refused(self) -> None:
        core = FakeCore()
        with pytest.raises(ToolError) as exc_info:
            await _tools(core).ha_set_entity(expose_new_entities={})

        assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
        assert core.sent == []

    @pytest.mark.asyncio
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
        self, entity_param: dict[str, Any]
    ) -> None:
        core = FakeCore()
        with pytest.raises(ToolError) as exc_info:
            await _tools(core).ha_set_entity(
                expose_new_entities={"cloud.alexa": True}, **entity_param
            )

        assert _error(exc_info)["code"] == "VALIDATION_MISSING_PARAMETER"
        assert core.sent == []

    @pytest.mark.asyncio
    async def test_registry_wide_params_with_an_entity_are_refused(self) -> None:
        core = FakeCore()
        with pytest.raises(ToolError) as exc_info:
            await _tools(core).ha_set_entity(
                entity_id="light.a",
                name="Lamp",
                expose_new_entities={"cloud.alexa": True},
            )

        error = _error(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "registry-wide" in error["message"]
        assert core.sent == []

    @pytest.mark.asyncio
    async def test_no_entity_and_nothing_registry_wide_is_refused(self) -> None:
        core = FakeCore()
        with pytest.raises(ToolError) as exc_info:
            await _tools(core).ha_set_entity()

        assert _error(exc_info)["code"] == "VALIDATION_MISSING_PARAMETER"
        assert core.sent == []
