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

from ...utilities.assertions import MCPAssertions, assert_mcp_success, safe_call_tool
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


async def _plane_titles(mcp, entry_id: str) -> dict[str, str]:
    listed = await mcp.call_tool_success(
        "ha_get_integration", {"entry_id": entry_id, "include_subentries": True}
    )
    return {s["subentry_id"]: s["title"] for s in listed["subentries"]}


async def _subentry_backup(mcp_client, entry_id: str, subentry_id: str) -> str | None:
    """The write's backup, or None where no component can read subentry data.

    Core lists subentries without their data, so without the component the
    write goes ahead and nothing partial is saved.
    """
    target = {
        "domain": "helper_config_subentry",
        "entity_id": f"{entry_id}/{subentry_id}",
    }
    if component_surface_available():
        return await _wait_for_backup(mcp_client, **target)
    listed = await mcp_client.call_tool(
        "ha_manage_backup", {"scope": "edits", "action": "list", **target}
    )
    assert not assert_mcp_success(listed, "list subentry backups").get("backups")
    return None


@pytest.fixture
async def solar_plane(mcp_client, ha_client):
    """A Forecast.Solar entry with one plane subentry (in-tree, no network)."""
    flow = await ha_client.start_config_flow("forecast_solar")
    done = await ha_client.submit_config_flow_step(
        flow["flow_id"],
        {
            "latitude": 52.0,
            "longitude": 5.0,
            "declination": 30,
            "azimuth": 180,
            "modules_power": 1000,
        },
    )
    entry_id = done["result"]["entry_id"]
    try:
        async with MCPAssertions(mcp_client) as mcp:
            (subentry_id,) = await _plane_titles(mcp, entry_id)
        yield entry_id, subentry_id
    finally:
        await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )


@pytest.mark.helper
@pytest.mark.cleanup
async def test_subentry_edit_is_backed_up_and_restored(mcp_client, solar_plane) -> None:
    entry_id, subentry_id = solar_plane
    async with MCPAssertions(mcp_client) as mcp:
        await mcp.call_tool_success(
            "ha_config_set_helper",
            {
                "helper_type": "config_subentry",
                "entry_id": entry_id,
                "subentry_type": "plane",
                "subentry_id": subentry_id,
                "config": {"modules_power": 1500},
            },
        )
        backup_name = await _subentry_backup(mcp_client, entry_id, subentry_id)
        if backup_name is None:
            return
        await _restore(mcp, backup_name)
        assert (await _plane_titles(mcp, entry_id))[subentry_id].endswith("1000W")


@pytest.mark.helper
@pytest.mark.cleanup
async def test_deleted_subentry_is_backed_up_and_created_again(
    mcp_client, solar_plane
) -> None:
    entry_id, subentry_id = solar_plane
    async with MCPAssertions(mcp_client) as mcp:
        await mcp.call_tool_success(
            "ha_remove_helpers_integrations",
            {
                "target": entry_id,
                "helper_type": "config_subentry",
                "subentry_id": subentry_id,
                "confirm": True,
            },
        )
        backup_name = await _subentry_backup(mcp_client, entry_id, subentry_id)
        if backup_name is None:
            return
        # Nothing exists to take a safety backup of.
        restored = await mcp.call_tool_success(
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        new_id = restored["data"]["result"]["subentry_id"]
        assert (await _plane_titles(mcp, entry_id)) == {new_id: "30° / 180° / 1000W"}
