"""
E2E tests for the registry state of a device's entities in ha_get_device.

Disabled and hidden entities have no state, so the device's entity rows are
the only place a caller sees them. The rows are the native entity-registry
entries; ``disabled_by`` / ``hidden_by`` are present only when set.
"""

from typing import Any

import pytest

from ...utilities.assertions import MCPAssertions


def _restorable(row: dict[str, Any]) -> bool:
    """An enabled, visible entity, so the test can put it back as it was."""
    return not row.get("disabled_by") and not row.get("hidden_by")


@pytest.mark.registry
async def test_device_entities_report_disabled_and_hidden_state(
    mcp_client: Any,
) -> None:
    """A disabled and a hidden entity keep their registry state in the rows."""
    async with MCPAssertions(mcp_client) as mcp:
        list_data = await mcp.call_tool_success("ha_get_device", {})

        target = None
        candidates: list[dict[str, Any]] = []
        for device in list_data.get("devices", []):
            get_data = await mcp.call_tool_success(
                "ha_get_device", {"device_id": device["device_id"]}
            )
            candidates = [e for e in get_data.get("entities", []) if _restorable(e)]
            if len(candidates) >= 2:
                target = get_data
                break
        if target is None:
            pytest.skip("No device with two enabled, visible entities")

        device_id = target["device"]["device_id"]
        disabled_id, hidden_id = (e["entity_id"] for e in candidates[-2:])

        try:
            await mcp.call_tool_success(
                "ha_set_entity", {"entity_id": disabled_id, "enabled": False}
            )
            await mcp.call_tool_success(
                "ha_set_entity", {"entity_id": hidden_id, "hidden": True}
            )

            get_data = await mcp.call_tool_success(
                "ha_get_device", {"device_id": device_id}
            )
            rows = {row["entity_id"]: row for row in get_data["entities"]}

            assert rows[disabled_id].get("disabled_by") == "user", rows[disabled_id]
            assert rows[hidden_id].get("hidden_by") == "user", rows[hidden_id]
            assert "disabled_by" not in rows[hidden_id], rows[hidden_id]
            for row in rows.values():
                assert row.get("platform"), f"Native registry field missing: {row}"
                assert row.get("device_id") == device_id, row
        finally:
            # Each restore runs even if the other one fails.
            try:
                await mcp_client.call_tool(
                    "ha_set_entity", {"entity_id": disabled_id, "enabled": True}
                )
            finally:
                await mcp_client.call_tool(
                    "ha_set_entity", {"entity_id": hidden_id, "hidden": False}
                )
