"""
E2E: ha_config_set_helper backs up every helper type it edits (#2632).

A flow helper other than template is snapshotted from its options and
restored through its options flow; zone, a storage helper the backup family
used to skip, is restored through ``zone/update``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.topology import component_surface_available
from .test_capture_and_restore import (
    _HA_PROPAGATION_SETTLE_SECONDS,
    _wait_for_backup,
)


async def _restore(mcp, backup_name: str) -> dict:
    restored = await mcp.call_tool_success(
        "ha_manage_backup",
        {"scope": "edits", "action": "restore", "backup_name": backup_name},
    )
    assert restored["data"]["safety_backup"] is not None
    return restored


@pytest.mark.helper
@pytest.mark.cleanup
async def test_utility_meter_edit_is_backed_up_and_restored(mcp_client) -> None:
    async with MCPAssertions(mcp_client) as mcp:
        if not component_surface_available():
            # The options are read through the component; without it the
            # capture refuses rather than saving an entity-state stand-in.
            refused = await mcp.call_tool_failure(
                "ha_manage_backup",
                {
                    "scope": "edits",
                    "action": "create",
                    "domain": "helper_utility_meter",
                    "entity_id": uuid.uuid4().hex,
                },
            )
            assert refused["error"]["code"] == "RESOURCE_NOT_FOUND"
            return
        created = await mcp.call_tool_success(
            "ha_config_set_helper",
            {
                "helper_type": "utility_meter",
                "name": f"E2E Backup Meter {uuid.uuid4().hex[:6]}",
                "config": {
                    "source": "sensor.outlet_1_power",
                    "cycle": "daily",
                    "periodically_resetting": True,
                },
            },
        )
        entry_id = created["data"]["entry_id"]
        try:
            await mcp.call_tool_success(
                "ha_config_set_helper",
                {
                    "helper_type": "utility_meter",
                    "helper_id": entry_id,
                    "config": {"periodically_resetting": False},
                },
            )
            backup_name = await _wait_for_backup(
                mcp_client, domain="helper_utility_meter", entity_id=entry_id
            )
            await _restore(mcp, backup_name)
            described = await mcp.call_tool_success(
                "ha_config_list_helpers",
                {
                    "helper_type": "utility_meter",
                    "describe": True,
                    "helper_id": entry_id,
                },
            )
            fields = {f["name"]: f for f in described["fields"]}
            assert fields["periodically_resetting"]["current"] is True
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )


@pytest.mark.helper
@pytest.mark.cleanup
async def test_zone_edit_is_backed_up_and_restored(mcp_client) -> None:
    name = f"e2e_zone_{uuid.uuid4().hex[:8]}"
    async with MCPAssertions(mcp_client) as mcp:
        created = await mcp.call_tool_success(
            "ha_config_set_helper",
            {
                "helper_type": "zone",
                "name": name,
                "config": {"latitude": 52.1, "longitude": 4.3, "radius": 100},
            },
        )
        entity_id = created["entity_id"]
        try:
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)
            await mcp.call_tool_success(
                "ha_config_set_helper",
                {
                    "helper_type": "zone",
                    "helper_id": entity_id,
                    "config": {"radius": 250},
                },
            )
            backup_name = await _wait_for_backup(
                mcp_client, domain="helper_zone", entity_id=entity_id
            )
            await _restore(mcp, backup_name)
            state = await mcp.call_tool_success(
                "ha_get_state", {"entity_id": entity_id}
            )
            assert state["data"]["attributes"]["radius"] == 100
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {"target": entity_id, "helper_type": "zone", "confirm": True},
            )
