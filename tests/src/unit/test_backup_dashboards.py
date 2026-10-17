"""A dashboard snapshot can bring back a dashboard that was deleted."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import ha_mcp.backup_manager as bm
import ha_mcp.tools.tools_config_dashboards as dash
from ha_mcp.backup_dashboards import fetch_dashboard, restore_dashboard
from ha_mcp.client.rest_client import HomeAssistantCommandError, HomeAssistantError

from .test_backup_manager import _StubSettings

_BODY = {"views": [{"title": "Kitchen", "cards": []}]}
_LIST = {"type": "lovelace/dashboards/list"}


def _row(url_path: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": url_path.replace("-", "_"),
        "url_path": url_path,
        "title": "Kitchen",
        "show_in_sidebar": True,
        "require_admin": False,
        "mode": "storage",
        **fields,
    }


def _resolver(rows: list[dict[str, Any]], sent: list[Any] | None = None) -> Any:
    """``_resolve_dashboard`` over ``rows``; logs its list read into ``sent``."""

    async def resolve(_client: Any, identifier: str) -> Any:
        if sent is not None:
            sent.append(_LIST)
        for row in rows:
            if identifier in (row["id"], row["url_path"]):
                return {"url_path": row["url_path"], "id": row["id"]}, rows
        return None, rows

    return resolve


async def _unreadable_list(_client: Any, _identifier: str) -> Any:
    return None, None


async def _failing_list(_client: Any, _identifier: str) -> Any:
    raise HomeAssistantError("lovelace/dashboards/list timed out")


async def _component_body(_client: Any, _url_path: str) -> Any:
    return _BODY


class _FakeWs:
    """Answers ``config_not_found`` to saves until a dashboard is created."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.created = False
        self.fail_update = False

    async def __call__(self, _client: Any, message: dict[str, Any]) -> Any:
        self.sent.append(message)
        if message["type"] == "lovelace/dashboards/create":
            self.created = True
        elif message["type"] == "lovelace/dashboards/update":
            if self.fail_update:
                raise HomeAssistantCommandError("Command failed: bad", "invalid_format")
        elif not self.created:
            raise HomeAssistantCommandError(
                f"Unknown config specified: {message['url_path']}", "config_not_found"
            )
        return None


async def test_capture_keeps_the_dashboard_settings_needed_to_recreate_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row("kitchen-dash", icon="mdi:stove")
    monkeypatch.setattr(dash, "_resolve_dashboard", _resolver([row]))
    monkeypatch.setattr(dash, "_component_dashboard_config", _component_body)

    assert await fetch_dashboard(object(), "kitchen_dash") == {
        "dashboard": row,
        "config": _BODY,
    }


async def test_capture_of_a_dashboard_without_a_settings_row_keeps_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh install's default dashboard has no row until Core migrates it."""
    monkeypatch.setattr(dash, "_resolve_dashboard", _resolver([_row("other-dash")]))
    monkeypatch.setattr(dash, "_component_dashboard_config", _component_body)

    assert await fetch_dashboard(object(), "lovelace") == {
        "dashboard": None,
        "config": _BODY,
    }


@pytest.mark.parametrize(
    "resolver", [_unreadable_list, _failing_list], ids=["unexpected-shape", "error"]
)
async def test_capture_fails_when_the_dashboard_list_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, resolver: Any
) -> None:
    """An unread list is not a dashboard without settings."""
    monkeypatch.setattr(dash, "_resolve_dashboard", resolver)
    monkeypatch.setattr(dash, "_component_dashboard_config", _component_body)

    with pytest.raises(HomeAssistantError, match="could not be captured"):
        await fetch_dashboard(object(), "kitchen-dash")


