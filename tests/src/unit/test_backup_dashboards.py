"""A dashboard snapshot can bring back a dashboard that was deleted."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import ha_mcp.backup_manager as bm
import ha_mcp.tools.tools_config_dashboards as dash
from ha_mcp.backup_dashboards import fetch_dashboard, restore_dashboard
from ha_mcp.client.rest_client import HomeAssistantCommandError

from .test_backup_manager import _StubSettings

_BODY = {"views": [{"title": "Kitchen", "cards": []}]}


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


def _resolver(rows: list[dict[str, Any]]) -> Any:
    async def resolve(_client: Any, identifier: str) -> Any:
        for row in rows:
            if identifier in (row["id"], row["url_path"]):
                return {"url_path": row["url_path"], "id": row["id"]}, rows
        return None, rows

    return resolve


async def _component_body(_client: Any, _url_path: str) -> Any:
    return _BODY


class _FakeWs:
    """Answers ``config_not_found`` to saves until a dashboard is created."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.created = False

    async def __call__(self, _client: Any, message: dict[str, Any]) -> Any:
        self.sent.append(message)
        if message["type"] == "lovelace/dashboards/create":
            self.created = True
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

    assert await fetch_dashboard(object(), "lovelace") == _BODY


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
        (False, ["lovelace/config/delete", "lovelace/dashboards/create"]),
        (True, ["lovelace/config/delete"]),
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
        if message["type"] == "lovelace/dashboards/create":
            # websocket_api's ERR_HOME_ASSISTANT_ERROR for url_already_exists.
            raise HomeAssistantCommandError(
                'The URL "lovelace" is already in use.', "home_assistant_error"
            )
        raise HomeAssistantCommandError(
            "Unknown config specified: lovelace", "config_not_found"
        )

    monkeypatch.setattr(bm, "_ws_send", ws)
    row = _row("lovelace", icon="mdi:view-dashboard")

    result = await restore_dashboard(
        object(), "lovelace", {"dashboard": row, "config": _BODY}
    )

    assert sent[-1] == {"type": "lovelace/config/save", "config": _BODY}
    assert [m["type"] for m in sent[:-1]] == [
        "lovelace/config/save",
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
    save = {
        "type": "lovelace/config/save",
        "url_path": row["url_path"],
        "config": _BODY,
    }

    await restore_dashboard(object(), row["id"], {"dashboard": row, "config": _BODY})

    assert ws.sent == [save, expected_create, save]


async def test_restore_to_an_existing_dashboard_only_saves_its_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's save answers with no result, which must not read as a deleted dashboard."""
    ws = _FakeWs()
    ws.created = True
    monkeypatch.setattr(bm, "_ws_send", ws)
    row = _row("kitchen-dash")

    await restore_dashboard(object(), row["id"], {"dashboard": row, "config": _BODY})

    assert [m["type"] for m in ws.sent] == ["lovelace/config/save"]


async def test_restoring_an_old_body_only_snapshot_of_a_deleted_dashboard_explains_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWs()
    monkeypatch.setattr(bm, "_ws_send", ws)
    monkeypatch.setattr(dash, "_resolve_dashboard", _resolver([]))

    with pytest.raises(bm.BackupRestoreError) as excinfo:
        await restore_dashboard(object(), "kitchen-dash", _BODY)

    assert excinfo.value.outcome["reason"] == "entry_deleted"
    assert "ha_config_set_dashboard" in excinfo.value.outcome["suggestions"][0]
    assert [m["type"] for m in ws.sent] == ["lovelace/config/save"]


async def test_an_old_body_only_snapshot_previews_against_the_live_config(
    tmp_path: Path,
) -> None:
    """An unchanged dashboard must not show its settings row as a difference."""
    fetched = [_BODY, {"dashboard": _row("kitchen-dash"), "config": _BODY}]

    async def fetch(_client: Any, _entity_id: str) -> Any:
        return fetched.pop(0)

    mgr = bm.BackupManager(_StubSettings(auto_backup_dir=str(tmp_path)), object())
    mgr.register(bm.DomainHandler("dashboard", fetch, restore_dashboard))
    path = await mgr.maybe_snapshot("dashboard", "kitchen-dash")
    assert path is not None

    assert (await mgr.diff_snapshot(path.name))["unchanged"] is True
