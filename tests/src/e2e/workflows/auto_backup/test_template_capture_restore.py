"""Template-helper backup coverage across component topologies."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.cleanup import CleanupSteps
from ...utilities.topology import component_surface_available
from ...utilities.wait_helpers import wait_for_entity_state, wait_for_tool_result
from .test_capture_and_restore import _wait_for_backup


async def _assert_template_alias_capture(
    mcp_client, entity_id: str, entry_id: str
) -> None:
    """An entity-addressed capture belongs to the stable entry's history."""
    mcp = MCPAssertions(mcp_client)
    created = await mcp.call_tool_success(
        "ha_manage_backup",
        {
            "scope": "edits",
            "action": "create",
            "domain": "helper_template",
            "entity_id": entity_id,
        },
    )
    name = created["data"]["backup_name"]
    try:
        viewed = await mcp.call_tool_success(
            "ha_manage_backup",
            {"scope": "edits", "action": "view", "backup_name": name},
        )
        assert viewed["data"]["entity_id"] == entry_id
        stable_filter = {
            "scope": "edits",
            "domain": "helper_template",
            "entity_id": entry_id,
        }
        listed = await mcp.call_tool_success(
            "ha_manage_backup", {**stable_filter, "action": "list"}
        )
        assert name in {row["name"] for row in listed["data"]["backups"]}
        deleted = await mcp.call_tool_success(
            "ha_manage_backup", {**stable_filter, "action": "delete"}
        )
        assert deleted["data"]["deleted"] == [name]
    finally:
        await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": name},
        )