async def test_capture_of_a_never_saved_dashboard_keeps_its_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a dashboard that has no saved config must still be undoable."""

    async def no_component_body(_client: Any, _url_path: str) -> Any:
        return None

    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value={
            "success": False,
            "error": "Command failed: No config found.",
            "error_code": "config_not_found",
        }
    )
    row = _row("kitchen-dash")
    monkeypatch.setattr(dash, "_resolve_dashboard", _resolver([row]))
    monkeypatch.setattr(dash, "_component_dashboard_config", no_component_body)

    assert await fetch_dashboard(client, "kitchen-dash") == {
        "dashboard": row,
        "config": None,
    }


@pytest.mark.parametrize(
    ("present", "expected_types"),
    [
        (False, ["lovelace/dashboards/list", "lovelace/dashboards/create"]),
        (
            True,
            [
                "lovelace/dashboards/list",
                "lovelace/config/delete",
                "lovelace/dashboards/update",
            ],
        ),
    ],
    ids=["deleted", "since-saved"],
)
async def test_restore_of_a_never_saved_dashboard_returns_it_to_no_stored_config(
    monkeypatch: pytest.MonkeyPatch, present: bool, expected_types: list[str]
) -> None:
    ws = _FakeWs()
    ws.created = present
    monkeypatch.setattr(bm, "_ws_send", ws)
    row = _row("kitchen-dash")
    rows = [row] if present else []
    monkeypatch.setattr(dash, "_resolve_dashboard", _resolver(rows, ws.sent))

    await restore_dashboard(object(), row["id"], {"dashboard": row, "config": None})

    assert [m["type"] for m in ws.sent] == expected_types


async def test_restore_of_deleted_lovelace_falls_back_to_the_default_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core re-registers its built-in /lovelace panel, so the create is refused."""
    sent: list[dict[str, Any]] = []

    async def ws(_client: Any, message: dict[str, Any]) -> Any:
        sent.append(message)
        if message.get("url_path") != "lovelace":
            return None
        # websocket_api's ERR_HOME_ASSISTANT_ERROR for url_already_exists.
        raise HomeAssistantCommandError(
            'The URL "lovelace" is already in use.', "home_assistant_error"
        )

    monkeypatch.setattr(bm, "_ws_send", ws)
    monkeypatch.setattr(dash, "_resolve_dashboard", _resolver([], sent))
    row = _row("lovelace", icon="mdi:view-dashboard")

    result = await restore_dashboard(
        object(), "lovelace", {"dashboard": row, "config": _BODY}
    )

    assert sent[-1] == {"type": "lovelace/config/save", "config": _BODY}
    assert [m["type"] for m in sent[:-1]] == [
        "lovelace/dashboards/list",
        "lovelace/dashboards/create",
    ]
    assert "restart" in result["note"]


@pytest.mark.parametrize(
    ("row", "expected_create"),
    [
        (
            # Component rows report an unset icon as None.
            _row("kitchen-dash", icon=None),
            {
                "type": "lovelace/dashboards/create",
                "url_path": "kitchen-dash",
                "title": "Kitchen",
                "show_in_sidebar": True,
                "require_admin": False,
            },
        ),
        (
            _row("map", icon="mdi:map"),
            {
                "type": "lovelace/dashboards/create",
                "url_path": "map",
                "title": "Kitchen",
                "show_in_sidebar": True,
                "require_admin": False,
                "icon": "mdi:map",
                "allow_single_word": True,
            },
        ),
    ],
    ids=["hyphenated", "single-word"],
)
async def test_restore_recreates_a_deleted_dashboard_then_saves_its_config(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, Any],
    expected_create: dict[str, Any],
) -> None:
    ws = _FakeWs()
    monkeypatch.setattr(bm, "_ws_send", ws)
    monkeypatch.setattr(dash, "_resolve_dashboard", _resolver([], ws.sent))
    save = {
        "type": "lovelace/config/save",
        "url_path": row["url_path"],
        "config": _BODY,
    }

    await restore_dashboard(object(), row["id"], {"dashboard": row, "config": _BODY})

    assert ws.sent == [_LIST, expected_create, save]


@pytest.mark.parametrize(
    ("icon", "fields"),
    [("mdi:stove", {"icon": "mdi:stove"}), (None, {"icon": None}), (None, {})],
    ids=["icon", "icon-cleared", "core-row-without-icon"],
)
async def test_restore_to_an_existing_dashboard_saves_its_config_and_settings(
    monkeypatch: pytest.MonkeyPatch, icon: str | None, fields: dict[str, Any]
) -> None:
    ws = _FakeWs()
    ws.created = True
    monkeypatch.setattr(bm, "_ws_send", ws)
    row = _row("kitchen-dash", **fields)
    live = _row("kitchen-dash", id="live_id", title="Renamed", icon="mdi:other")
    monkeypatch.setattr(dash, "_resolve_dashboard", _resolver([live], ws.sent))

    await restore_dashboard(object(), row["id"], {"dashboard": row, "config": _BODY})

    assert ws.sent == [
        _LIST,
        {"type": "lovelace/config/save", "url_path": "kitchen-dash", "config": _BODY},
        {
            "type": "lovelace/dashboards/update",
            "dashboard_id": "live_id",
            "title": "Kitchen",
            "icon": icon,
            "require_admin": False,
            "show_in_sidebar": True,
        },
    ]


