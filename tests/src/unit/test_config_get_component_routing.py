"""Always-legacy pins for ha_config_get_{automation,script,scene}.

The ``ha_mcp_tools`` component's in-process ``config_get`` was withdrawn before
release: it served an entity's ``raw_config``, which is only the storage body as
of the last COMPLETED async reload, with no version marker to tell a fresh body
from a stale one. A get racing a reload returned the pre-edit body and broke the
get -> python_transform -> set round-trip (caught live by the automation/script
config e2e on the arm/HAOS runners). All three get tools now serve their reads
from the legacy REST/WS pipeline (which reads the fresh config FILE), exactly as
scenes always have. Scenes were already legacy-only for a different reason — a
``HomeAssistantScene`` holds no raw storage body in memory at all — so they lead
the pattern the automation/script gets now follow. A file-reading ``config_get``
may return later (issue #1813).

These pin, for all three domains, that:

- the get is fully served by the legacy path EVEN WHEN the component advertises
  ``config_get`` — the tool skips the caps probe entirely, so the component WS is
  never touched (no ``info`` frame, no ``config_get`` frame),
- a missing item still maps onto the exact legacy not-found error (automation /
  script -> ``RESOURCE_NOT_FOUND``; scene -> ``ENTITY_NOT_FOUND``), served
  without any component involvement.

The WS client is an ``AsyncMock`` dispatching on the command type (built so it
WOULD serve ``config_get`` if asked — the point is that it is never asked); the
HA client is a spy that tallies every legacy fetch. Mirrors the scene precedent.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools import component_api
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from ._component_routing_helpers import make_ws

# --- Fixtures / bodies -------------------------------------------------------

# Automation body carries a legacy ``platform`` trigger + ``service`` action so
# the response check also exercises the roundtrip normalization the legacy path
# runs (platform->trigger, singular->plural root keys).
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

# A component advertising the full (now-defunct) capability set INCLUDING
# config_get. The pins wire a WS that would serve it precisely to prove the get
# tools never reach for it.
_CAPS_CONFIG_GET = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["config_get"],
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


def _error_payload(exc: ToolError) -> dict[str, Any]:
    return json.loads(str(exc))["error"]


# --- Component config_get result builders (component wire vocabulary) --------
# These are what the mocked WS WOULD return if a get tool asked for config_get.
# It never does — they exist only to make "the WS was never consulted" a
# meaningful assertion (the WS is fully capable of answering).


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


# --- Legacy-serving client builders ------------------------------------------


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


class TestAutomationConfigGetAlwaysLegacy:
    """Automation gets are legacy-only: the component's config_get was withdrawn
    (its ``raw_config`` freshness lagged the config file between write and
    reload), so ``ha_config_get_automation`` never routes through it. The tool
    skips the caps probe entirely and serves the read from the legacy REST/WS
    pipeline — so the component WS is never touched, even with ``config_get``
    advertised.
    """

    async def test_get_served_by_legacy_despite_config_get_caps(self) -> None:
        """config_get advertised, yet the automation read is fully legacy-served
        and no frame ever reaches the component."""
        client = _auto_legacy_client()
        # A component WS that WOULD serve config_get if asked — the point is that
        # the automation tool never asks it (no caps probe, no config_get frame).
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result=_auto_cg_found(),
        )
        factory = AsyncMock(return_value=ws)
        with patch.object(component_api, "get_websocket_client", factory):
            resp = await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.morning"
            )
        assert resp["success"] is True
        assert resp["automation_id"] == "automation.morning"
        # Legacy path canonicalizes (platform->trigger, singular->plural) and
        # injects the category from the registry.
        assert resp["config"]["triggers"] == [{"trigger": "time", "at": "07:00:00"}]
        assert resp["config"]["category"] == "cat_a"
        # Legacy pipeline ran; the component WS was never touched.
        assert client.get_automation_config.await_count == 1
        assert ws.send_command.await_count == 0

    async def test_not_found_raises_resource_not_found_on_legacy(self) -> None:
        """A missing automation raises the exact legacy RESOURCE_NOT_FOUND, with
        ``config_get`` advertised but never sent."""
        client = RoutingClient(
            automation_config_exc=HomeAssistantAPIError(
                "Automation not found", status_code=404
            ),
            registry_list=[],
        )
        ws = make_ws("ha_mcp_tools/config_get", info_result=_CAPS_CONFIG_GET)
        factory = AsyncMock(return_value=ws)
        with (
            patch.object(component_api, "get_websocket_client", factory),
            pytest.raises(ToolError) as exc_info,
        ):
            await AutomationConfigTools(client).ha_config_get_automation(
                identifier="automation.ghost"
            )
        err = _error_payload(exc_info.value)
        assert err["code"] == "RESOURCE_NOT_FOUND"
        assert err["message"] == "Automation not found: automation.ghost"
        assert ws.send_command.await_count == 0


# =============================================================================
# Script
# =============================================================================


class TestScriptConfigGetAlwaysLegacy:
    """Script analog of the automation pins — always legacy-served, component WS
    never consulted even with ``config_get`` advertised.
    """

    async def test_get_served_by_legacy_despite_config_get_caps(self) -> None:
        client = _script_legacy_client()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result=_script_cg_found(),
        )
        factory = AsyncMock(return_value=ws)
        with patch.object(component_api, "get_websocket_client", factory):
            resp = await ConfigScriptTools(client).ha_config_get_script(
                script_id="morning"
            )
        assert resp["success"] is True
        assert resp["script_id"] == "morning"
        # The legacy path returns the REST envelope under ``config`` (script_id +
        # category injected).
        assert resp["config"]["script_id"] == "morning"
        assert resp["config"]["category"] == "cat_s"
        assert client.get_script_config.await_count == 1
        assert ws.send_command.await_count == 0

    async def test_entity_id_prefix_stripped_on_legacy(self) -> None:
        """A ``script.`` prefix is stripped before the legacy fetch (parity),
        with the component still never consulted."""
        client = _script_legacy_client()
        ws = make_ws(
            "ha_mcp_tools/config_get",
            info_result=_CAPS_CONFIG_GET,
            cmd_result=_script_cg_found(),
        )
        factory = AsyncMock(return_value=ws)
        with patch.object(component_api, "get_websocket_client", factory):
            await ConfigScriptTools(client).ha_config_get_script(
                script_id="script.morning"
            )
        assert client.get_script_config.call_args.args == ("morning",)
        assert ws.send_command.await_count == 0

    async def test_not_found_raises_resource_not_found_on_legacy(self) -> None:
        client = RoutingClient(
            script_envelope_exc=HomeAssistantAPIError(
                "Script not found", status_code=404
            ),
            registry_list=[],
        )
        ws = make_ws("ha_mcp_tools/config_get", info_result=_CAPS_CONFIG_GET)
        factory = AsyncMock(return_value=ws)
        with (
            patch.object(component_api, "get_websocket_client", factory),
            pytest.raises(ToolError) as exc_info,
        ):
            await ConfigScriptTools(client).ha_config_get_script(script_id="ghost")
        err = _error_payload(exc_info.value)
        assert err["code"] == "RESOURCE_NOT_FOUND"
        assert err["message"] == "Script not found: ghost"
        assert ws.send_command.await_count == 0


# =============================================================================
# Scene
# =============================================================================


class TestSceneConfigGetAlwaysLegacy:
    """Scenes have always been legacy-only, for a different reason than the
    withdrawn config_get: a ``HomeAssistantScene`` holds ``scene_config.states``
    as runtime State objects, not the storage ``entities`` dict, so a
    component-served body would break shape parity and ``config_hash`` stability
    (caught live by the scene lifecycle e2e). Like the automation/script gets,
    ``ha_config_get_scene`` skips the caps probe entirely and serves the read
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
