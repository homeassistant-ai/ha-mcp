"""E2E: automation, script and scene edits are backed up and restored.

Each lane edits a config written through Home Assistant's REST config API,
restores the automatic backup, and reads the config back through its get tool.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from .test_capture_and_restore import (
    _HA_PROPAGATION_SETTLE_SECONDS,
    _backups_for,
    _enable_auto_backup,
    _update_automation,
    _wait_for_backup,
)

# ---------------------------------------------------------------- automation lane


@pytest.mark.automation
@pytest.mark.cleanup
@pytest.mark.external_only
class TestAutomationCaptureRestore:
    async def test_full_loop(self, mcp_client, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_auto_backup(monkeypatch)

        suffix = uuid.uuid4().hex[:8]
        identifier = f"e2e_backup_{suffix}"
        original = {
            "alias": f"E2E Backup Original {suffix}",
            "trigger": [{"platform": "time", "at": "12:00:00"}],
            "action": [{"service": "homeassistant.no_op"}],
        }
        # Create — pass identifier so the tool doesn't reject the call as
        # an ambiguous create-with-explicit-id.
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": original, "identifier": identifier},
        )
        assert create.get("success") is not False

        # Edit it once — decorator captures pre-edit state.
        edited = {**original, "alias": f"E2E Backup Edited {suffix}"}
        await _update_automation(mcp_client, identifier, edited)

        # List backups; ours should be there.
        listing = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "edits",
                "action": "list",
                "domain": "automation",
                "entity_id": identifier,
            },
        )
        assert listing.get("success") is True
        entries = listing.get("data", {}).get("backups", [])
        mine = _backups_for(entries, domain="automation", entity_id=identifier)
        assert len(mine) >= 1

        backup_name = mine[0]["name"]

        # View
        view = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "view", "backup_name": backup_name},
        )
        assert view.get("success") is True

        # Restore
        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        # A safety backup is taken before re-applying.
        assert restore.get("data", {}).get("safety_backup") is not None
        # The restore puts the pre-edit alias back.
        async with MCPAssertions(mcp_client) as mcp:
            restored = await mcp.call_tool_success(
                "ha_config_get_automation", {"identifier": identifier}
            )
        assert restored["config"]["alias"] == original["alias"]

        # Delete
        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup: remove the automation we created.
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_automation",
            {"identifier": identifier},
        )


# ---------------------------------------------------------------- script lane


@pytest.mark.script
@pytest.mark.cleanup
@pytest.mark.external_only
class TestScriptCaptureRestore:
    async def test_script_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Script restore is Group 1 (REST upsert via
        ``client.upsert_script_config``). Mechanically equivalent to the
        automation full-loop but proves the script-specific client method
        wires correctly end-to-end."""
        _enable_auto_backup(monkeypatch)
        script_id = f"e2e_bk_{uuid.uuid4().hex[:8]}"
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_script",
            {
                "script_id": script_id,
                "config": {
                    "alias": f"E2E Backup Script {script_id}",
                    "sequence": [{"service": "homeassistant.no_op"}],
                },
            },
        )
        if create.get("success") is False:
            pytest.skip(f"script create unsupported: {create}")
        # Settle so the decorator's pre-edit fetch finds the script.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)
        # Edit triggers capture.
        await MCPAssertions(mcp_client).call_tool_success(
            "ha_config_set_script",
            {
                "script_id": script_id,
                "config": {
                    "alias": f"E2E Backup Script {script_id} edited",
                    "sequence": [{"service": "homeassistant.no_op"}],
                },
            },
        )
        backup_name = await _wait_for_backup(
            mcp_client, domain="script", entity_id=script_id
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
                "ha_config_get_script", {"script_id": script_id}
            )
        assert restored["config"]["alias"] == f"E2E Backup Script {script_id}"

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_script",
            {"script_id": script_id},
        )


# ---------------------------------------------------------------- scene lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestSceneCaptureRestore:
    async def test_scene_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scene restore is Group 1 (REST upsert via
        ``client.upsert_scene_config``)."""
        _enable_auto_backup(monkeypatch)
        scene_id = f"e2e_bk_{uuid.uuid4().hex[:8]}"
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": {
                    "name": f"E2E Backup Scene {scene_id}",
                    "entities": {},
                },
            },
        )
        if create.get("success") is False:
            pytest.skip(f"scene create unsupported: {create}")
        # Settle so the decorator's pre-edit fetch finds the scene.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)
        await MCPAssertions(mcp_client).call_tool_success(
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": {
                    "name": f"E2E Backup Scene {scene_id} edited",
                    "entities": {},
                },
            },
        )
        backup_name = await _wait_for_backup(
            mcp_client, domain="scene", entity_id=scene_id
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
                "ha_config_get_scene", {"scene_id": scene_id}
            )
        assert restored["config"]["name"] == f"E2E Backup Scene {scene_id}"

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_scene",
            {"scene_id": scene_id},
        )