async def _assert_template_degraded_scrub_refused(
    mcp_client,
    config_path: str,
    entry_id: str,
    backup_name: str,
    expected_options: dict[str, Any],
) -> None:
    """Malformed disposable secrets must block capture and restore without writes."""
    mcp = MCPAssertions(mcp_client)
    snapshot_filter = {
        "scope": "edits",
        "domain": "helper_template",
        "entity_id": entry_id,
    }
    before = await mcp.call_tool_success(
        "ha_manage_backup", {**snapshot_filter, "action": "list"}
    )
    names = {row["name"] for row in before["data"]["backups"]}
    secrets_path = Path(config_path) / "secrets.yaml"
    original = secrets_path.read_bytes() if secrets_path.exists() else None
    try:
        secrets_path.write_text("review_fixture: [\n", encoding="utf-8")
        await mcp.call_tool_failure(
            "ha_manage_backup", {**snapshot_filter, "action": "create"}
        )
        await mcp.call_tool_failure(
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        after = await mcp.call_tool_success(
            "ha_manage_backup", {**snapshot_filter, "action": "list"}
        )
        assert {row["name"] for row in after["data"]["backups"]} == names
    finally:
        if original is None:
            secrets_path.unlink(missing_ok=True)
        else:
            secrets_path.write_bytes(original)
    helpers = await mcp.call_tool_success(
        "ha_config_list_helpers", {"helper_type": "template"}
    )
    assert (
        next(
            row["options"] for row in helpers["helpers"] if row["entry_id"] == entry_id
        )
        == expected_options
    )


@pytest.mark.helper
@pytest.mark.cleanup
class TestTemplateHelperCaptureRestore:
    @pytest.mark.component_topology
    @pytest.mark.parametrize("template_type", ["sensor", "binary_sensor"])
    async def test_template_options_full_loop(  # noqa: PLR0915
        self, mcp_client, ha_client, ha_container_with_fresh_config, template_type: str
    ) -> None:
        """Restore through either component entry; refuse absent-component reads.

        Auto-backup defaults are enabled with no throttle on every backend.
        Read them through MCP instead of changing pytest's environment, which
        cannot reconfigure an embedded, app, or stdio server.
        """
        async with MCPAssertions(mcp_client) as mcp:
            if not component_surface_available():
                unavailable = await mcp.call_tool_failure(
                    "ha_config_list_helpers", {"helper_type": "template"}
                )
                assert unavailable["error"]["code"] == "COMPONENT_NOT_INSTALLED"
                # A made-up id's capture is refused either way; nothing is saved.
                missing_id = uuid.uuid4().hex
                snapshot = await mcp.call_tool_failure(
                    "ha_manage_backup",
                    {
                        "scope": "edits",
                        "action": "create",
                        "domain": "helper_template",
                        "entity_id": missing_id,
                    },
                )
                refused = {"BACKUP_CAPTURE_FAILED", "RESOURCE_NOT_FOUND"}
                assert snapshot["error"]["code"] in refused
                listing = await mcp.call_tool_success(
                    "ha_manage_backup",
                    {
                        "scope": "edits",
                        "action": "list",
                        "domain": "helper_template",
                        "entity_id": missing_id,
                    },
                )
                assert listing["data"]["backups"] == []
                return
            settings = await mcp.call_tool_success(
                "ha_manage_backup", {"scope": "edits", "action": "list"}
            )
            assert settings["data"]["enabled"] is True, settings
            assert settings["data"]["throttle_minutes"] == 0, settings

        entry_id = None
        backup_names: set[str] = set()
        mcp = MCPAssertions(mcp_client)
        try:
            create = await mcp.call_tool_success(
                "ha_config_set_helper",
                {
                    "helper_type": "template",
                    "name": f"Backup Template {uuid.uuid4().hex[:8]}",
                    "menu_choice": template_type,
                    "config": {
                        "state": "{{ 12 }}"
                        if template_type == "sensor"
                        else "{{ true }}",
                    },
                },
            )
            assert create.get("success") is True, create
            entry_id = create["entry_id"]
            entity_ids = [
                entity_id
                for entity_id in create["entity_ids"]
                if entity_id.startswith(f"{template_type}.")
            ]
            assert len(entity_ids) == 1, create
            entity_id = entity_ids[0]
            before = await wait_for_tool_result(
                mcp_client,
                tool_name="ha_config_list_helpers",
                arguments={"helper_type": "template"},
                predicate=lambda d: any(
                    row.get("entry_id") == entry_id for row in d.get("helpers", [])
                ),
                description="template helper options are readable before edit",
            )
            original = next(
                row["options"]
                for row in before["helpers"]
                if row.get("entry_id") == entry_id
            )
            await _assert_template_alias_capture(mcp_client, entity_id, entry_id)
            changed = {
                "state": "{{ 99 }}" if template_type == "sensor" else "{{ false }}",
                "additional_options": {"availability": "{{ true }}"},
            }
            if template_type == "sensor":
                changed["unit_of_measurement"] = "W"
            edit = await mcp.call_tool_success(
                "ha_config_set_helper",
                {"helper_type": "template", "helper_id": entry_id, "config": changed},
            )
            assert edit.get("success") is True, edit
            persisted = await wait_for_tool_result(
                mcp_client,
                tool_name="ha_config_list_helpers",
                arguments={"helper_type": "template"},
                predicate=lambda data: any(
                    row.get("entry_id") == entry_id
                    and row.get("options", {}).get("state") == changed["state"]
                    for row in data.get("helpers", [])
                ),
                description="template edit changed persisted state before restore",
            )
            edited_options = next(
                row["options"]
                for row in persisted["helpers"]
                if row.get("entry_id") == entry_id
            )
            assert edited_options["additional_options"]["availability"] == "{{ true }}"
            if template_type == "sensor":
                assert edited_options["unit_of_measurement"] == "W"
            assert await wait_for_entity_state(
                mcp_client,
                entity_id,
                "99" if template_type == "sensor" else "off",
                timeout=20,
            ), f"Template helper {entity_id} did not activate the edited state"
            name = await _wait_for_backup(
                mcp_client, domain="helper_template", entity_id=entry_id
            )
            backup_names.add(name)
            viewed = await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "view", "backup_name": name},
            )
            assert viewed.get("success") is True, viewed
            snapshot = viewed["data"]["config"]
            assert snapshot["entry_id"] == entry_id
            assert snapshot["options"] == original
            assert len(snapshot["entities"]) == 1
            assert snapshot["entities"][0]["entity_id"] == entity_id
            assert snapshot["entities"][0]["unique_id"] == entry_id
            # Only testcontainers expose a disposable mounted config directory;
            # HAOS still exercises the API-only capture and restore assertions.
            if config_path := ha_container_with_fresh_config.get("config_path"):
                await _assert_template_degraded_scrub_refused(
                    mcp_client, config_path, entry_id, name, edited_options
                )
            restored = await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": name},
            )
            assert restored.get("success") is True, restored
            assert restored["data"]["safety_backup"] is not None
            backup_names.add(restored["data"]["safety_backup"])
            after = await mcp.call_tool_success(
                "ha_config_list_helpers", {"helper_type": "template"}
            )
            assert (
                next(
                    row["options"]
                    for row in after["helpers"]
                    if row.get("entry_id") == entry_id
                )
                == original
            )
            assert await wait_for_entity_state(
                mcp_client,
                entity_id,
                "12" if template_type == "sensor" else "on",
                timeout=20,
            ), f"Template helper {entity_id} did not activate the restored state"
            await mcp.call_tool_success(
                "ha_remove_helpers_integrations",
                {"target": entity_id, "helper_type": "template", "confirm": True},
            )
            deleted = await mcp.call_tool_success(
                "ha_manage_backup",
                {
                    "scope": "edits",
                    "action": "delete",
                    "domain": "helper_template",
                    "entity_id": entry_id,
                },
            )
            assert backup_names <= set(deleted["data"]["deleted"])
            backup_names.clear()
            entry_id = None
        finally:
            with CleanupSteps() as cleanup:
                with cleanup.step():
                    if entry_id is not None:
                        await ha_client.delete_config_entry(entry_id)
                for backup_name in backup_names:
                    with cleanup.step():
                        await safe_call_tool(
                            mcp_client,
                            "ha_manage_backup",
                            {
                                "scope": "edits",
                                "action": "delete",
                                "backup_name": backup_name,
                            },
                        )
