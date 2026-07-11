"""Routing tests for ha_config_get_{automation,script,scene} over the component gate.

When the ``ha_mcp_tools`` component advertises the ``config_get`` capability,
each get tool serves the whole read from one ``ha_mcp_tools/config_get``
WebSocket call and skips the legacy REST/WS pipeline (id resolution + per-id
config REST + ``fetch_entity_category`` WS). These tests pin:

- the fast path (legacy fetches never awaited; the ``info`` probe cached),
- storage happy-path byte-parity between the component and legacy responses
  for all three domains,
- ``found:false`` mapping onto the exact legacy not-found error (automation /
  script → ``RESOURCE_NOT_FOUND``; scene → ``ENTITY_NOT_FOUND``, the classified
  404), served without touching the legacy REST fetch,
- the error taxonomy (silent fallback on ``unknown_command``; legacy +
  ``warnings[]`` on any other command error),
- and that a client whose component has no ``config_get`` capability keeps the
  legacy path exactly as before.

The WS client is an ``AsyncMock`` dispatching on the command type; the HA
client is a spy that tallies every legacy fetch so a test can assert it never
ran on the component path. Mirrors ``test_ha_search_component_routing.py``.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantCommandError
from ha_mcp.tools import (
    component_api,
    tools_config_automations,
    tools_config_scenes,
    tools_config_scripts,
)
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

# --- Fixtures / bodies -------------------------------------------------------

# Automation body carries a legacy ``platform`` trigger + ``service`` action so
# the response-parity check also exercises the roundtrip normalization both
# paths run (platform→trigger, singular→plural root keys).
_AUTO_BODY: dict[str, Any] = {
    "id": "171",
    "alias": "Morning Routine",
    "trigger": [{"platform": "time", "at": "07:00:00"}],
    "action": [{"service": "light.turn_on", "target": {"area_id": "bedroom"}}],
}
_SCRIPT_BODY: dict[str, Any] = {
    "alias": "Morning Script",
    "sequence": [{"delay": {"seconds": 5}}],
}
_SCENE_BODY: dict[str, Any] = {
    "id": "movie_night",
    "name": "Movie Night",
    "entities": {"light.living_room": {"state": "on", "brightness": 50}},
}

_CAPS_CONFIG_GET = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["config_get"],
    "limits": {},
}
_CAPS_NO_CONFIG_GET = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["search"],
    "limits": {},
}


class RoutingClient:
    """Credentialed HA client spy: tallies every legacy fetch the get tools make.

    Every legacy fetch is an ``AsyncMock`` so a test can assert
    ``await_count == 0`` on the component path. ``send_websocket_message``
    routes by message type (registry list for id/category resolution) and
    tallies each type.
    """

    def __init__(
        self,
        *,
        automation_config: Any = None,
        automation_config_exc: Exception | None = None,
        script_envelope: Any = None,
        script_envelope_exc: Exception | None = None,
        scene_envelope: Any = None,
        scene_envelope_exc: Exception | None = None,
        categories: dict[str, str] | None = None,
        registry_list: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.ws_types: Counter[str] = Counter()
        self._categories = categories or {}
        self._registry_list = registry_list or []
        self.get_states = AsyncMock(return_value=[])
        self.get_automation_config = AsyncMock(
            return_value=automation_config, side_effect=automation_config_exc
        )
        self.get_script_config = AsyncMock(
            return_value=script_envelope, side_effect=script_envelope_exc
        )
        self.get_scene_config = AsyncMock(
            return_value=scene_envelope, side_effect=scene_envelope_exc
        )
        self.resolve_scene_id = AsyncMock(
            side_effect=lambda sid: sid.removeprefix("scene.")
        )

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type", "")
        self.ws_types[msg_type] += 1
        if msg_type == "config/entity_registry/list":
            return {"success": True, "result": self._registry_list}
        if msg_type == "config/entity_registry/get":
            return {"success": True, "result": {"categories": dict(self._categories)}}
        return {"success": False}

    def legacy_fetch_count(self) -> int:
        """Total awaited legacy REST/WS fetches (for fast-path assertions)."""
        return (
            self.get_states.await_count
            + self.get_automation_config.await_count
            + self.get_script_config.await_count
            + self.get_scene_config.await_count
            + self.resolve_scene_id.await_count
            + sum(self.ws_types.values())
        )


def _make_ws(
    *,
    info_result: dict[str, Any] | None = None,
    info_exc: Exception | None = None,
    config_get_result: dict[str, Any] | None = None,
    config_get_exc: Exception | None = None,
) -> AsyncMock:
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            if info_exc is not None:
                raise info_exc
            return {"success": True, "result": info_result}
        if command_type == "ha_mcp_tools/config_get":
            if config_get_exc is not None:
                raise config_get_exc
            return {"success": True, "result": config_get_result}
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


@contextlib.contextmanager
def _patch_ws(ws: AsyncMock, tool_module: Any) -> Any:
    """Patch ``get_websocket_client`` on both the gate and the tool module."""
    factory = AsyncMock(return_value=ws)
    with (
        patch.object(component_api, "get_websocket_client", factory),
        patch.object(tool_module, "get_websocket_client", factory),
    ):
        yield ws


def _config_get_calls(ws: AsyncMock) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args and c.args[0] == "ha_mcp_tools/config_get"
    ]


def _info_calls(ws: AsyncMock) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args and c.args[0] == "ha_mcp_tools/info"
    ]


def _error_payload(exc: ToolError) -> dict[str, Any]:
    return json.loads(str(exc))["error"]


# --- Component config_get result builders (component wire vocabulary) --------


def _auto_cg_found(category: str | None = "cat_a") -> dict[str, Any]:
    return {
        "found": True,
        "domain": "automation",
        "item_id": "171",
        "entity_id": "automation.morning",
        "source": "storage",
        "config": dict(_AUTO_BODY),
        "category": category,
    }


def _script_cg_found(category: str | None = "cat_s") -> dict[str, Any]:
    return {
        "found": True,
        "domain": "script",
        "item_id": "morning",
        "entity_id": "script.morning",
        "source": "storage",
        "config": dict(_SCRIPT_BODY),
        "category": category,
    }


def _scene_cg_found(category: str | None = "cat_x") -> dict[str, Any]:
    return {
        "found": True,
        "domain": "scene",
        "item_id": "movie_night",
        "entity_id": "scene.movie_night",
        "source": "storage",
        "config": dict(_SCENE_BODY),
        "category": category,
    }


# --- Legacy-serving client builders (no config_get capability) ---------------


def _auto_legacy_client(category: str | None = "cat_a") -> RoutingClient:
    cats = {"automation": category} if category else {}
    return RoutingClient(automation_config=dict(_AUTO_BODY), categories=cats)


def _script_legacy_client(category: str | None = "cat_s") -> RoutingClient:
    cats = {"script": category} if category else {}
    return RoutingClient(
        script_envelope={
            "success": True,
            "script_id": "morning",
            "config": dict(_SCRIPT_BODY),
        },
        categories=cats,
    )


def _scene_legacy_client(category: str | None = "cat_x") -> RoutingClient:
    cats = {"scene": category} if category else {}
    return RoutingClient(
        scene_envelope={
            "success": True,
            "scene_id": "movie_night",
            "config": dict(_SCENE_BODY),
        },
        categories=cats,
        registry_list=[{"entity_id": "scene.movie_night", "unique_id": "movie_night"}],
    )


# =============================================================================
# Automation
# =============================================================================


class TestAutomationConfigGetRouting:
    async def test_fast_path_skips_legacy_fetches(self) -> None:
        """Component serves the read: none of the legacy fetches are awaited."""
        client = _auto_legacy_client()
        ws = _make_ws(info_result=_CAPS_CONFIG_GET, config_get_result=_auto_cg_found())
        with _patch_ws(ws, tools_config_automations):
            resp = await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.morning"
            )
        assert resp["success"] is True
        assert resp["automation_id"] == "automation.morning"
        assert resp["config"]["category"] == "cat_a"
        assert client.legacy_fetch_count() == 0
        assert len(_config_get_calls(ws)) == 1
        # The component receives the domain + the caller identifier verbatim.
        call = _config_get_calls(ws)[0]
        assert call.kwargs == {"domain": "automation", "item_id": "automation.morning"}

    async def test_storage_happy_path_parity(self) -> None:
        """Component and legacy responses are byte-identical for a storage item."""
        comp_client = RoutingClient()  # legacy must not be reached on this path
        ws = _make_ws(info_result=_CAPS_CONFIG_GET, config_get_result=_auto_cg_found())
        with _patch_ws(ws, tools_config_automations):
            component = await AutomationConfigTools(
                comp_client
            ).ha_config_get_automation(identifier="automation.morning")

        legacy_client = _auto_legacy_client()
        ws_legacy = _make_ws(info_result=_CAPS_NO_CONFIG_GET)
        with _patch_ws(ws_legacy, tools_config_automations):
            legacy = await AutomationConfigTools(
                legacy_client
            ).ha_config_get_automation(identifier="automation.morning")

        assert component == legacy

    async def test_found_false_raises_resource_not_found(self) -> None:
        """found:false → the exact RESOURCE_NOT_FOUND, without the legacy REST fetch."""
        client = RoutingClient(registry_list=[])
        ws = _make_ws(info_result=_CAPS_CONFIG_GET, config_get_result={"found": False})
        with (
            _patch_ws(ws, tools_config_automations),
            pytest.raises(ToolError) as exc_info,
        ):
            await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.ghost"
            )
        err = _error_payload(exc_info.value)
        assert err["code"] == "RESOURCE_NOT_FOUND"
        assert err["message"] == "Automation not found: automation.ghost"
        # The component's not-found is authoritative — no legacy config REST call.
        assert client.get_automation_config.await_count == 0

    async def test_unknown_command_falls_back_silently(self) -> None:
        """unknown_command on config_get → legacy path, no fallback warning."""
        client = _auto_legacy_client()
        ws = _make_ws(
            info_result=_CAPS_CONFIG_GET,
            config_get_exc=HomeAssistantCommandError(
                "Command failed: nope", "unknown_command"
            ),
        )
        with _patch_ws(ws, tools_config_automations):
            resp = await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.morning"
            )
        assert resp["success"] is True
        assert client.get_automation_config.await_count == 1
        assert "warnings" not in resp

    async def test_raised_command_falls_back_with_warning(self) -> None:
        """A non-unknown command error → legacy path AND a warnings[] entry."""
        client = _auto_legacy_client()
        ws = _make_ws(
            info_result=_CAPS_CONFIG_GET,
            config_get_exc=HomeAssistantCommandError(
                "Command failed: boom", "internal_error"
            ),
        )
        with _patch_ws(ws, tools_config_automations):
            resp = await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.morning"
            )
        assert resp["success"] is True
        assert client.get_automation_config.await_count == 1
        assert any("served via legacy path" in w for w in resp["warnings"])

    async def test_no_capability_uses_legacy_untouched(self) -> None:
        """A component without config_get → legacy path, config_get never sent."""
        client = _auto_legacy_client()
        ws = _make_ws(info_result=_CAPS_NO_CONFIG_GET)
        with _patch_ws(ws, tools_config_automations):
            resp = await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.morning"
            )
        assert resp["success"] is True
        assert client.get_automation_config.await_count == 1
        assert _config_get_calls(ws) == []

    async def test_caps_probed_once_across_calls(self) -> None:
        """The info probe is cached: two reads, one ha_mcp_tools/info call."""
        client = _auto_legacy_client()
        ws = _make_ws(info_result=_CAPS_CONFIG_GET, config_get_result=_auto_cg_found())
        tools = AutomationConfigTools(client)
        with _patch_ws(ws, tools_config_automations):
            await tools.ha_config_get_automation(identifier="automation.morning")
            await tools.ha_config_get_automation(identifier="automation.morning")
        assert len(_info_calls(ws)) == 1
        assert len(_config_get_calls(ws)) == 2


# =============================================================================
# Script
# =============================================================================


class TestScriptConfigGetRouting:
    async def test_fast_path_skips_legacy_fetches(self) -> None:
        client = _script_legacy_client()
        ws = _make_ws(
            info_result=_CAPS_CONFIG_GET, config_get_result=_script_cg_found()
        )
        with _patch_ws(ws, tools_config_scripts):
            resp = await ConfigScriptTools(client).ha_config_get_script(
                script_id="morning"
            )
        assert resp["success"] is True
        assert resp["script_id"] == "morning"
        # The legacy path returns the REST envelope under ``config``; the
        # component path reconstructs it (with category injected).
        assert resp["config"]["script_id"] == "morning"
        assert resp["config"]["category"] == "cat_s"
        assert client.legacy_fetch_count() == 0
        call = _config_get_calls(ws)[0]
        assert call.kwargs == {"domain": "script", "item_id": "morning"}

    async def test_entity_id_prefix_stripped_before_routing(self) -> None:
        """A ``script.`` prefix is stripped before the component call (parity)."""
        client = RoutingClient()
        ws = _make_ws(
            info_result=_CAPS_CONFIG_GET, config_get_result=_script_cg_found()
        )
        with _patch_ws(ws, tools_config_scripts):
            await ConfigScriptTools(client).ha_config_get_script(
                script_id="script.morning"
            )
        assert _config_get_calls(ws)[0].kwargs["item_id"] == "morning"

    async def test_storage_happy_path_parity(self) -> None:
        comp_client = RoutingClient()
        ws = _make_ws(
            info_result=_CAPS_CONFIG_GET, config_get_result=_script_cg_found()
        )
        with _patch_ws(ws, tools_config_scripts):
            component = await ConfigScriptTools(comp_client).ha_config_get_script(
                script_id="morning"
            )

        legacy_client = _script_legacy_client()
        ws_legacy = _make_ws(info_result=_CAPS_NO_CONFIG_GET)
        with _patch_ws(ws_legacy, tools_config_scripts):
            legacy = await ConfigScriptTools(legacy_client).ha_config_get_script(
                script_id="morning"
            )

        assert component == legacy

    async def test_found_false_raises_resource_not_found(self) -> None:
        client = RoutingClient(registry_list=[])
        ws = _make_ws(info_result=_CAPS_CONFIG_GET, config_get_result={"found": False})
        with (
            _patch_ws(ws, tools_config_scripts),
            pytest.raises(ToolError) as exc_info,
        ):
            await ConfigScriptTools(client).ha_config_get_script(script_id="ghost")
        err = _error_payload(exc_info.value)
        assert err["code"] == "RESOURCE_NOT_FOUND"
        assert err["message"] == "Script not found: ghost"
        assert client.get_script_config.await_count == 0

    async def test_unknown_command_falls_back_silently(self) -> None:
        client = _script_legacy_client()
        ws = _make_ws(
            info_result=_CAPS_CONFIG_GET,
            config_get_exc=HomeAssistantCommandError(
                "Command failed: nope", "unknown_command"
            ),
        )
        with _patch_ws(ws, tools_config_scripts):
            resp = await ConfigScriptTools(client).ha_config_get_script(
                script_id="morning"
            )
        assert resp["success"] is True
        assert client.get_script_config.await_count == 1
        assert "warnings" not in resp

    async def test_raised_command_falls_back_with_warning(self) -> None:
        client = _script_legacy_client()
        ws = _make_ws(
            info_result=_CAPS_CONFIG_GET,
            config_get_exc=HomeAssistantCommandError(
                "Command failed: boom", "internal_error"
            ),
        )
        with _patch_ws(ws, tools_config_scripts):
            resp = await ConfigScriptTools(client).ha_config_get_script(
                script_id="morning"
            )
        assert resp["success"] is True
        assert client.get_script_config.await_count == 1
        assert any("served via legacy path" in w for w in resp["warnings"])

    async def test_no_capability_uses_legacy_untouched(self) -> None:
        client = _script_legacy_client()
        ws = _make_ws(info_result=_CAPS_NO_CONFIG_GET)
        with _patch_ws(ws, tools_config_scripts):
            resp = await ConfigScriptTools(client).ha_config_get_script(
                script_id="morning"
            )
        assert resp["success"] is True
        assert client.get_script_config.await_count == 1
        assert _config_get_calls(ws) == []


# =============================================================================
# Scene
# =============================================================================


class TestSceneConfigGetRouting:
    async def test_fast_path_skips_legacy_fetches(self) -> None:
        client = _scene_legacy_client()
        ws = _make_ws(info_result=_CAPS_CONFIG_GET, config_get_result=_scene_cg_found())
        with _patch_ws(ws, tools_config_scenes):
            resp = await ConfigSceneTools(client).ha_config_get_scene(
                scene_id="movie_night"
            )
        assert resp["success"] is True
        assert resp["scene_id"] == "movie_night"
        assert resp["config"] == _SCENE_BODY
        assert resp["category"] == "cat_x"
        assert client.legacy_fetch_count() == 0
        call = _config_get_calls(ws)[0]
        assert call.kwargs == {"domain": "scene", "item_id": "movie_night"}

    async def test_storage_happy_path_parity(self, monkeypatch) -> None:
        monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)
        comp_client = RoutingClient()
        ws = _make_ws(info_result=_CAPS_CONFIG_GET, config_get_result=_scene_cg_found())
        with _patch_ws(ws, tools_config_scenes):
            component = await ConfigSceneTools(comp_client).ha_config_get_scene(
                scene_id="movie_night"
            )

        legacy_client = _scene_legacy_client()
        ws_legacy = _make_ws(info_result=_CAPS_NO_CONFIG_GET)
        with _patch_ws(ws_legacy, tools_config_scenes):
            legacy = await ConfigSceneTools(legacy_client).ha_config_get_scene(
                scene_id="movie_night"
            )

        assert component == legacy

    async def test_found_false_raises_entity_not_found_parity(
        self, monkeypatch
    ) -> None:
        """Scene found:false → the SAME classified error the legacy 404 produces."""
        monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)

        comp_client = RoutingClient()
        ws = _make_ws(info_result=_CAPS_CONFIG_GET, config_get_result={"found": False})
        with (
            _patch_ws(ws, tools_config_scenes),
            pytest.raises(ToolError) as comp_exc,
        ):
            await ConfigSceneTools(comp_client).ha_config_get_scene(scene_id="ghost")
        # Component's not-found is authoritative — no legacy REST call.
        assert comp_client.get_scene_config.await_count == 0

        legacy_client = RoutingClient(
            scene_envelope_exc=HomeAssistantAPIError(
                "Scene not found: ghost", status_code=404
            )
        )
        ws_legacy = _make_ws(info_result=_CAPS_NO_CONFIG_GET)
        with (
            _patch_ws(ws_legacy, tools_config_scenes),
            pytest.raises(ToolError) as legacy_exc,
        ):
            await ConfigSceneTools(legacy_client).ha_config_get_scene(scene_id="ghost")

        comp_err = _error_payload(comp_exc.value)
        legacy_err = _error_payload(legacy_exc.value)
        assert comp_err["code"] == legacy_err["code"] == "ENTITY_NOT_FOUND"
        assert comp_err["message"] == legacy_err["message"]

    async def test_unknown_command_falls_back_silently(self, monkeypatch) -> None:
        monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)
        client = _scene_legacy_client()
        ws = _make_ws(
            info_result=_CAPS_CONFIG_GET,
            config_get_exc=HomeAssistantCommandError(
                "Command failed: nope", "unknown_command"
            ),
        )
        with _patch_ws(ws, tools_config_scenes):
            resp = await ConfigSceneTools(client).ha_config_get_scene(
                scene_id="movie_night"
            )
        assert resp["success"] is True
        assert client.get_scene_config.await_count == 1
        assert "warnings" not in resp

    async def test_raised_command_falls_back_with_warning(self, monkeypatch) -> None:
        monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)
        client = _scene_legacy_client()
        ws = _make_ws(
            info_result=_CAPS_CONFIG_GET,
            config_get_exc=HomeAssistantCommandError(
                "Command failed: boom", "internal_error"
            ),
        )
        with _patch_ws(ws, tools_config_scenes):
            resp = await ConfigSceneTools(client).ha_config_get_scene(
                scene_id="movie_night"
            )
        assert resp["success"] is True
        assert client.get_scene_config.await_count == 1
        assert any("served via legacy path" in w for w in resp["warnings"])

    async def test_no_capability_uses_legacy_untouched(self, monkeypatch) -> None:
        monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)
        client = _scene_legacy_client()
        ws = _make_ws(info_result=_CAPS_NO_CONFIG_GET)
        with _patch_ws(ws, tools_config_scenes):
            resp = await ConfigSceneTools(client).ha_config_get_scene(
                scene_id="movie_night"
            )
        assert resp["success"] is True
        assert client.get_scene_config.await_count == 1
        assert _config_get_calls(ws) == []
