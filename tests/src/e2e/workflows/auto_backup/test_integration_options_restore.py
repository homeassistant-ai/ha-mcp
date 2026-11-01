"""E2E: an integration edit backup puts the entry's options back (#2927).

The demo entry's options flow (``options_1`` then ``options_2``) is safe to
drive and needs no credentials, so its ``int`` option carries the round trip.
"""

from __future__ import annotations

from typing import Any

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.wait_helpers import wait_for_tool_result
from .test_capture_and_restore import _enable_auto_backup, _wait_for_backup


async def _wait_for_int_option(mcp_client, entry_id: str, value: int) -> None:
    await wait_for_tool_result(
        mcp_client,
        tool_name="ha_get_integration",
        arguments={"entry_id": entry_id},
        predicate=lambda d: d.get("entry", {}).get("options", {}).get("int") == value,
        description=f"demo entry option int == {value}",
        timeout=20,
    )


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
# The snapshot reads options through the component's config-entry surface.
@pytest.mark.requires_component_surface
class TestIntegrationOptionsRestore:
    async def test_restore_brings_back_the_pre_edit_options(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        backup_name = None
        async with MCPAssertions(mcp_client) as mcp:
            listed = await mcp.call_tool_success(
                "ha_get_integration", {"domain": "demo"}
            )
            entry_id = next(
                e["entry_id"] for e in listed["entries"] if e.get("supports_options")
            )
            before: dict[str, Any] = (
                await mcp.call_tool_success(
                    "ha_get_integration", {"entry_id": entry_id}
                )
            )["entry"].get("options", {})
            try:
                await mcp.call_tool_success(
                    "ha_set_integration", {"entry_id": entry_id, "config": {"int": 11}}
                )
                await _wait_for_int_option(mcp_client, entry_id, 11)
                await mcp.call_tool_success(
                    "ha_set_integration", {"entry_id": entry_id, "config": {"int": 42}}
                )
                await _wait_for_int_option(mcp_client, entry_id, 42)

                # Newest first: the snapshot taken before the second edit.
                backup_name = await _wait_for_backup(
                    mcp_client, domain="integration", entity_id=entry_id
                )
                viewed = await mcp.call_tool_success(
                    "ha_manage_backup",
                    {"scope": "edits", "action": "view", "backup_name": backup_name},
                )
                snapshot = viewed["data"]["config"]
                assert snapshot["options"]["int"] == 11, snapshot

                await mcp.call_tool_success(
                    "ha_manage_backup",
                    {"scope": "edits", "action": "restore", "backup_name": backup_name},
                )
                # A restore that only reapplied the enabled state would still
                # report success.
                await _wait_for_int_option(mcp_client, entry_id, 11)
            finally:
                await safe_call_tool(
                    mcp_client,
                    "ha_set_integration",
                    {"entry_id": entry_id, "config": {"int": before.get("int", 10)}},
                )
                if backup_name:
                    await safe_call_tool(
                        mcp_client,
                        "ha_manage_backup",
                        {
                            "scope": "edits",
                            "action": "delete",
                            "backup_name": backup_name,
                        },
                    )
