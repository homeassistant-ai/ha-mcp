"""Enforce mode conceals hidden scripts, scenes and automations addressed by bare key.

The script, scene and automation tools accept the bare storage key
(``unlock_house``) or the config ``id`` (the registry ``unique_id``) and derive
the entity themselves, and Home Assistant exposes each script as a
``script.<storage key>`` service, so a hidden entity was reachable by an
argument that never spells out its entity_id.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools
from ha_mcp.visibility.identifier_aliases import (
    BARE_IDENTIFIER_ARGS,
    PREFIXED_IDENTIFIER_ARGS,
)

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


# A hidden script renamed from ``come_home``: its storage key, service name and
# trace item_id stay ``come_home`` while its entity_id is ``script.front_door``.
_RENAMED_HIDDEN_SCRIPT = _entry("script.front_door", "come_home", "script")


def _enforce_hiding(
    set_config: Callable[..., Any], hidden: str, *entries: dict[str, str]
) -> Any:
    set_config(enabled=True, enforce=True, deny_entity_ids=[hidden])
    client = FakeClient(registry=_registry(*entries))
    return make_mw(get_client=lambda: client)


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
            pytest.param(
                "ha_config_get_script",
                {"script_id": "front_door"},
                _RENAMED_HIDDEN_SCRIPT,
                id="renamed-script-new-object-id",
            ),
            pytest.param(
                "ha_config_get_script",
                {"script_id": "script.come_home"},
                _RENAMED_HIDDEN_SCRIPT,
                id="renamed-script-prefixed-storage-key",
            ),
            # Traces are keyed by storage key, and the trace tool falls back to
            # the bare object ID when no entity has the given entity_id.
            pytest.param(
                "ha_get_automation_traces",
                {"automation_id": "script.come_home"},
                _RENAMED_HIDDEN_SCRIPT,
                id="trace-by-script-storage-key",
            ),
            pytest.param(
                "ha_call_service",
                {
                    "ws_command": "trace/get",
                    "data": json.dumps(
                        {"domain": "script", "item_id": "come_home", "run_id": "1"}
                    ),
                },
                _RENAMED_HIDDEN_SCRIPT,
                id="raw-trace-command-by-storage-key",
            ),
            # The check must see the proxy-unwrapped call, not the envelope.
            pytest.param(
                "ha_call_write_tool",
                {
                    "name": "ha_config_set_script",
                    "arguments": json.dumps({"script_id": "come_home", "run": "start"}),
                },
                _RENAMED_HIDDEN_SCRIPT,
                id="proxied-bare-storage-key",
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
            # The automation tools read a bare identifier only as a config id.
            pytest.param(
                "automation.door_auto",
                [
                    _entry("automation.door_auto", "1700000000001", "automation"),
                    _entry("automation.kitchen_motion", "door_auto", "automation"),
                ],
                "ha_config_get_automation",
                {"identifier": "door_auto"},
                id="visible-automation-config-id-like-hidden-object-id",
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


class TestHiddenScriptStorageKeyInContent:
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            pytest.param(
                "ha_config_set_script",
                {
                    "script_id": "visible_one",
                    "config": {"sequence": [{"action": "script.come_home"}]},
                },
                id="script-calling-hidden-script-service",
            ),
            pytest.param(
                "ha_config_set_automation",
                {
                    "config": {
                        "alias": "Run it",
                        "triggers": [],
                        "actions": [{"action": "script.come_home"}],
                    },
                    "run_actions": True,
                },
                id="automation-calling-hidden-script-service",
            ),
        ],
    )
    async def test_config_body_running_a_renamed_hidden_script_is_refused(
        self, set_config: Callable[..., Any], tool: str, args: dict[str, Any]
    ) -> None:
        mw = _enforce_hiding(set_config, "script.front_door", _RENAMED_HIDDEN_SCRIPT)
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context(tool, args), _unreached_call_next)
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_raw_trace_listing_naming_a_hidden_script_is_refused(
        self, set_config: Callable[..., Any]
    ) -> None:
        mw = _enforce_hiding(set_config, "script.front_door", _RENAMED_HIDDEN_SCRIPT)
        listing = {
            "success": True,
            "result": [
                {"domain": "script", "item_id": "wake_up", "run_id": "1"},
                {"domain": "script", "item_id": "come_home", "run_id": "2"},
            ],
        }
        context = make_context(
            "ha_call_service",
            {"ws_command": "trace/list", "data": {"domain": "script"}},
        )
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                context, _returns(text_result(json.dumps(listing), listing))
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"


class _NotFoundClient(FakeClient):
    """Answers every config fetch with a 404, as for a genuinely absent key."""

    async def get_script_config(self, script_id: str) -> dict[str, Any]:
        raise HomeAssistantAPIError(f"Script not found: {script_id}", status_code=404)

    async def get_automation_config(self, identifier: str) -> dict[str, Any]:
        raise HomeAssistantAPIError(f"Not found: {identifier}", status_code=404)


@pytest.mark.parametrize(
    ("hidden", "visible", "list_key", "call_tool"),
    [
        pytest.param(
            _entry("script.unlock_house", "unlock_house", "script"),
            _entry("script.wake_up", "wake_up", "script"),
            "available_script_ids",
            lambda client: ConfigScriptTools(client).ha_config_get_script(
                script_id="no_such"
            ),
            id="script",
        ),
        pytest.param(
            _entry("automation.buzz_door", "1700000000001", "automation"),
            _entry("automation.kitchen_motion", "1700000000002", "automation"),
            "available_automation_ids",
            lambda client: AutomationConfigTools(client).ha_config_get_automation(
                identifier="no_such"
            ),
            id="automation",
        ),
    ],
)
async def test_genuine_not_found_lists_only_visible_ids(
    set_config: Callable[..., Any],
    hidden: dict[str, str],
    visible: dict[str, str],
    list_key: str,
    call_tool: Callable[[Any], Awaitable[Any]],
) -> None:
    set_config(enabled=True, enforce=True, deny_entity_ids=[hidden["entity_id"]])
    client = _NotFoundClient(registry=_registry(hidden, visible))
    mw = make_mw(get_client=lambda: client)

    async def _run_tool(_context: Any) -> Any:
        return await call_tool(client)

    with pytest.raises(ToolError) as exc:
        await mw.on_call_tool(make_context("ha_config_get_script"), _run_tool)
    body = _error_body(exc)
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    listed = body[list_key]
    hidden_object_id = hidden["entity_id"].partition(".")[2]
    assert listed, body
    assert all(hidden_object_id not in item for item in listed)


def _takes_bare_identifier(tool_name: str, param_name: str) -> bool:
    if param_name in ("script_id", "scene_id", "automation_id"):
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
    covered |= set(PREFIXED_IDENTIFIER_ARGS.items())

    assert discovered == covered, (
        "BARE_IDENTIFIER_ARGS is out of sync with the registered tools.\n"
        f"  missing (hidden entity reachable by bare key): {sorted(discovered - covered)}\n"
        f"  stale (tool or parameter no longer exists): {sorted(covered - discovered)}"
    )
