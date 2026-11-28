"""``ha_mcp_tools/config_keys``: an automation's or script's config keys, read from Core."""

from __future__ import annotations

from typing import Any

from custom_components.ha_mcp_tools.config_keys import WS_CONFIG_KEYS

from ._component_core import ws_call, ws_result


async def test_the_script_keys_are_cores_script_fields_and_its_blueprint_key(
    component_ws: Any,
) -> None:
    keys = (await ws_result(component_ws, WS_CONFIG_KEYS, domain="script"))["keys"]

    assert {"sequence", "trace", "fields", "use_blueprint"} <= set(keys)
    assert "triggers" not in keys


async def test_the_automation_keys_are_cores_automation_fields_and_its_blueprint_key(
    component_ws: Any,
) -> None:
    keys = (await ws_result(component_ws, WS_CONFIG_KEYS, domain="automation"))["keys"]

    assert {
        "id",
        "triggers",
        "actions",
        "trace",
        "initial_state",
        "trigger_variables",
        "use_blueprint",
    } <= set(keys)
    # The singular forms Core renames to the plural keys.
    assert {"trigger", "condition", "action"} <= set(keys)
    assert "sequence" not in keys


async def test_another_domain_is_refused(component_ws: Any) -> None:
    reply = await ws_call(component_ws, WS_CONFIG_KEYS, domain="scene")

    assert reply["success"] is False
