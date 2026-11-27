"""``ha_mcp_tools/script_config_keys``: a script config's keys, read from Core."""

from __future__ import annotations

from typing import Any

from custom_components.ha_mcp_tools.script_config_keys import WS_SCRIPT_CONFIG_KEYS

from ._component_core import ws_result


async def test_the_keys_are_cores_script_fields_and_its_blueprint_key(
    component_ws: Any,
) -> None:
    keys = (await ws_result(component_ws, WS_SCRIPT_CONFIG_KEYS))["keys"]

    assert {"sequence", "trace", "fields", "use_blueprint"} <= set(keys)
    assert "triggers" not in keys
