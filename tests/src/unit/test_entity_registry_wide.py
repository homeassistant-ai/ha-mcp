"""ha_get_entity / ha_set_entity without an entity, and exposure of an entity
without a registry entry.

Core's own behaviour for these commands is pinned on a real ``hass`` in
test_entity_registry_real_core.py; these tests pin what the tools send and
how they shape Core's replies.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools import entity_exposure
from ha_mcp.tools.tools_entities import register_entity_tools

_EXPOSED = {
    "light.living_room": {"conversation": True, "cloud.alexa": True},
    "light.bedroom": {"conversation": True},
    "light.kitchen": {"cloud.google_assistant": True},
}


@pytest.fixture(autouse=True)
def _no_component(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core's own list answers; the component path has its routing tests."""
    monkeypatch.setattr(
        entity_exposure, "get_component_caps", AsyncMock(return_value=None)
    )


class FakeCore:
    """Answers each WS command type from a table and records what was sent."""

    def __init__(self, **replies: dict[str, Any]) -> None:
        self.replies: dict[str, Any] = {
            "config/entity_registry/settings/get": {
                "success": True,
                "result": {"entity_id_parts": None},
            },
            "homeassistant/expose_entity/list": {
                "success": True,
                "result": {"exposed_entities": _EXPOSED},
            },
            "homeassistant/expose_new_entities/get": {
                "success": True,
                "result": {"expose_new": True},
            },
            "homeassistant/expose_new_entities/set": {"success": True, "result": None},
            "config/entity_registry/settings/update": {
                "success": True,
                "result": {"entity_id_parts": ["device", "entity"]},
            },
            "config/entity_registry/get": {
                "success": False,
                "error": "Entity not found",
                "error_code": "not_found",
            },
        }
        self.replies.update({k.replace("__", "/"): v for k, v in replies.items()})
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any], **_kwargs: Any) -> Any:
        self.sent.append(message)
        return self.replies[message["type"]]

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]


def _tools(
    core: FakeCore,
    has_state: bool = False,
    state_error: HomeAssistantAPIError | None = None,
) -> dict[str, Any]:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=core.__call__)
    if has_state:
        client.get_entity_state = AsyncMock(return_value={"state": "on"})
    else:
        client.get_entity_state = AsyncMock(
            side_effect=state_error or HomeAssistantAPIError("missing", status_code=404)
        )
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        fmcp = getattr(method, "__fastmcp__", None)
        registered[(fmcp.name if fmcp else None) or method.__name__] = method

    mcp = MagicMock()
    mcp.add_tool = capture_add_tool
    register_entity_tools(mcp, client)
    return registered


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))["error"]


