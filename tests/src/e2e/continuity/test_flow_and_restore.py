"""Helper-edit and script-restore contracts exercised through public MCP tools."""

from copy import deepcopy
from typing import Any

import pytest

from ._support import public_scenario

pytestmark = pytest.mark.asyncio


async def test_template_options_edit_changes_output_and_preserves_unit_and_name(
    mcp_client: Any,
) -> None:
    """ha_config_set_helper must retain omitted options in a partial template edit.

    Cover the helper entry point, including installs without the custom component.
    """
    async with public_scenario(mcp_client) as case:
        name = case.unique_name()
        created = await case.mcp.call_tool_success(
            "ha_config_set_helper",
            {
                "helper_type": "template",
                "name": name,
                "config": {
                    "next_step_id": "sensor",
                    "state": "{{ 2 + 3 }}",
                    "unit_of_measurement": "W",
                },
            },
        )
        entry_id = created["entry_id"]
        case.remove_after(
            "ha_remove_helpers_integrations", target=entry_id, confirm=True
        )
        entity_ids = [eid for eid in created["entity_ids"] if eid.startswith("sensor.")]
        assert len(entity_ids) == 1, created
        entity_id = entity_ids[0]
        await case.state(entity_id, "5")
        initial = await case.mcp.call_tool_success(
            "ha_get_state", {"entity_id": entity_id}
        )
        assert initial["data"]["attributes"]["unit_of_measurement"] == "W"
        assert initial["data"]["attributes"]["friendly_name"] == name
        original = await case.mcp.call_tool_success(
            "ha_get_integration", {"entry_id": entry_id, "include_options": True}
        )
        assert original["entry"]["options"]["state"] == "{{ 2 + 3 }}"
        assert original["entry"]["options"]["unit_of_measurement"] == "W"

        await case.mcp.call_tool_success(
            "ha_config_set_helper",
            {
                "helper_type": "template",
                "helper_id": entry_id,
                "config": {"state": "{{ 3 + 4 }}"},
            },
        )
        await case.state(entity_id, "7")
        changed = await case.mcp.call_tool_success(
            "ha_get_state", {"entity_id": entity_id}
        )
        assert changed["data"]["attributes"]["unit_of_measurement"] == "W"
        assert changed["data"]["attributes"]["friendly_name"] == name
        persisted = await case.mcp.call_tool_success(
            "ha_get_integration", {"entry_id": entry_id, "include_options": True}
        )
        assert persisted["entry"]["options"]["state"] == "{{ 3 + 4 }}"
        assert persisted["entry"]["options"]["unit_of_measurement"] == "W"


async def test_script_snapshot_restores_saved_behavior_after_explicit_reload(
    mcp_client: Any,
) -> None:
    """A saved script must be restored exactly and executable after a Core reload."""
    async with public_scenario(mcp_client) as case:
        target = await case.helper("input_text", "idle")
        script_id = await case.script(
            {
                "alias": case.unique_name(),
                "sequence": [
                    {
                        "action": "input_text.set_value",
                        "target": {"entity_id": target},
                        "data": {"value": "saved behavior"},
                    }
                ],
            }
        )
        original = await case.mcp.call_tool_success(
            "ha_config_get_script", {"script_id": script_id}
        )
        snapshot = await case.mcp.call_tool_success(
            "ha_manage_backup",
            {
                "scope": "edits",
                "action": "create",
                "domain": "script",
                "entity_id": script_id,
            },
        )
        backup_name = snapshot["data"]["backup_name"]
        assert backup_name
        case.remove_after(
            "ha_manage_backup", scope="edits", action="delete", backup_name=backup_name
        )
        edited = deepcopy(original["config"])
        edited["sequence"][0]["data"]["value"] = "later behavior"
        await case.mcp.call_tool_success(
            "ha_config_set_script",
            {
                "script_id": script_id,
                "config": edited,
                "config_hash": original["config_hash"],
            },
        )
        before_restore = await case.mcp.call_tool_success(
            "ha_config_get_script", {"script_id": script_id}
        )
        assert (
            before_restore["config"]["sequence"][0]["data"]["value"] == "later behavior"
        )
        assert before_restore["config_hash"] != original["config_hash"]

        await case.mcp.call_tool_success(
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        restored = await case.mcp.call_tool_success(
            "ha_config_get_script", {"script_id": script_id}
        )
        assert restored["config"] == original["config"]
        assert restored["config_hash"] == original["config_hash"]
        await case.mcp.call_tool_success(
            "ha_call_service", {"domain": "script", "service": "reload"}
        )
        idle = await case.mcp.call_tool_success("ha_get_state", {"entity_id": target})
        assert idle["data"]["state"] == "idle"
        await case.mcp.call_tool_success(
            "ha_call_service", {"domain": "script", "service": script_id}
        )
        await case.state(target, "saved behavior")
