"""Enforce mode conceals hidden scripts, scenes and automations addressed by bare key.

The script, scene and automation tools accept the bare storage key
(``unlock_house``) or the config ``id`` (the registry ``unique_id``) and derive
the entity themselves, and Home Assistant exposes each script as a
``script.<storage key>`` service, so a hidden entity was reachable by an
argument that never spells out its entity_id.

The registry rows are Core's own, read over the server's client, and a
genuine not-found is Core's own 404 from its config API.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools
from ha_mcp.visibility.identifier_aliases import BARE_IDENTIFIER_ARGS
from ha_mcp.visibility.model import VisibilityConfig

from .._visibility_core import (
    enforce_hiding,
    error_body,
    error_code,
    register_entity,
    unreached_call_next,
)
from .._registered_tools import all_registered_tools
from ._chained_enforcement import (
    _ChainedEnforcement,
    _returns,
    make_context,
    text_result,
)

# (entity_id, registry unique_id, registry platform)
_Row = tuple[str, str, str]

# A hidden script renamed from ``come_home``: its storage key and service name
# stay ``come_home`` while its entity_id is ``script.front_door``.
_RENAMED_HIDDEN_SCRIPT: _Row = ("script.front_door", "come_home", "script")


@pytest.fixture(autouse=True)
async def registry_api(hass: HomeAssistant) -> None:
    """``config`` serves the registry list and the script/automation config API."""
    assert await async_setup_component(hass, "config", {})


def _register_rows(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, *rows: _Row
) -> None:
    for entity_id, unique_id, platform in rows:
        register_entity(hass, entity_registry, entity_id, unique_id, platform)


class TestBareIdentifierConcealment:
    @pytest.mark.parametrize(
        ("tool", "args", "row"),
        [
            pytest.param(
                "ha_config_set_script",
                {"script_id": "unlock_house", "run": "start"},
                ("script.unlock_house", "unlock_house", "script"),
                id="script-bare-object-id",
            ),
            pytest.param(
                "ha_config_get_script",
                {"script_id": "unlock_house"},
                ("script.front_door", "unlock_house", "script"),
                id="renamed-script-storage-key",
            ),
            pytest.param(
                "ha_call_service",
                {"domain": "script", "service": "unlock_house"},
                ("script.front_door", "unlock_house", "script"),
                id="script-service-named-by-storage-key",
            ),
            pytest.param(
                "ha_config_set_scene",
                {"scene_id": "1727266720448", "activate": True},
                ("scene.open_up", "1727266720448", "homeassistant"),
                id="scene-config-id",
            ),
            # The tools fall back to ``scene.<id>``, and Home Assistant
            # lowercases an entity_id, so the object ID matches in any case.
            pytest.param(
                "ha_config_set_scene",
                {"scene_id": "Open_Up", "activate": True},
                ("scene.open_up", "1727266720448", "homeassistant"),
                id="scene-object-id-in-other-case",
            ),
            pytest.param(
                "ha_config_set_automation",
                {"entity_id": "1700000000001", "run_actions": True},
                ("automation.buzz_door", "1700000000001", "automation"),
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
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
        tool: str,
        args: dict[str, Any],
        row: _Row,
    ) -> None:
        """The bare key never reaches the tool, and the answer is a plain not-found."""
        _register_rows(hass, entity_registry, row)
        enforce_hiding(set_config, row[0])
        mw = _ChainedEnforcement(lambda: core_client)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context(tool, args), unreached_call_next)

        assert error_code(exc) == "ENTITY_NOT_FOUND"

    @pytest.mark.parametrize(
        ("hidden", "rows", "tool", "args"),
        [
            pytest.param(
                "script.unlock_house",
                [
                    ("script.unlock_house", "unlock_house", "script"),
                    ("script.wake_up", "wake_up", "script"),
                ],
                "ha_config_set_script",
                {"script_id": "wake_up", "run": "start"},
                id="visible-script-bare-key",
            ),
            pytest.param(
                "script.unlock_house",
                [("script.unlock_house", "unlock_house", "script")],
                "ha_config_get_scene",
                {"scene_id": "unlock_house"},
                id="same-key-in-another-domain",
            ),
            pytest.param(
                "scene.open_up",
                [
                    ("scene.open_up", "Door", "homeassistant"),
                    ("scene.porch", "door", "homeassistant"),
                ],
                "ha_config_get_scene",
                {"scene_id": "door"},
                id="visible-scene-config-id-differing-only-in-case",
            ),
            pytest.param(
                "script.front_door",
                [
                    ("script.front_door", "unlock_house", "script"),
                    ("script.front_door_2", "front_door", "script"),
                ],
                "ha_call_service",
                {"domain": "script", "service": "front_door"},
                id="visible-script-service-named-like-hidden-object-id",
            ),
            # The automation tools read a bare entity_id only as a config id.
            pytest.param(
                "automation.door_auto",
                [
                    ("automation.door_auto", "1700000000001", "automation"),
                    ("automation.kitchen_motion", "door_auto", "automation"),
                ],
                "ha_config_get_automation",
                {"entity_id": "door_auto"},
                id="visible-automation-config-id-like-hidden-object-id",
            ),
        ],
    )
    async def test_visible_entity_sharing_a_hidden_alias_stays_reachable(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
        hidden: str,
        rows: list[_Row],
        tool: str,
        args: dict[str, Any],
    ) -> None:
        """A visible item whose key looks like a hidden alias still runs."""
        _register_rows(hass, entity_registry, *rows)
        enforce_hiding(set_config, hidden)
        mw = _ChainedEnforcement(lambda: core_client)

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
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
        tool: str,
        args: dict[str, Any],
    ) -> None:
        """A body naming the hidden script's service is refused before it is written."""
        _register_rows(hass, entity_registry, _RENAMED_HIDDEN_SCRIPT)
        enforce_hiding(set_config, _RENAMED_HIDDEN_SCRIPT[0])
        mw = _ChainedEnforcement(lambda: core_client)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context(tool, args), unreached_call_next)

        assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"