async def test_restore_writes_nothing_when_the_dashboard_list_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWs()
    monkeypatch.setattr(bm, "_ws_send", ws)
    monkeypatch.setattr(dash, "_resolve_dashboard", _unreadable_list)
    row = _row("kitchen-dash")

    with pytest.raises(HomeAssistantError, match="nothing was restored"):
        await restore_dashboard(
            object(), row["id"], {"dashboard": row, "config": _BODY}
        )

    assert ws.sent == []


async def test_restore_reports_settings_left_unrestored_after_the_config_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWs()
    ws.created = True
    ws.fail_update = True
    monkeypatch.setattr(bm, "_ws_send", ws)
    row = _row("kitchen-dash")
    monkeypatch.setattr(dash, "_resolve_dashboard", _resolver([row], ws.sent))

    with pytest.raises(bm.BackupRestoreError) as excinfo:
        await restore_dashboard(
            object(), row["id"], {"dashboard": row, "config": _BODY}
        )

    assert excinfo.value.outcome["apply_status"] == "unknown"
    assert excinfo.value.outcome["reason"] == "settings_not_restored"
    assert [m["type"] for m in ws.sent] == [
        "lovelace/dashboards/list",
        "lovelace/config/save",
        "lovelace/dashboards/update",
    ]


async def test_a_captured_dashboard_without_a_settings_row_restores_its_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWs()
    ws.created = True
    monkeypatch.setattr(bm, "_ws_send", ws)

    await restore_dashboard(object(), "lovelace", {"dashboard": None, "config": _BODY})

    assert ws.sent == [
        {"type": "lovelace/config/save", "url_path": "lovelace", "config": _BODY}
    ]


def _manager(tmp_path: Path, fetched: list[Any]) -> Any:
    async def fetch(_client: Any, _entity_id: str) -> Any:
        return fetched.pop(0)

    mgr = bm.BackupManager(_StubSettings(auto_backup_dir=str(tmp_path)), object())
    mgr.register(bm.DomainHandler("dashboard", fetch, restore_dashboard))
    return mgr


async def _old_body_only_snapshot(mgr: Any) -> str:
    """A dashboard snapshot as written before the settings row was captured."""
    path = await mgr.maybe_snapshot("dashboard", "kitchen-dash")
    assert path is not None
    text = path.read_text()
    assert "kind: dashboard_settings\n" in text
    path.write_text(text.replace("kind: dashboard_settings\n", ""))
    return str(path.name)


async def test_restoring_an_old_body_only_snapshot_of_a_deleted_dashboard_explains_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mgr = _manager(tmp_path, [_BODY])
    name = await _old_body_only_snapshot(mgr)
    ws = _FakeWs()
    monkeypatch.setattr(bm, "_ws_send", ws)
    monkeypatch.setattr(dash, "_resolve_dashboard", _resolver([]))

    with pytest.raises(bm.BackupRestoreError) as excinfo:
        await mgr.restore_snapshot(name, take_safety_backup=False)

    assert excinfo.value.outcome["reason"] == "entry_deleted"
    assert "ha_config_set_dashboard" in excinfo.value.outcome["suggestions"][0]
    assert ws.sent == [
        {"type": "lovelace/config/save", "url_path": "kitchen-dash", "config": _BODY}
    ]


async def test_an_old_body_only_snapshot_previews_against_the_live_config(
    tmp_path: Path,
) -> None:
    """An unchanged dashboard must not show its settings row as a difference."""
    mgr = _manager(
        tmp_path, [_BODY, {"dashboard": _row("kitchen-dash"), "config": _BODY}]
    )
    name = await _old_body_only_snapshot(mgr)

    assert (await mgr.diff_snapshot(name))["unchanged"] is True


async def test_a_snapshot_without_a_settings_row_shows_a_row_added_since(
    tmp_path: Path,
) -> None:
    row = _row("kitchen-dash")
    mgr = _manager(
        tmp_path,
        [{"dashboard": None, "config": _BODY}, {"dashboard": row, "config": _BODY}],
    )
    path = await mgr.maybe_snapshot("dashboard", "kitchen-dash")
    assert path is not None

    diff = await mgr.diff_snapshot(path.name)

    assert diff["unchanged"] is False
    assert [op["path"] for op in diff["patch"]] == ["/dashboard"]


async def test_a_settings_snapshot_holding_a_bare_body_is_refused(
    tmp_path: Path,
) -> None:
    """The format comes from the snapshot's kind, never from the body's keys."""
    mgr = _manager(tmp_path, [_BODY])
    path = await mgr.maybe_snapshot("dashboard", "kitchen-dash")
    assert path is not None

    with pytest.raises(bm.BackupRestoreError) as excinfo:
        await mgr.restore_snapshot(path.name, take_safety_backup=False)

    assert excinfo.value.outcome["reason"] == "invalid_snapshot"
