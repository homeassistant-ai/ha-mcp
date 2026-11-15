"""A dashboard snapshot can bring back a dashboard that was deleted."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

import ha_mcp.backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantError

from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_dashboards_core import (
    BODY,
    add_dashboard,
    dashboard_paths,
    dashboard_row,
    rewrite_snapshot,
    stored_config,
    ws_ok,
)
from ._backup_dashboards_core import (
    dashboard_manager as dashboard_manager,
)


def _handler(manager: bm.BackupManager) -> bm.DomainHandler:
    handler = manager.handler_for("dashboard")
    assert handler is not None
    return handler


async def _delete(client: HomeAssistantClient, url_path: str) -> None:
    row = await dashboard_row(client, url_path)
    await ws_ok(
        client, {"type": "lovelace/dashboards/delete", "dashboard_id": row["id"]}
    )


async def test_capture_keeps_the_dashboard_settings_needed_to_recreate_it(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    await add_dashboard(core_client, icon="mdi:stove")

    held = await _handler(dashboard_manager).fetch(core_client, "kitchen_dash")

    assert held == {
        "dashboard": await dashboard_row(core_client, "kitchen-dash"),
        "config": BODY,
    }
    assert held["dashboard"]["icon"] == "mdi:stove"


async def test_capture_of_a_dashboard_without_a_settings_row_keeps_the_body(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    """A fresh install's default dashboard has no row until Core migrates it."""
    await ws_ok(core_client, {"type": "lovelace/config/save", "config": BODY})

    held = await _handler(dashboard_manager).fetch(core_client, "lovelace")

    assert held == {"dashboard": None, "config": BODY}


async def test_capture_fails_when_the_dashboard_list_cannot_be_read(
    core_client: HomeAssistantClient, snapshot_manager: bm.BackupManager
) -> None:
    """Without Core's dashboards the list is refused; that is not a dashboard
    without settings."""
    with pytest.raises(HomeAssistantError, match="could not be captured"):
        await _handler(snapshot_manager).fetch(core_client, "kitchen-dash")