class TestRegistryWideRead:
    @pytest.mark.asyncio
    async def test_lists_every_exposed_entity_with_the_settings(self) -> None:
        core = FakeCore()
        result = await _tools(core)["ha_get_entity"]()

        assert result["settings"] == {"entity_id_parts": None}
        assert result["exposed_entities"] == _EXPOSED
        assert result["count"] == 3
        assert result["summary"] == {
            "conversation": 2,
            "cloud.alexa": 1,
            "cloud.google_assistant": 1,
        }
        assert "expose_new_entities" not in result

    @pytest.mark.asyncio
    async def test_exposed_to_lists_one_assistant_with_its_flag(self) -> None:
        core = FakeCore()
        result = await _tools(core)["ha_get_entity"](exposed_to="cloud.alexa")

        assert result["exposed_entities"] == {
            "light.living_room": _EXPOSED["light.living_room"]
        }
        assert result["count"] == 1
        assert result["assistant"] == "cloud.alexa"
        assert result["expose_new_entities"] is True
        # Core judges the name first; its reply is the flag.
        assert core.sent[0] == {
            "type": "homeassistant/expose_new_entities/get",
            "assistant": "cloud.alexa",
        }

    @pytest.mark.asyncio
    async def test_an_unknown_assistant_is_cores_refusal(self) -> None:
        core = FakeCore(
            homeassistant__expose_new_entities__get={
                "success": False,
                "error": "value must be one of ['cloud.alexa']",
                "error_code": "invalid_format",
            }
        )
        with pytest.raises(ToolError) as exc_info:
            await _tools(core)["ha_get_entity"](exposed_to="bogus")

        error = _error(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "must be one of" in error["message"]
        assert core.types() == ["homeassistant/expose_new_entities/get"]

    @pytest.mark.asyncio
    async def test_a_failed_list_read_carries_cores_message(self) -> None:
        core = FakeCore(
            homeassistant__expose_entity__list={
                "success": False,
                "error": "Service unavailable",
            }
        )
        with pytest.raises(ToolError) as exc_info:
            await _tools(core)["ha_get_entity"]()

        assert "Service unavailable" in _error(exc_info)["message"]

    @pytest.mark.asyncio
    async def test_exposed_to_with_an_entity_is_refused(self) -> None:
        core = FakeCore()
        with pytest.raises(ToolError) as exc_info:
            await _tools(core)["ha_get_entity"](
                entity_id="light.bedroom", exposed_to="conversation"
            )

        assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
        assert core.sent == []


class TestEntityWithoutRegistryEntry:
    @pytest.mark.asyncio
    async def test_a_stateful_entity_gets_its_stored_exposure(self) -> None:
        core = FakeCore()
        result = await _tools(core, has_state=True)["ha_get_entity"](
            entity_id="light.bedroom"
        )

        assert result == {
            "success": True,
            "entity_id": "light.bedroom",
            "entity_entry": None,
            "exposed_to": {"conversation": True},
            "is_exposed_anywhere": True,
        }

    @pytest.mark.asyncio
    async def test_a_stateful_unexposed_entity_is_exposed_nowhere(self) -> None:
        core = FakeCore()
        result = await _tools(core, has_state=True)["ha_get_entity"](
            entity_id="light.porch"
        )

        assert result["entity_entry"] is None
        assert result["exposed_to"] == {}
        assert result["is_exposed_anywhere"] is False

    @pytest.mark.asyncio
    async def test_a_stored_exposure_answers_without_a_state(self) -> None:
        core = FakeCore()
        result = await _tools(core)["ha_get_entity"](entity_id="light.kitchen")

        assert result["exposed_to"] == {"cloud.google_assistant": True}

    @pytest.mark.asyncio
    async def test_a_failed_state_read_is_not_a_missing_entity(self) -> None:
        core = FakeCore()
        down = HomeAssistantAPIError("server down", status_code=500)
        with pytest.raises(ToolError) as exc_info:
            await _tools(core, state_error=down)["ha_get_entity"](
                entity_id="light.porch"
            )

        assert "server down" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_an_unknown_entity_stays_not_found(self) -> None:
        core = FakeCore()
        with pytest.raises(ToolError) as exc_info:
            await _tools(core)["ha_get_entity"](entity_id="light.nowhere")

        assert "Entity not found" in _error(exc_info)["message"]

    @pytest.mark.asyncio
    async def test_another_registry_failure_reads_no_exposure(self) -> None:
        core = FakeCore(
            config__entity_registry__get={"success": False, "error": "boom"}
        )
        with pytest.raises(ToolError) as exc_info:
            await _tools(core, has_state=True)["ha_get_entity"](
                entity_id="light.bedroom"
            )

        assert "boom" in _error(exc_info)["message"]
        assert core.types() == ["config/entity_registry/get"]


class TestRegistryWideWrite:
    @pytest.mark.asyncio
    async def test_expose_new_entities_reports_the_flag_it_replaces(self) -> None:
        core = FakeCore()
        result = await _tools(core)["ha_set_entity"](
            expose_new_entities={"cloud.alexa": False}
        )

        assert core.sent == [
            {
                "type": "homeassistant/expose_new_entities/get",
                "assistant": "cloud.alexa",
            },
            {
                "type": "homeassistant/expose_new_entities/set",
                "assistant": "cloud.alexa",
                "expose_new": False,
            },
        ]
        assert result["expose_new_entities"] == {"cloud.alexa": False}
        assert result["previous_expose_new_entities"] == {"cloud.alexa": True}

    @pytest.mark.asyncio
    async def test_entity_id_parts_reports_the_settings_it_replaces(self) -> None:
        core = FakeCore()
        result = await _tools(core)["ha_set_entity"](
            entity_id_parts=["device", "entity"]
        )

        assert core.sent[-1] == {
            "type": "config/entity_registry/settings/update",
            "entity_id_parts": ["device", "entity"],
        }
        assert result["settings"] == {"entity_id_parts": ["device", "entity"]}
        assert result["previous_settings"] == {"entity_id_parts": None}

    @pytest.mark.asyncio
    async def test_an_empty_list_restores_cores_default(self) -> None:
        core = FakeCore()
        await _tools(core)["ha_set_entity"](entity_id_parts=[])

        assert core.sent[-1]["entity_id_parts"] is None

    @pytest.mark.asyncio
    async def test_a_refused_list_is_cores_refusal(self) -> None:
        core = FakeCore(
            config__entity_registry__settings__update={
                "success": False,
                "error": "value must contain device",
                "error_code": "invalid_format",
            }
        )
        with pytest.raises(ToolError) as exc_info:
            await _tools(core)["ha_set_entity"](entity_id_parts=["entity"])

        error = _error(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "must contain device" in error["message"]

    @pytest.mark.asyncio
    async def test_a_failed_flag_write_after_the_parts_says_so(self) -> None:
        core = FakeCore(
            homeassistant__expose_new_entities__set={"success": False, "error": "boom"}
        )
        with pytest.raises(ToolError) as exc_info:
            await _tools(core)["ha_set_entity"](
                entity_id_parts=["device", "entity"],
                expose_new_entities={"conversation": True},
            )

        assert "entity_id_parts already updated" in _error(exc_info)["message"]

    @pytest.mark.asyncio
    async def test_registry_wide_and_entity_params_do_not_mix(self) -> None:
        core = FakeCore()
        tools = _tools(core)
        with pytest.raises(ToolError) as with_entity:
            await tools["ha_set_entity"](
                entity_id="light.a",
                name="Lamp",
                expose_new_entities={"cloud.alexa": True},
            )
        with pytest.raises(ToolError) as without_entity:
            await tools["ha_set_entity"](
                name="Lamp", expose_new_entities={"cloud.alexa": True}
            )

        assert _error(with_entity)["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "registry-wide" in _error(with_entity)["message"]
        assert _error(without_entity)["code"] == "VALIDATION_MISSING_PARAMETER"
        assert core.sent == []

    @pytest.mark.asyncio
    async def test_no_entity_and_nothing_registry_wide_is_refused(self) -> None:
        core = FakeCore()
        with pytest.raises(ToolError) as exc_info:
            await _tools(core)["ha_set_entity"]()

        assert _error(exc_info)["code"] == "VALIDATION_MISSING_PARAMETER"
        assert core.sent == []
