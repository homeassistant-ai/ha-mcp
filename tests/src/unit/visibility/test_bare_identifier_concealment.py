"""Enforce mode conceals hidden scripts, scenes and automations addressed by bare key.

The script, scene and automation tools accept the bare storage key
(``unlock_house``) or the config ``id`` (the registry ``unique_id``) and derive
the entity themselves, and Home Assistant exposes each script as a
``script.<storage key>`` service, so a hidden entity was reachable by an
argument that never spells out its entity_id.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.visibility.identifier_aliases import BARE_IDENTIFIER_ARGS

from ..test_container_param_coercion_complete import _all_registered_tools
from .test_enforcement import (
    FakeClient,
    _error_body,
    _returns,
    _unreached_call_next,
    make_context,
    make_mw,
    text_result,
)


def _registry(*entries: dict[str, str]) -> dict[str, Any]:
    return {"success": True, "result": list(entries)}


def _entry(entity_id: str, unique_id: str, platform: str) -> dict[str, str]:
    return {"entity_id": entity_id, "unique_id": unique_id, "platform": platform}


class TestBareIdentifierConcealment:
    @pytest.mark.parametrize(
        ("tool", "args", "registry_entry"),
        [
            pytest.param(
                "ha_config_set_script",
                {"script_id": "unlock_house", "run": "start"},
                _entry("script.unlock_house", "unlock_house", "script"),
                id="script-bare-object-id",
            ),
            pytest.param(
                "ha_config_get_script",
                {"script_id": "unlock_house"},
                _entry("script.front_door", "unlock_house", "script"),
                id="renamed-script-storage-key",
            ),
            pytest.param(
                "ha_call_service",
                {"domain": "script", "service": "unlock_house"},
                _entry("script.front_door", "unlock_house", "script"),
                id="script-service-named-by-storage-key",
            ),
            pytest.param(
                "ha_config_set_scene",
                {"scene_id": "1727266720448", "activate": True},
                _entry("scene.open_up", "1727266720448", "homeassistant"),
                id="scene-config-id",
            ),
            # The tools fall back to ``scene.<id>``, and Home Assistant
            # lowercases an entity_id, so the object ID matches in any case.
            pytest.param(
                "ha_config_set_scene",
                {"scene_id": "Open_Up", "activate": True},
                _entry("scene.open_up", "1727266720448", "homeassistant"),
                id="scene-object-id-in-other-case",
            ),
            pytest.param(
                "ha_config_set_automation",
                {"identifier": "1700000000001", "run_actions": True},
                _entry("automation.buzz_door", "1700000000001", "automation"),
                id="automation-config-id",
            ),
        ],
    )
    async def test_hidden_entity_reached_by_bare_identifier_is_concealed(
        self,
        set_config: Callable[..., Any],
        tool: str,
        args: dict[str, Any],
        registry_entry: dict[str, str],
    ) -> None:
        set_config(
            enabled=True, enforce=True, deny_entity_ids=[registry_entry["entity_id"]]
        )
        client = FakeClient(registry=_registry(registry_entry))
        mw = make_mw(get_client=lambda: client)
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context(tool, args), _unreached_call_next)
        assert _error_body(exc)["error"]["code"] == "ENTITY_NOT_FOUND"

    @pytest.mark.parametrize(
        ("hidden", "registry_entries", "tool", "args"),
        [
            pytest.param(
                "script.unlock_house",
                [
                    _entry("script.unlock_house", "unlock_house", "script"),
                    _entry("script.wake_up", "wake_up", "script"),
                ],
                "ha_config_set_script",
                {"script_id": "wake_up", "run": "start"},
                id="visible-script-bare-key",
            ),
            pytest.param(
                "script.unlock_house",
                [_entry("script.unlock_house", "unlock_house", "script")],
                "ha_config_get_scene",
                {"scene_id": "unlock_house"},
                id="same-key-in-another-domain",
            ),
            pytest.param(
                "scene.open_up",
                [
                    _entry("scene.open_up", "Door", "homeassistant"),
                    _entry("scene.porch", "door", "homeassistant"),
                ],
                "ha_config_get_scene",
                {"scene_id": "door"},
                id="visible-scene-config-id-differing-only-in-case",
            ),
            pytest.param(
                "script.front_door",
                [
                    _entry("script.front_door", "unlock_house", "script"),
                    _entry("script.front_door_2", "front_door", "script"),
                ],
                "ha_call_service",
                {"domain": "script", "service": "front_door"},
                id="visible-script-service-named-like-hidden-object-id",
            ),
        ],
    )
    async def test_visible_entity_sharing_a_hidden_alias_stays_reachable(
        self,
        set_config: Callable[..., Any],
        hidden: str,
        registry_entries: list[dict[str, str]],
        tool: str,
        args: dict[str, Any],
    ) -> None:
        set_config(enabled=True, enforce=True, deny_entity_ids=[hidden])
        client = FakeClient(registry=_registry(*registry_entries))
        mw = make_mw(get_client=lambda: client)
        result = await mw.on_call_tool(
            make_context(tool, args), _returns(text_result("ran"))
        )
        assert result.content[0].text == "ran"


def _takes_bare_identifier(tool_name: str, param_name: str) -> bool:
    if param_name in ("script_id", "scene_id"):
        return True
    return param_name == "identifier" and "automation" in tool_name


def test_every_bare_identifier_tool_is_concealed_by_enforce_mode() -> None:
    tools = _all_registered_tools()
    assert tools, "no tools registered — guardrail cannot run"

    discovered = {
        (tool_name, param_name)
        for tool_name, tool in tools.items()
        for param_name in inspect.signature(tool.fn).parameters
        if _takes_bare_identifier(tool_name, param_name)
    }
    covered = {(tool, arg) for tool, (arg, _domain) in BARE_IDENTIFIER_ARGS.items()}

    assert discovered == covered, (
        "BARE_IDENTIFIER_ARGS is out of sync with the registered tools.\n"
        f"  missing (hidden entity reachable by bare key): {sorted(discovered - covered)}\n"
        f"  stale (tool or parameter no longer exists): {sorted(covered - discovered)}"
    )