async def test_restore_of_a_deleted_dashboard_with_no_stored_config_returns_it_empty(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    """Deleting a dashboard that has no saved config must still be undoable."""
    await add_dashboard(core_client, body=None)
    handler = _handler(dashboard_manager)
    held = await handler.fetch(core_client, "kitchen-dash")
    await _delete(core_client, "kitchen-dash")

    await handler.restore(core_client, "kitchen-dash", held)

    assert await dashboard_paths(core_client) == ["kitchen-dash"]
    assert await stored_config(core_client, "kitchen-dash") is None


async def test_restore_of_a_dashboard_saved_since_returns_it_to_no_stored_config(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    """A config saved after the snapshot is removed again, not left behind."""
    await add_dashboard(core_client, body=None)
    handler = _handler(dashboard_manager)
    held = await handler.fetch(core_client, "kitchen-dash")
    await ws_ok(
        core_client,
        {"type": "lovelace/config/save", "url_path": "kitchen-dash", "config": BODY},
    )

    await handler.restore(core_client, "kitchen-dash", held)

    assert await stored_config(core_client, "kitchen-dash") is None


async def test_restore_of_deleted_lovelace_falls_back_to_the_default_store(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    """Core re-registers its built-in /lovelace panel, so the create is refused."""
    handler = _handler(dashboard_manager)
    row = {
        "id": "lovelace",
        "url_path": "lovelace",
        "title": "Overview",
        "icon": "mdi:view-dashboard",
        "show_in_sidebar": True,
        "require_admin": False,
        "mode": "storage",
    }

    result = await handler.restore(
        core_client, "lovelace", {"dashboard": row, "config": BODY}
    )

    assert "restart" in result["note"]
    assert await stored_config(core_client, None) == BODY


@pytest.mark.parametrize(
    ("url_path", "icon"),
    [("kitchen-dash", None), ("map", "mdi:map")],
    ids=["hyphenated", "single-word"],
)
async def test_restore_recreates_a_deleted_dashboard_then_saves_its_config(
    core_client: HomeAssistantClient,
    dashboard_manager: bm.BackupManager,
    url_path: str,
    icon: str | None,
) -> None:
    await add_dashboard(core_client, url_path, icon=icon)
    handler = _handler(dashboard_manager)
    held = await handler.fetch(core_client, url_path)
    row = await dashboard_row(core_client, url_path)
    await _delete(core_client, url_path)

    await handler.restore(core_client, url_path, held)

    restored = await dashboard_row(core_client, url_path)
    assert {k: v for k, v in restored.items() if k != "id"} == {
        k: v for k, v in row.items() if k != "id"
    }
    assert restored.get("icon") == icon
    assert await stored_config(core_client, url_path) == BODY


@pytest.mark.parametrize("icon", ["mdi:stove", None], ids=["icon", "icon-cleared"])
async def test_restore_to_an_existing_dashboard_saves_its_config_and_settings(
    core_client: HomeAssistantClient,
    dashboard_manager: bm.BackupManager,
    icon: str | None,
) -> None:
    await add_dashboard(core_client, icon=icon)
    handler = _handler(dashboard_manager)
    held = await handler.fetch(core_client, "kitchen-dash")
    live = await dashboard_row(core_client, "kitchen-dash")
    await ws_ok(
        core_client,
        {
            "type": "lovelace/dashboards/update",
            "dashboard_id": live["id"],
            "title": "Renamed",
            "icon": "mdi:other",
            "show_in_sidebar": False,
        },
    )
    await ws_ok(
        core_client,
        {
            "type": "lovelace/config/save",
            "url_path": "kitchen-dash",
            "config": {"views": []},
        },
    )

    await handler.restore(core_client, "kitchen-dash", held)

    restored = await dashboard_row(core_client, "kitchen-dash")
    assert restored["id"] == live["id"]
    assert restored["title"] == "Kitchen"
    assert restored["show_in_sidebar"] is True
    assert restored.get("icon") == icon
    assert await stored_config(core_client, "kitchen-dash") == BODY


@pytest.mark.parametrize("with_component", [True, False], ids=["component", "core"])
async def test_restore_puts_back_every_setting_core_lets_a_dashboard_update(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    dashboard_manager: bm.BackupManager,
    with_component: bool,
) -> None:
    """The sidebar and admin settings come back with the title, whether or not
    the component reports Core's update schema."""
    if with_component:
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    await add_dashboard(core_client, require_admin=True, show_in_sidebar=False)
    handler = _handler(dashboard_manager)
    held = await handler.fetch(core_client, "kitchen-dash")
    live = await dashboard_row(core_client, "kitchen-dash")
    await ws_ok(
        core_client,
        {
            "type": "lovelace/dashboards/update",
            "dashboard_id": live["id"],
            "require_admin": False,
            "show_in_sidebar": True,
        },
    )

    await handler.restore(core_client, "kitchen-dash", held)

    restored = await dashboard_row(core_client, "kitchen-dash")
    assert restored["require_admin"] is True
    assert restored["show_in_sidebar"] is False


async def test_restore_with_the_component_sends_only_what_core_update_accepts(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    dashboard_manager: bm.BackupManager,
) -> None:
    """A stored key Core's update schema does not take would get the update
    refused after the config was already written."""
    from custom_components.ha_mcp_tools import websocket_api

    websocket_api.async_register_commands(hass)
    await add_dashboard(core_client)
    handler = _handler(dashboard_manager)
    held = await handler.fetch(core_client, "kitchen-dash")
    held["dashboard"]["created_at"] = "2026-11-01"
    await ws_ok(
        core_client,
        {
            "type": "lovelace/dashboards/update",
            "dashboard_id": held["dashboard"]["id"],
            "title": "Renamed",
        },
    )

    await handler.restore(core_client, "kitchen-dash", held)

    assert (await dashboard_row(core_client, "kitchen-dash"))["title"] == "Kitchen"


async def test_restore_writes_nothing_when_the_dashboard_list_cannot_be_read(
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    sent: AsyncMock,
) -> None:
    row = {"id": "kitchen_dash", "url_path": "kitchen-dash", "title": "Kitchen"}

    with pytest.raises(HomeAssistantError, match="nothing was restored"):
        await _handler(snapshot_manager).restore(
            core_client, "kitchen-dash", {"dashboard": row, "config": BODY}
        )

    assert [c.args[0]["type"] for c in sent.call_args_list] == [
        "lovelace/dashboards/list"
    ]


async def test_restore_reports_settings_left_unrestored_after_the_config_write(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    """A stored key Core's update refuses, without the component to filter it,
    leaves the config restored and the settings not."""
    await add_dashboard(core_client, body={"views": []})
    handler = _handler(dashboard_manager)
    held = await handler.fetch(core_client, "kitchen-dash")
    held["config"] = BODY
    held["dashboard"]["created_at"] = "2026-11-01"

    with pytest.raises(bm.BackupRestoreError) as excinfo:
        await handler.restore(core_client, "kitchen-dash", held)

    assert excinfo.value.outcome["apply_status"] == "unknown"
    assert excinfo.value.outcome["reason"] == "settings_not_restored"
    assert await stored_config(core_client, "kitchen-dash") == BODY


async def test_a_captured_dashboard_without_a_settings_row_restores_its_body(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    """The default dashboard of a fresh install has no row; its snapshot must
    still put the body back."""
    await ws_ok(core_client, {"type": "lovelace/config/save", "config": BODY})
    handler = _handler(dashboard_manager)
    held: Any = await handler.fetch(core_client, "lovelace")
    await ws_ok(core_client, {"type": "lovelace/config/save", "config": {"views": []}})

    await handler.restore(core_client, "lovelace", held)

    assert await stored_config(core_client, None) == BODY


async def _old_body_only_snapshot(manager: bm.BackupManager) -> str:
    """A dashboard snapshot as written before the settings row was captured."""
    path = await manager.maybe_snapshot("dashboard", "kitchen-dash")
    assert path is not None

    def body_only(payload: dict[str, Any]) -> None:
        kind = payload.pop("kind")
        assert kind == "dashboard_settings"
        payload["config"] = payload["config"]["config"]

    rewrite_snapshot(path, body_only)
    return path.name


async def test_restoring_an_old_body_only_snapshot_of_a_deleted_dashboard_explains_why(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    await add_dashboard(core_client)
    name = await _old_body_only_snapshot(dashboard_manager)
    await _delete(core_client, "kitchen-dash")

    with pytest.raises(bm.BackupRestoreError) as excinfo:
        await dashboard_manager.restore_snapshot(name, take_safety_backup=False)

    assert excinfo.value.outcome["reason"] == "entry_deleted"
    assert "ha_config_set_dashboard" in excinfo.value.outcome["suggestions"][0]
    assert await dashboard_paths(core_client) == []


async def test_restoring_an_old_body_only_snapshot_of_a_live_dashboard_restores_its_body(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    """The twin of the deleted case: while the dashboard exists, the body
    alone is put back."""
    await add_dashboard(core_client)
    name = await _old_body_only_snapshot(dashboard_manager)
    await ws_ok(
        core_client,
        {
            "type": "lovelace/config/save",
            "url_path": "kitchen-dash",
            "config": {"views": []},
        },
    )

    await dashboard_manager.restore_snapshot(name, take_safety_backup=False)

    assert await stored_config(core_client, "kitchen-dash") == BODY


async def test_an_old_body_only_snapshot_previews_against_the_live_config(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    """An unchanged dashboard must not show its settings row as a difference."""
    await add_dashboard(core_client)
    name = await _old_body_only_snapshot(dashboard_manager)

    assert (await dashboard_manager.diff_snapshot(name))["unchanged"] is True


async def test_a_snapshot_without_a_settings_row_shows_a_row_added_since(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    await add_dashboard(core_client)
    path = await dashboard_manager.maybe_snapshot("dashboard", "kitchen-dash")
    assert path is not None

    def without_row(payload: dict[str, Any]) -> None:
        payload["config"]["dashboard"] = None

    rewrite_snapshot(path, without_row)

    diff = await dashboard_manager.diff_snapshot(path.name)

    assert diff["kind"] == "dict"
    assert diff["unchanged"] is False
    assert [op["path"] for op in diff["patch"]] == ["/dashboard"]


async def test_a_settings_snapshot_holding_a_bare_body_is_refused(
    core_client: HomeAssistantClient, dashboard_manager: bm.BackupManager
) -> None:
    """The format comes from the snapshot's kind, never from the body's keys."""
    await add_dashboard(core_client)
    path = await dashboard_manager.maybe_snapshot("dashboard", "kitchen-dash")
    assert path is not None

    def bare_body(payload: dict[str, Any]) -> None:
        payload["config"] = payload["config"]["config"]

    rewrite_snapshot(path, bare_body)

    with pytest.raises(bm.BackupRestoreError) as excinfo:
        await dashboard_manager.restore_snapshot(path.name, take_safety_backup=False)

    assert excinfo.value.outcome["reason"] == "invalid_snapshot"
