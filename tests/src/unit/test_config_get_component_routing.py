"""Routing tests for ha_config_get_{automation,script,scene} over the component gate.

When the ``ha_mcp_tools`` component advertises the ``config_get`` capability,
the automation and script get tools serve the whole read from one
``ha_mcp_tools/config_get`` WebSocket call and skip the legacy REST/WS pipeline
(id resolution + per-id config REST + ``fetch_entity_category`` WS). Scenes are
the deliberate exception: ``ha_config_get_scene`` NEVER routes through the
component (a ``HomeAssistantScene`` holds no raw storage body in memory — its
``scene_config.states`` is runtime State objects, not the storage ``entities``
dict), so it stays on the legacy path regardless of caps. These tests pin:

- the automation/script fast path (legacy fetches never awaited; the ``info``
  probe cached),
- storage happy-path byte-parity between the component and legacy responses for
  automations and scripts,
- ``found:false`` mapping onto the exact legacy not-found error (automation /
  script → ``RESOURCE_NOT_FOUND``), served without touching the legacy REST
  fetch,
- the error taxonomy (silent fallback on ``unknown_command``; legacy +
  ``warnings[]`` on any other command error),
- that a client whose component has no ``config_get`` capability keeps the
  legacy path exactly as before,
- and that scenes stay legacy-served even when the component advertises
  ``config_get`` (no ``config_get`` frame is ever sent for a scene), with the
  scene 404 still classified as ``ENTITY_NOT_FOUND`` on that legacy path.

The WS client is an ``AsyncMock`` dispatching on the command type; the HA
client is a spy that tallies every legacy fetch so a test can assert it never
ran on the component path. Mirrors ``test_ha_search_component_routing.py``.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import (
    component_api,
    tools_config_automations,
    tools_config_scripts,
)
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from ._component_routing_helpers import make_ws, patch_ws

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
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result=_auto_cg_found(),
        )
        with patch_ws(ws, tools_config_automations):
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
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result=_auto_cg_found(),
        )
        with patch_ws(ws, tools_config_automations):
            component = await AutomationConfigTools(
                comp_client
            ).ha_config_get_automation(identifier="automation.morning")

        legacy_client = _auto_legacy_client()
        ws_legacy = make_ws("ha_mcp_tools/config_get", info_result=_CAPS_NO_CONFIG_GET)
        with patch_ws(ws_legacy, tools_config_automations):
            legacy = await AutomationConfigTools(
                legacy_client
            ).ha_config_get_automation(identifier="automation.morning")

        assert component == legacy

    async def test_found_false_raises_resource_not_found(self) -> None:
        """found:false → the exact RESOURCE_NOT_FOUND, without the legacy REST fetch."""
        client = RoutingClient(registry_list=[])
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result={"found": False},
        )
        with (
            patch_ws(ws, tools_config_automations),
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
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_exc=HomeAssistantCommandError(
                "Command failed: nope", "unknown_command"
            ),
        )
        with patch_ws(ws, tools_config_automations):
            resp = await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.morning"
            )
        assert resp["success"] is True
        assert client.get_automation_config.await_count == 1
        assert "warnings" not in resp

    async def test_raised_command_falls_back_with_warning(self) -> None:
        """A non-unknown command error → legacy path AND a warnings[] entry."""
        client = _auto_legacy_client()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_exc=HomeAssistantCommandError("Command failed: boom", "internal_error"),
        )
        with patch_ws(ws, tools_config_automations):
            resp = await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.morning"
            )
        assert resp["success"] is True
        assert client.get_automation_config.await_count == 1
        assert any("served via legacy path" in w for w in resp["warnings"])

    async def test_no_capability_uses_legacy_untouched(self) -> None:
        """A component without config_get → legacy path, config_get never sent."""
        client = _auto_legacy_client()
        ws = make_ws("ha_mcp_tools/config_get", info_result=_CAPS_NO_CONFIG_GET)
        with patch_ws(ws, tools_config_automations):
            resp = await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.morning"
            )
        assert resp["success"] is True
        assert client.get_automation_config.await_count == 1
        assert _config_get_calls(ws) == []

    async def test_caps_probed_once_across_calls(self) -> None:
        """The info probe is cached: two reads, one ha_mcp_tools/info call."""
        client = _auto_legacy_client()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result=_auto_cg_found(),
        )
        tools = AutomationConfigTools(client)
        with patch_ws(ws, tools_config_automations):
            await tools.ha_config_get_automation(identifier="automation.morning")
            await tools.ha_config_get_automation(identifier="automation.morning")
        assert len(_info_calls(ws)) == 1
        assert len(_config_get_calls(ws)) == 2

    async def test_command_timeout_falls_back_with_warning(self) -> None:
        """A component WS timeout → legacy path AND a warnings[] entry."""
        client = _auto_legacy_client()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_exc=HomeAssistantCommandTimeout("Command timeout"),
        )
        with patch_ws(ws, tools_config_automations):
            resp = await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.morning"
            )
        assert resp["success"] is True
        assert client.get_automation_config.await_count == 1
        assert any("served via legacy path" in w for w in resp["warnings"])

    async def test_malformed_success_falls_back_to_legacy(self) -> None:
        """found:true with a non-dict config → silent legacy fallback (untrusted)."""
        client = _auto_legacy_client()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result={"found": True, "config": "not-a-dict"},
        )
        with patch_ws(ws, tools_config_automations):
            resp = await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.morning"
            )
        assert resp["success"] is True
        # The malformed component body is not trusted; legacy served the read.
        assert resp["config"]["alias"] == "Morning Routine"
        assert client.get_automation_config.await_count == 1
        # A malformed success is a silent fallback (no component-failure warning).
        assert "warnings" not in resp

    async def test_downgrade_reprobes_and_serves_legacy_without_component(self) -> None:
        """routed OK → unknown_command → invalidate → re-probe serves legacy only.

        A component dropped mid-session: the first call is component-served, the
        second's config_get comes back unknown_command (caps invalidated + silent
        legacy), and the third re-probes ``info`` — now advertising no
        ``config_get`` — so it serves legacy without ever sending a third
        config_get frame.
        """
        client = _auto_legacy_client()
        info_probes = [0]
        config_gets = [0]

        async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
            if command_type == "ha_mcp_tools/info":
                info_probes[0] += 1
                caps = _CAPS_CONFIG_GET if info_probes[0] == 1 else _CAPS_NO_CONFIG_GET
                return {"success": True, "result": caps}
            if command_type == "ha_mcp_tools/config_get":
                config_gets[0] += 1
                if config_gets[0] == 1:
                    return {"success": True, "result": _auto_cg_found()}
                raise HomeAssistantCommandError(
                    "Command failed: gone", "unknown_command"
                )
            raise AssertionError(f"unexpected command {command_type!r}")

        ws = AsyncMock()
        ws.send_command = AsyncMock(side_effect=_send)
        tools = AutomationConfigTools(client)
        with patch_ws(ws, tools_config_automations):
            first = await tools.ha_config_get_automation(
                identifier="automation.morning"
            )
            second = await tools.ha_config_get_automation(
                identifier="automation.morning"
            )
            third = await tools.ha_config_get_automation(
                identifier="automation.morning"
            )

        # Call 1 was component-served (no legacy config fetch yet).
        assert first["config"]["category"] == "cat_a"
        assert second["success"] is True and third["success"] is True
        # config_get was consulted exactly twice (call 1 OK, call 2 unknown); the
        # re-probed call 3 saw no capability and never sent a third frame.
        assert config_gets[0] == 2
        assert info_probes[0] == 2
        # Legacy served both the downgraded call and the re-probed call.
        assert client.get_automation_config.await_count == 2


# =============================================================================
# Script
# =============================================================================


class TestScriptConfigGetRouting:
    async def test_fast_path_skips_legacy_fetches(self) -> None:
        client = _script_legacy_client()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result=_script_cg_found(),
        )
        with patch_ws(ws, tools_config_scripts):
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
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result=_script_cg_found(),
        )
        with patch_ws(ws, tools_config_scripts):
            await ConfigScriptTools(client).ha_config_get_script(
                script_id="script.morning"
            )
        assert _config_get_calls(ws)[0].kwargs["item_id"] == "morning"

    async def test_storage_happy_path_parity(self) -> None:
        comp_client = RoutingClient()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result=_script_cg_found(),
        )
        with patch_ws(ws, tools_config_scripts):
            component = await ConfigScriptTools(comp_client).ha_config_get_script(
                script_id="morning"
            )

        legacy_client = _script_legacy_client()
        ws_legacy = make_ws("ha_mcp_tools/config_get", info_result=_CAPS_NO_CONFIG_GET)
        with patch_ws(ws_legacy, tools_config_scripts):
            legacy = await ConfigScriptTools(legacy_client).ha_config_get_script(
                script_id="morning"
            )

        assert component == legacy

    async def test_found_false_raises_resource_not_found(self) -> None:
        client = RoutingClient(registry_list=[])
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result={"found": False},
        )
        with (
            patch_ws(ws, tools_config_scripts),
            pytest.raises(ToolError) as exc_info,
        ):
            await ConfigScriptTools(client).ha_config_get_script(script_id="ghost")
        err = _error_payload(exc_info.value)
        assert err["code"] == "RESOURCE_NOT_FOUND"
        assert err["message"] == "Script not found: ghost"
        assert client.get_script_config.await_count == 0

    async def test_unknown_command_falls_back_silently(self) -> None:
        client = _script_legacy_client()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_exc=HomeAssistantCommandError(
                "Command failed: nope", "unknown_command"
            ),
        )
        with patch_ws(ws, tools_config_scripts):
            resp = await ConfigScriptTools(client).ha_config_get_script(
                script_id="morning"
            )
        assert resp["success"] is True
        assert client.get_script_config.await_count == 1
        assert "warnings" not in resp

    async def test_raised_command_falls_back_with_warning(self) -> None:
        client = _script_legacy_client()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_exc=HomeAssistantCommandError("Command failed: boom", "internal_error"),
        )
        with patch_ws(ws, tools_config_scripts):
            resp = await ConfigScriptTools(client).ha_config_get_script(
                script_id="morning"
            )
        assert resp["success"] is True
        assert client.get_script_config.await_count == 1
        assert any("served via legacy path" in w for w in resp["warnings"])

    async def test_no_capability_uses_legacy_untouched(self) -> None:
        client = _script_legacy_client()
        ws = make_ws("ha_mcp_tools/config_get", info_result=_CAPS_NO_CONFIG_GET)
        with patch_ws(ws, tools_config_scripts):
            resp = await ConfigScriptTools(client).ha_config_get_script(
                script_id="morning"
            )
        assert resp["success"] is True
        assert client.get_script_config.await_count == 1
        assert _config_get_calls(ws) == []

    async def test_command_timeout_falls_back_with_warning(self) -> None:
        """A component WS timeout → legacy path AND a warnings[] entry."""
        client = _script_legacy_client()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_exc=HomeAssistantCommandTimeout("Command timeout"),
        )
        with patch_ws(ws, tools_config_scripts):
            resp = await ConfigScriptTools(client).ha_config_get_script(
                script_id="morning"
            )
        assert resp["success"] is True
        assert client.get_script_config.await_count == 1
        assert any("served via legacy path" in w for w in resp["warnings"])

    async def test_malformed_success_falls_back_to_legacy(self) -> None:
        """found:true with a non-dict config → silent legacy fallback (untrusted)."""
        client = _script_legacy_client()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result={"found": True, "config": ["not", "a", "dict"]},
        )
        with patch_ws(ws, tools_config_scripts):
            resp = await ConfigScriptTools(client).ha_config_get_script(
                script_id="morning"
            )
        assert resp["success"] is True
        assert resp["config"]["config"] == _SCRIPT_BODY  # legacy REST envelope
        assert client.get_script_config.await_count == 1
        assert "warnings" not in resp


# =============================================================================
# Scene
# =============================================================================


class TestSceneConfigGetAlwaysLegacy:
    """Scenes are deliberately legacy-only: even with the component advertising
    ``config_get``, ``ha_config_get_scene`` never routes through it. A
    ``HomeAssistantScene`` holds ``scene_config.states`` as runtime State
    objects, not the storage ``entities`` dict, so a component-served body would
    break shape parity and ``config_hash`` stability (caught live by the scene
    lifecycle e2e). The tool skips the caps probe entirely and serves the read
    from the legacy REST/WS pipeline — so the component WS is never touched.
    """

    async def test_scene_get_served_by_legacy_despite_config_get_caps(
        self, monkeypatch
    ) -> None:
        """config_get advertised, yet the scene read is fully legacy-served and
        no ``config_get`` frame ever reaches the component."""
        monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)
        client = _scene_legacy_client()
        # A component WS that WOULD serve config_get if asked — the point is that
        # the scene tool never asks it (no caps probe, no config_get frame).
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result=_scene_cg_found(),
        )
        factory = AsyncMock(return_value=ws)
        with patch.object(component_api, "get_websocket_client", factory):
            resp = await ConfigSceneTools(client).ha_config_get_scene(
                scene_id="movie_night"
            )
        assert resp["success"] is True
        assert resp["scene_id"] == "movie_night"
        assert resp["config"] == _SCENE_BODY
        assert resp["category"] == "cat_x"
        # Legacy pipeline ran; the component WS was never touched.
        assert client.get_scene_config.await_count == 1
        assert ws.send_command.await_count == 0

    async def test_scene_not_found_classifies_entity_not_found_on_legacy(
        self, monkeypatch
    ) -> None:
        """A scene 404 stays classified as ENTITY_NOT_FOUND on the legacy path,
        with ``config_get`` advertised but never sent."""
        monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)
        client = RoutingClient(
            scene_envelope_exc=HomeAssistantAPIError(
                "Scene not found: ghost", status_code=404
            )
        )
        ws = make_ws("ha_mcp_tools/config_get", info_result=_CAPS_CONFIG_GET)
        factory = AsyncMock(return_value=ws)
        with (
            patch.object(component_api, "get_websocket_client", factory),
            pytest.raises(ToolError) as exc_info,
        ):
            await ConfigSceneTools(client).ha_config_get_scene(scene_id="ghost")
        err = _error_payload(exc_info.value)
        # The legacy REST 404 is classified via the tool's ``except Exception``
        # (entity_id in context) into the ENTITY_NOT_FOUND taxonomy.
        assert err["code"] == "ENTITY_NOT_FOUND"
        assert err["message"] == "Entity 'scene.ghost' not found"
        assert ws.send_command.await_count == 0