@pytest.mark.parametrize(
    ("hidden", "visible", "tool", "list_key", "call_tool"),
    [
        pytest.param(
            ("script.unlock_house", "unlock_house", "script"),
            ("script.wake_up", "wake_up", "script"),
            "ha_config_get_script",
            "available_script_ids",
            lambda client: ConfigScriptTools(client).ha_config_get_script(
                script_id="no_such"
            ),
            id="script",
        ),
        pytest.param(
            ("automation.buzz_door", "1700000000001", "automation"),
            ("automation.kitchen_motion", "1700000000002", "automation"),
            "ha_config_get_automation",
            "available_automation_ids",
            lambda client: AutomationConfigTools(client).ha_config_get_automation(
                entity_id="no_such"
            ),
            id="automation",
        ),
    ],
)
async def test_genuine_not_found_lists_only_visible_ids(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    set_config: Callable[..., VisibilityConfig],
    hidden: _Row,
    visible: _Row,
    tool: str,
    list_key: str,
    call_tool: Callable[[HomeAssistantClient], Awaitable[Any]],
) -> None:
    """Core's 404 for an absent key lists the visible ids, never the hidden one."""
    _register_rows(hass, entity_registry, hidden, visible)
    enforce_hiding(set_config, hidden[0])
    mw = _ChainedEnforcement(lambda: core_client)

    async def _run_tool(_context: Any) -> Any:
        return await call_tool(core_client)

    with pytest.raises(ToolError) as exc:
        await mw.on_call_tool(make_context(tool, {}), _run_tool)

    body = error_body(exc)
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    listed = body[list_key]
    assert any(visible[0].partition(".")[2] in item for item in listed), body
    hidden_object_id = hidden[0].partition(".")[2]
    assert all(hidden_object_id not in item for item in listed)


def _takes_bare_identifier(tool_name: str, param_name: str) -> bool:
    if param_name in ("script_id", "scene_id", "automation_id"):
        return True
    if param_name == "item_id" and tool_name == "ha_get_automation_traces":
        return True
    if param_name == "entity_id" and tool_name == "ha_get_automation_traces":
        return True
    # The automation config tools also take a bare config id as ``entity_id``.
    return (
        param_name == "entity_id"
        and tool_name.startswith("ha_config_")
        and (tool_name.endswith("_automation"))
    )


def test_every_bare_identifier_tool_is_concealed_by_enforce_mode() -> None:
    tools = all_registered_tools()
    assert tools, "no tools registered — guardrail cannot run"

    discovered = {
        (tool_name, param_name)
        for tool_name, tool in tools.items()
        for param_name in inspect.signature(tool.fn).parameters
        if _takes_bare_identifier(tool_name, param_name)
    }
    covered = {(tool, arg) for tool, (arg, _domain) in BARE_IDENTIFIER_ARGS.items()}
    covered |= {
        ("ha_get_automation_traces", "entity_id"),
        ("ha_get_automation_traces", "item_id"),
    }

    assert discovered == covered, (
        "BARE_IDENTIFIER_ARGS is out of sync with the registered tools.\n"
        f"  missing (hidden entity reachable by bare key): {sorted(discovered - covered)}\n"
        f"  stale (tool or parameter no longer exists): {sorted(covered - discovered)}"
    )
