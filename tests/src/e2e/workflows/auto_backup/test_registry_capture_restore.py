"""E2E: label, category, zone and area edits are backed up and restored.

Each of these stores applies an update before it replies, so every lane reads
the restored item back directly instead of polling.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from .test_capture_and_restore import (
    _HA_PROPAGATION_SETTLE_SECONDS,
    _delete_leftover_backup,
    _enable_auto_backup,
    _wait_for_backup,
)

# ---------------------------------------------------------------- label lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestLabelCaptureRestore:
    """Label restore exercises the ``config/label_registry/update`` WS
    command (Group 2). Field-mangling matters here — the handler strips
    ``label_id`` from the captured config before re-injecting it under
    the ``label_id`` key, so a round-trip that lost or duplicated the key
    would only surface end-to-end."""

    async def test_label_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_label",
            {
                "name": f"e2e_bk_label_{suffix}",
                "color": "blue",
                "icon": "mdi:test-tube",
                "description": "auto-backup e2e label",
            },
        )
        if create.get("success") is False:
            pytest.skip(f"label create unsupported: {create}")
        label_id = create.get("data", {}).get("label_id") or create.get("label_id")
        assert label_id, f"label_id missing from create response: {create}"
        backup_name = None
        try:
            # Settle so the decorator's pre-edit fetch finds the label.
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

            # Edit — change name + color so capture fires.
            edit = await safe_call_tool(
                mcp_client,
                "ha_config_set_label",
                {
                    "label_id": label_id,
                    "name": f"e2e_bk_label_{suffix}_edited",
                    "color": "red",
                },
            )
            assert edit.get("success") is not False

            backup_name = await _wait_for_backup(
                mcp_client, domain="label", entity_id=label_id
            )

            restore = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": backup_name},
            )
            assert restore.get("success") is True
            assert restore.get("data", {}).get("safety_backup") is not None
            async with MCPAssertions(mcp_client) as mcp:
                restored = await mcp.call_tool_success(
                    "ha_config_get_label", {"label_id": label_id}
                )
            assert restored["label"]["name"] == f"e2e_bk_label_{suffix}"
            assert restored["label"]["color"] == "blue"

            delete = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "delete", "backup_name": backup_name},
            )
            assert delete.get("success") is True
            backup_name = None
        finally:
            # Cleanup
            await safe_call_tool(
                mcp_client, "ha_config_remove_label", {"label_id": label_id}
            )
            await _delete_leftover_backup(mcp_client, backup_name)


# ---------------------------------------------------------------- category lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestCategoryCaptureRestore:
    """Category restore is Group 2 (``config/category_registry/update``).
    Entity ID is the composite ``<scope>:<category_id>`` — the handler
    splits it back into scope + id on restore, so a captured snapshot
    that lost the scope prefix would silently restore to the wrong
    registry. Worth e2e coverage."""

    async def test_category_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        scope = "automation"
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_category",
            {
                "name": f"e2e_bk_cat_{suffix}",
                "scope": scope,
                "icon": "mdi:tag",
            },
        )
        if create.get("success") is False:
            pytest.skip(f"category create unsupported: {create}")
        cat_id = create.get("data", {}).get("category_id") or create.get("category_id")
        assert cat_id, f"category_id missing: {create}"
        composite = f"{scope}:{cat_id}"
        backup_name = None
        try:
            # Settle so the decorator's pre-edit fetch finds the category.
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

            edit = await safe_call_tool(
                mcp_client,
                "ha_config_set_category",
                {
                    "category_id": cat_id,
                    "scope": scope,
                    "name": f"e2e_bk_cat_{suffix}_edited",
                    "icon": "mdi:tag-outline",
                },
            )
            assert edit.get("success") is not False

            backup_name = await _wait_for_backup(
                mcp_client, domain="category", entity_id=composite
            )

            restore = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": backup_name},
            )
            assert restore.get("success") is True
            assert restore.get("data", {}).get("safety_backup") is not None
            async with MCPAssertions(mcp_client) as mcp:
                restored = await mcp.call_tool_success(
                    "ha_config_get_category", {"scope": scope, "category_id": cat_id}
                )
            assert restored["category"]["name"] == f"e2e_bk_cat_{suffix}"
            assert restored["category"]["icon"] == "mdi:tag"

            delete = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "delete", "backup_name": backup_name},
            )
            assert delete.get("success") is True
            backup_name = None
        finally:
            # Cleanup
            await safe_call_tool(
                mcp_client,
                "ha_config_remove_category",
                {"scope": scope, "category_id": cat_id},
            )
            await _delete_leftover_backup(mcp_client, backup_name)


# ---------------------------------------------------------------- zone lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestZoneCaptureRestore:
    """Zone restore is Group 2 (``config/zone/update``). The handler
    strips ``id`` and re-injects under ``zone_id`` — different key name
    from the rest of the WS-update commands, so a copy-paste bug from
    the label handler would only surface here."""

    async def test_zone_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        create = await safe_call_tool(
            mcp_client,
            "ha_set_zone",
            {
                "name": f"e2e_bk_zone_{suffix}",
                "latitude": 40.7128,
                "longitude": -74.0060,
                "radius": 150,
                "icon": "mdi:briefcase",
            },
        )
        if create.get("success") is False:
            pytest.skip(f"zone create unsupported: {create}")
        zone_id = create.get("data", {}).get("zone_id") or create.get("zone_id")
        assert zone_id, f"zone_id missing: {create}"
        backup_name = None
        try:
            # Settle so the decorator's pre-edit fetch finds the zone.
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

            edit = await safe_call_tool(
                mcp_client,
                "ha_set_zone",
                {
                    "zone_id": zone_id,
                    "name": f"e2e_bk_zone_{suffix}_edited",
                    "radius": 250,
                },
            )
            assert edit.get("success") is not False

            backup_name = await _wait_for_backup(
                mcp_client, domain="zone", entity_id=zone_id
            )

            restore = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": backup_name},
            )
            assert restore.get("success") is True
            assert restore.get("data", {}).get("safety_backup") is not None
            async with MCPAssertions(mcp_client) as mcp:
                restored = await mcp.call_tool_success(
                    "ha_get_zone", {"zone_id": zone_id}
                )
            assert restored["zone"]["name"] == f"e2e_bk_zone_{suffix}"
            assert restored["zone"]["radius"] == 150

            delete = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "delete", "backup_name": backup_name},
            )
            assert delete.get("success") is True
            backup_name = None
        finally:
            # Cleanup
            await safe_call_tool(mcp_client, "ha_remove_zone", {"zone_id": zone_id})
            await _delete_leftover_backup(mcp_client, backup_name)


# ---------------------------------------------------------------- area lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestAreaCaptureRestore:
    """Area restore is Group 2 (``config/area_registry/update``). The
    handler keys on ``area:<area_id>`` and dispatches to area or floor
    based on the prefix — floor uses the identical mechanism with a
    different registry, so area-only coverage proves both
    code paths."""

    async def test_area_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        create = await safe_call_tool(
            mcp_client,
            "ha_set_area_or_floor",
            {
                "kind": "area",
                "name": f"e2e_bk_area_{suffix}",
                "icon": "mdi:sofa",
            },
        )
        if create.get("success") is False:
            pytest.skip(f"area create unsupported: {create}")
        area_id = create.get("data", {}).get("area_id") or create.get("area_id")
        assert area_id, f"area_id missing: {create}"
        composite = f"area:{area_id}"
        backup_name = None
        try:
            # Settle so the decorator's pre-edit fetch finds the area.
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

            edit = await safe_call_tool(
                mcp_client,
                "ha_set_area_or_floor",
                {
                    "kind": "area",
                    "id": area_id,
                    "name": f"e2e_bk_area_{suffix}_edited",
                    "icon": "mdi:sofa-outline",
                },
            )
            assert edit.get("success") is not False

            backup_name = await _wait_for_backup(
                mcp_client, domain="area_or_floor", entity_id=composite
            )

            restore = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": backup_name},
            )
            assert restore.get("success") is True
            assert restore.get("data", {}).get("safety_backup") is not None
            async with MCPAssertions(mcp_client) as mcp:
                topology = await mcp.call_tool_success("ha_list_floors_areas", {})
            restored = next(
                (a for a in topology["unassigned_areas"] if a["area_id"] == area_id),
                None,
            )
            assert restored is not None, f"area {area_id} missing: {topology}"
            assert restored["name"] == f"e2e_bk_area_{suffix}"
            assert restored["icon"] == "mdi:sofa"

            delete = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "delete", "backup_name": backup_name},
            )
            assert delete.get("success") is True
            backup_name = None
        finally:
            # Cleanup
            await safe_call_tool(
                mcp_client,
                "ha_remove_area_or_floor",
                {"kind": "area", "id": area_id},
            )
            await _delete_leftover_backup(mcp_client, backup_name)
