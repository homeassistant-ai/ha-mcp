"""Restoring a deleted dashboard resource that Core stored without a type."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

URL = "/local/imported.js"


async def test_restore_of_a_deleted_untyped_resource_is_refused_with_a_fix(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
    tmp_path: Path,
) -> None:
    """Core's create needs a type the snapshot lacks: the user gets a refusal
    saying so, not Core's schema error about an internal field."""
    # Core imports an old ``resources:`` block whose entries need no type.
    hass_storage["lovelace_resources"] = {
        "version": 1,
        "key": "lovelace_resources",
        "data": {"items": [{"id": "imported", "url": URL}]},
    }
    assert await async_setup_component(hass, "lovelace", {})
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path / "backups"),
    )
    manager = bm.get_backup_manager(core_client, settings)
    path = await manager.maybe_snapshot("dashboard_resource", "imported")
    assert path is not None
    assert "type" not in manager.read_snapshot(path.name)["config"]
    deleted = await core_client.send_websocket_message(
        {"type": "lovelace/resources/delete", "resource_id": "imported"}
    )
    assert deleted["success"] is True

    # Each restore command opens its own socket; the spy sees all of them.
    with (
        patch.object(
            HomeAssistantWebSocketClient,
            "send_command",
            autospec=True,
            wraps=HomeAssistantWebSocketClient.send_command,
        ) as spy,
        pytest.raises(bm.BackupRestoreError) as excinfo,
    ):
        await manager.restore_snapshot(path.name)

    message = str(excinfo.value)
    assert "without a type" in message
    assert "Home Assistant needs one to create it" in message
    assert excinfo.value.outcome["apply_status"] == "not_applied"
    sent = [call.args[1] for call in spy.call_args_list]
    assert "lovelace/resources/update" in sent, "the restore must have run"
    assert "lovelace/resources/create" not in sent
    listed = await core_client.send_websocket_message({"type": "lovelace/resources"})
    assert listed["result"] == []
