"""Enforce mode conceals hidden scripts, scenes and automations addressed by bare key.

The script, scene and automation tools accept the bare storage key
(``unlock_house``) or the config ``id`` (the registry ``unique_id``) and derive
the entity themselves, and Home Assistant exposes each script as a
``script.<storage key>`` service, so a hidden entity was reachable by an
argument that never spells out its entity_id.
"""

from __future__ import annotations

import inspect

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


def _registry(*entries):
    return {"success": True, "result": list(entries)}


class TestBareIdentifierConcealment:
    @pytest.mark.parametrize(
        ("tool", "args", "registry_entry"),
        [
            pytest.param(
                "ha_config_set_script",
                {"script_id": "unlock_house", "run": "start"},
                {
                    "entity_id": "script.unlock_house",
                    "unique_id": "unlock_house",
                    "platform": "script",
                },
                id="script-bare-object-id",
            ),
            pytest.param(
                "ha_config_get_script",
                {"script_id": "unlock_house"},
                {
                    "entity_id": "script.front_door",
                    "unique_id": "unlock_house",
                    "platform": "script",
                },
                id="renamed-script-storage-key",
            ),
            pytest.param(
                "ha_call_service",
                {"domain": "script", "service": "unlock_house"},
                {
                    "entity_id": "script.front_door",
                    "unique_id": "unlock_house",
                    "platform": "script",
                },
                id="script-service-named-by-storage-key",
            ),
            pytest.param(
                "ha_config_set_scene",
                {"scene_id": "1727266720448", "activate": True},
                {
                    "entity_id": "scene.open_up",
                    "unique_id": "1727266720448",
                    "platform": "homeassistant",
                },
                id="scene-config-id",
            ),
            pytest.param(
                "ha_config_set_automation",
                {"identifier": "1700000000001", "run_actions": True},
                {
                    "entity_id": "automation.buzz_door",
                    "unique_id": "1700000000001",
                    "platform": "automation",
                },
                id="automation-config-id",
            ),
        ],
    )
    async def test_hidden_entity_reached_by_bare_identifier_is_concealed(
        self, set_config, tool, args, registry_entry
    ):
        set_config(
            enabled=True, enforce=True, deny_entity_ids=[registry_entry["entity_id"]]
        )
        client = FakeClient(registry=_registry(registry_entry))
        mw = make_mw(get_client=lambda: client)
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context(tool, args), _unreached_call_next)
        assert _error_body(exc)["error"]["code"] == "ENTITY_NOT_FOUND"

    async def test_bare_identifier_of_visible_script_runs(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["script.unlock_house"])
        client = FakeClient(
            registry=_registry(
                {
                    "entity_id": "script.unlock_house",
                    "unique_id": "unlock_house",
                    "platform": "script",
                },
                {
                    "entity_id": "script.wake_up",
                    "unique_id": "wake_up",
                    "platform": "script",
                },
            )
        )
        mw = make_mw(get_client=lambda: client)
        result = await mw.on_call_tool(
            make_context(
                "ha_config_set_script", {"script_id": "wake_up", "run": "start"}
            ),
            _returns(text_result("started")),
        )
        assert result.content[0].text == "started"

    async def test_hidden_script_key_does_not_hide_same_named_scene(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["script.unlock_house"])
        client = FakeClient(
            registry=_registry(
                {
                    "entity_id": "script.unlock_house",
                    "unique_id": "unlock_house",
                    "platform": "script",
                }
            )
        )
        mw = make_mw(get_client=lambda: client)
        result = await mw.on_call_tool(
            make_context("ha_config_get_scene", {"scene_id": "unlock_house"}),
            _returns(text_result("scene body")),
        )
        assert result.content[0].text == "scene body"


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
