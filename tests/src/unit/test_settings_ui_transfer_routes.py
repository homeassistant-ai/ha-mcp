"""Settings export and import on a server's own settings page (#3099).

Standalone, Docker, the app (add-on) and the stdio sidecar serve these routes
themselves: they move the settings files, the File & YAML Tools permissions
through the component's services, and in the app the settings Supervisor
keeps as its options. The server's client talks to a real ``hass``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient

from ._file_services import make_file_service
from ._settings_ui_app import (
    SELF_INFO,
    SELF_OPTIONS,
    in_app,
    post_settings,
    settings_http,
)
from ._settings_ui_app import settings_world as settings_world
from ._tools_addons_core import Supervisor, app_info
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

EXPORT = "/api/settings/transfer/export"
IMPORT = "/api/settings/transfer/import"


async def _export(
    monkeypatch: pytest.MonkeyPatch, client: HomeAssistantClient
) -> dict[str, Any]:
    response = await post_settings(monkeypatch, client, EXPORT, {})
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].startswith("attachment;")
    bundle: dict[str, Any] = response.json()
    return bundle


def _write(directory: Path, name: str, document: dict[str, Any]) -> None:
    (directory / name).write_text(json.dumps(document), encoding="utf-8")


async def test_standalone_settings_round_trip_without_connection_secrets(
    settings_world: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    _write(settings_world, "tool_config.json", {"tools": {"ha_search": "disabled"}})
    _write(settings_world, "feature_flags.json", {"redact_secrets": True})
    original = await _export(monkeypatch, core_client)
    assert original["contains_credentials"] is False
    # No File & YAML Tools entry: nothing to say about its permissions.
    assert original["entries"] == {}

    _write(settings_world, "tool_config.json", {"tools": {}})
    response = await post_settings(monkeypatch, core_client, IMPORT, original)

    assert response.json() == {
        "success": True,
        "restart_required": True,
        "restart": "server",
        "skipped": [],
    }
    assert await _export(monkeypatch, core_client) == original


async def test_an_embedded_servers_file_keeps_this_servers_connection(
    settings_world: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    bundle = await _export(monkeypatch, core_client)
    bundle["contains_credentials"] = True
    bundle["entries"]["server"] = {"data": {"webhook_id": "x"}, "options": {}}
    bundle["server_settings"]["backup"] = {"auto_backup_retain_per_entity": 21}

    response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

    assert response.json()["skipped"] == ["connection"]
    assert json.loads((settings_world / "backup_settings.json").read_text()) == {
        "auto_backup_retain_per_entity": 21
    }


async def test_file_permissions_move_through_the_tools_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    await make_file_service(hass, allowed="pyscript")
    bundle = await _export(monkeypatch, core_client)
    assert bundle["entries"]["tools"] == {
        "allowed_dirs": ["pyscript"],
        "extra_yaml_keys": [],
    }
    bundle["entries"]["tools"] = {"allowed_dirs": ["www"], "extra_yaml_keys": ["mqtt"]}

    response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

    assert response.json()["skipped"] == []
    assert (await _export(monkeypatch, core_client))["entries"]["tools"] == {
        "allowed_dirs": ["www"],
        "extra_yaml_keys": ["mqtt"],
    }


async def test_permissions_without_a_tools_entry_are_reported_as_skipped(
    monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> None:
    bundle = await _export(monkeypatch, core_client)
    bundle["entries"]["tools"] = {"allowed_dirs": ["www"], "extra_yaml_keys": []}

    response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

    assert response.status_code == 200
    assert response.json()["skipped"] == ["file_permissions"]


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ({"version": 2, "new_section": {}}, 400, "incompatible_version"),
        ({"format": "other"}, 400, "invalid_backup"),
        ({"contains_credentials": True}, 400, "invalid_backup"),
        ({"server_settings": {"tools": "no"}}, 400, "invalid_backup"),
    ],
)
async def test_an_unusable_file_changes_nothing(
    settings_world: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    change: dict[str, Any],
    status: int,
    code: str,
) -> None:
    _write(settings_world, "feature_flags.json", {"redact_secrets": True})
    original = await _export(monkeypatch, core_client)

    response = await post_settings(
        monkeypatch, core_client, IMPORT, {**original, **change}
    )

    assert (response.status_code, response.json()["error"]) == (status, code)
    assert await _export(monkeypatch, core_client) == original


async def test_an_oversized_upload_is_refused_before_parsing(
    monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> None:
    response = await post_settings(
        monkeypatch, core_client, IMPORT, {"padding": "x" * (4 * 1024 * 1024)}
    )

    assert (response.status_code, response.json()["error"]) == (413, "too_large")


async def test_the_sidecar_moves_settings_without_a_live_server(
    settings_world: Path,
) -> None:
    _write(settings_world, "feature_flags.json", {"redact_secrets": True})
    async with settings_http(None, is_sidecar=True) as http:
        exported = (await http.post(EXPORT, json={})).json()
        exported["server_settings"]["features"] = {"redact_secrets": False}
        response = await http.post(IMPORT, json=exported)

    assert response.json()["restart_required"] is True
    assert json.loads((settings_world / "feature_flags.json").read_text()) == {
        "redact_secrets": False
    }


class TestInsideTheApp:
    @pytest.fixture
    def app(
        self, monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor
    ) -> Supervisor:
        in_app(monkeypatch, supervisor)
        supervisor.answer(
            *SELF_INFO,
            app_info(
                options={"enable_tool_search": True, "auto_backup_retain_per_entity": 9}
            ),
        )
        return supervisor

    async def test_export_carries_the_settings_kept_as_app_options(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        bundle = await _export(monkeypatch, core_client)

        assert bundle["server_settings"]["features"] == {"enable_tool_search": True}
        assert bundle["server_settings"]["backup"] == {
            "auto_backup_retain_per_entity": 9
        }

    async def test_import_writes_the_app_options_it_changes(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        app.answer(*SELF_OPTIONS)
        bundle = await _export(monkeypatch, core_client)
        bundle["server_settings"]["backup"] = {"auto_backup_retain_per_entity": 30}

        response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

        assert response.json()["restart_required"] is True
        [sent] = app.sent(*SELF_OPTIONS)
        assert sent["options"] == {
            "enable_tool_search": True,
            "auto_backup_retain_per_entity": 30,
        }

    async def test_supervisor_refusing_the_options_restores_the_files(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        app.refuse(*SELF_OPTIONS, "unknown option")
        original = await _export(monkeypatch, core_client)
        bundle = json.loads(json.dumps(original))
        bundle["server_settings"]["backup"] = {"auto_backup_retain_per_entity": 30}
        bundle["server_settings"]["policy"] = {"wait_seconds": 12}

        response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

        assert (response.status_code, response.json()["error"]) == (
            500,
            "restore_failed",
        )
        assert await _export(monkeypatch, core_client) == original
