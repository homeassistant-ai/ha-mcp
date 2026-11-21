"""Settings export and import on a server's own settings page (#3099).

Standalone, Docker, the app (add-on) and the stdio sidecar serve these routes
themselves: they move the settings files, the File & YAML Tools permissions
through the component's services, and in the app the settings Supervisor
keeps as its options. The server's client talks to a real ``hass``.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.helpers.storage import Store

from ha_mcp.client.rest_client import HomeAssistantClient

from ._file_services import make_file_service
from ._settings_ui_app import (
    SELF_INFO,
    SELF_OPTIONS,
    app_server,
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
    # Nothing of the connection lands on this server.
    names = await asyncio.to_thread(os.listdir, settings_world)
    assert {n for n in names if not n.startswith(".")} == {"backup_settings.json"}


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


async def test_a_refused_directory_leaves_the_permissions_as_they_were(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    await make_file_service(hass, allowed="pyscript")
    original = await _export(monkeypatch, core_client)
    bundle = json.loads(json.dumps(original))
    bundle["entries"]["tools"]["allowed_dirs"] = ["www", "../outside"]

    response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

    assert (response.status_code, response.json()["error"]) == (400, "invalid_backup")
    assert await _export(monkeypatch, core_client) == original


async def test_a_failed_permission_write_restores_files_and_permissions(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    await make_file_service(hass, allowed="pyscript")
    original = await _export(monkeypatch, core_client)
    bundle = json.loads(json.dumps(original))
    bundle["entries"]["tools"] = {"allowed_dirs": ["www"], "extra_yaml_keys": ["mqtt"]}
    bundle["server_settings"]["features"] = {"redact_secrets": True}
    save = Store.async_save

    async def fail_keys(store: Store, data: Any) -> None:
        if data == {"keys": ["mqtt"]}:
            raise OSError("storage unavailable")
        await save(store, data)

    monkeypatch.setattr(Store, "async_save", fail_keys)
    response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

    assert (response.status_code, response.json()["error"]) == (500, "restore_failed")
    monkeypatch.setattr(Store, "async_save", save)
    assert await _export(monkeypatch, core_client) == original


async def test_one_failed_permission_undo_still_undoes_the_other(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    await make_file_service(hass, allowed="pyscript")
    bundle = await _export(monkeypatch, core_client)
    # "http" is refused after "mqtt" is saved, so the import fails and undoes both.
    bundle["entries"]["tools"] = {
        "allowed_dirs": ["www"],
        "extra_yaml_keys": ["mqtt", "http"],
    }
    save = Store.async_save

    async def fail_paths_undo(store: Store, data: Any) -> None:
        if data == {"paths": ["pyscript"]}:
            raise OSError("storage unavailable")
        await save(store, data)

    monkeypatch.setattr(Store, "async_save", fail_paths_undo)
    response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

    assert (response.status_code, response.json()["error"]) == (
        400,
        "rollback_incomplete",
    )
    monkeypatch.setattr(Store, "async_save", save)
    assert (await _export(monkeypatch, core_client))["entries"]["tools"] == {
        "allowed_dirs": ["www"],
        "extra_yaml_keys": [],
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


async def test_an_oversized_upload_is_refused_while_it_is_read(
    monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> None:
    chunk = b"x" * (1024 * 1024)
    sent = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(64):
            sent += len(chunk)
            yield chunk

    async with settings_http(app_server(monkeypatch, core_client)) as http:
        response = await http.post(
            IMPORT, content=body(), headers={"Content-Type": "application/json"}
        )

    assert (response.status_code, response.json()["error"]) == (413, "too_large")
    # Stops reading just past the limit instead of holding all 64 MiB.
    assert sent <= 6 * len(chunk)


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


class TestAnUnreadableAppConfiguration:
    """Supervisor cannot report the app's options."""

    async def test_export_fails_instead_of_leaving_the_app_options_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: Supervisor,
        core_client: HomeAssistantClient,
    ) -> None:
        in_app(monkeypatch, supervisor)
        supervisor.refuse(*SELF_INFO, "unavailable", status=503)

        response = await post_settings(monkeypatch, core_client, EXPORT, {})

        assert (response.status_code, response.json()["error"]) == (
            500,
            "export_failed",
        )

    async def test_import_fails_and_restores_the_files(
        self,
        settings_world: Path,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: Supervisor,
        core_client: HomeAssistantClient,
    ) -> None:
        bundle = await _export(monkeypatch, core_client)
        bundle["server_settings"]["backup"] = {"auto_backup_retain_per_entity": 30}
        in_app(monkeypatch, supervisor)
        supervisor.refuse(*SELF_INFO, "unavailable", status=503)

        response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

        assert (response.status_code, response.json()["error"]) == (
            500,
            "restore_failed",
        )
        assert not (settings_world / "backup_settings.json").exists()
        assert supervisor.sent(*SELF_OPTIONS) == []


async def test_an_undo_keeps_a_permission_saved_during_the_import(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    administrator = await make_file_service(hass, allowed="pyscript")
    bundle = await _export(monkeypatch, core_client)
    # "http" is refused, so the import fails after both writes and undoes them.
    bundle["entries"]["tools"] = {
        "allowed_dirs": ["www"],
        "extra_yaml_keys": ["mqtt", "http"],
    }
    save = Store.async_save
    saves: list[asyncio.Future[Any]] = []

    async def another_save_in_between(store: Store, data: Any) -> None:
        await save(store, data)
        if data == {"paths": ["www"]}:
            # Another administrator saves the directories right after the import.
            saves.append(
                asyncio.ensure_future(
                    administrator("set_allowed_paths", {"paths": ["media"]})
                )
            )
        elif data == {"keys": ["mqtt"]}:
            await saves[0]

    monkeypatch.setattr(Store, "async_save", another_save_in_between)
    response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

    assert (response.status_code, response.json()["error"]) == (400, "invalid_backup")
    monkeypatch.setattr(Store, "async_save", save)
    assert (await _export(monkeypatch, core_client))["entries"]["tools"] == {
        "allowed_dirs": ["media"],
        "extra_yaml_keys": [],
    }


def _on_service_call(hass: HomeAssistant, service: str) -> asyncio.Event:
    """Set when Home Assistant receives a call to ``ha_mcp_tools.<service>``."""
    called = asyncio.Event()

    @callback
    def seen(event: Event) -> None:
        if (
            event.data.get("domain") == "ha_mcp_tools"
            and event.data.get("service") == service
        ):
            called.set()

    hass.bus.async_listen(EVENT_CALL_SERVICE, seen)
    return called


async def test_a_lost_permission_reply_that_cannot_be_undone_is_reported(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    hass_access_token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_file_service(hass, allowed="pyscript")
    bundle = await _export(monkeypatch, core_client)
    # The component stores ["www"] for this list.
    bundle["entries"]["tools"] = {
        "allowed_dirs": ["www", "../outside"],
        "extra_yaml_keys": [],
    }
    impatient = HomeAssistantClient(
        base_url=core_client.base_url,
        token=hass_access_token,
        verify_ssl=False,
        timeout=2,
    )
    save = Store.async_save
    calls: list[asyncio.Event] = []

    async def save_once_the_reply_is_lost(store: Store, data: Any) -> None:
        if data == {"paths": ["www"]}:
            # Held until the import's undo arrives, so the first reply times out
            # and Core stores the list only then.
            calls.append(_on_service_call(hass, "set_allowed_paths"))
            await asyncio.wait_for(calls[0].wait(), 30)
        await save(store, data)

    monkeypatch.setattr(Store, "async_save", save_once_the_reply_is_lost)
    try:
        response = await post_settings(monkeypatch, impatient, IMPORT, bundle)
    finally:
        await impatient.close()

    # The undo got a reply and kept the value (an undo that failed logs differently).
    assert "could not undo allowed_dirs: its write got no reply" in caplog.text
    assert (response.status_code, response.json()["error"]) == (
        500,
        "rollback_incomplete",
    )


async def test_an_export_waits_for_an_import_in_progress(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    await make_file_service(hass, allowed="pyscript")
    original = await _export(monkeypatch, core_client)
    bundle = json.loads(json.dumps(original))
    bundle["server_settings"]["features"] = {"redact_secrets": True}
    # "http" is refused, so the import fails and rolls back.
    bundle["entries"]["tools"] = {"allowed_dirs": ["www"], "extra_yaml_keys": ["http"]}
    paused, release = asyncio.Event(), asyncio.Event()
    save = Store.async_save

    async def pause_the_import(store: Store, data: Any) -> None:
        await save(store, data)
        if data == {"paths": ["www"]}:
            paused.set()
            await release.wait()

    monkeypatch.setattr(Store, "async_save", pause_the_import)
    importing = asyncio.create_task(
        post_settings(monkeypatch, core_client, IMPORT, bundle)
    )
    try:
        await paused.wait()
        exporting = asyncio.create_task(_export(monkeypatch, core_client))
        await asyncio.sleep(0.3)
    finally:
        release.set()
    await importing

    assert await exporting == original


async def test_a_permission_saved_after_the_import_read_it_stops_the_import(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    administrator = await make_file_service(hass, allowed="pyscript")
    original = await _export(monkeypatch, core_client)
    bundle = json.loads(json.dumps(original))
    bundle["entries"]["tools"] = {"allowed_dirs": ["www"], "extra_yaml_keys": []}
    read = _on_service_call(hass, "get_extra_yaml_keys")
    saved: list[asyncio.Task[Any]] = []

    async def save_after_the_read() -> None:
        await read.wait()
        await administrator("set_allowed_paths", {"paths": ["media"]})

    saved.append(asyncio.ensure_future(save_after_the_read()))
    response = await post_settings(monkeypatch, core_client, IMPORT, bundle)
    await saved[0]

    assert (response.status_code, response.json()["error"]) == (
        409,
        "permissions_changed",
    )
    assert (await _export(monkeypatch, core_client))["entries"]["tools"] == {
        "allowed_dirs": ["media"],
        "extra_yaml_keys": [],
    }


async def test_a_component_without_conditional_writes_refuses_permissions(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    await make_file_service(hass, allowed="pyscript")
    original = await _export(monkeypatch, core_client)
    bundle = json.loads(json.dumps(original))
    bundle["entries"]["tools"] = {"allowed_dirs": ["www"], "extra_yaml_keys": []}
    # The description an older component registers: no only_if field.
    async_set_service_schema(
        hass,
        "ha_mcp_tools",
        "set_allowed_paths",
        {"name": "Set Allowed Filesystem Paths", "fields": {"paths": {}}},
    )

    response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

    assert (response.status_code, response.json()["error"]) == (
        409,
        "component_outdated",
    )
    assert await _export(monkeypatch, core_client) == original


async def test_a_restore_the_component_rejects_is_an_incomplete_rollback(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    config = Path(hass.config.config_dir)
    outside = config.parent / "outside_config"
    await asyncio.to_thread((config / "shared").mkdir)
    await asyncio.to_thread(outside.mkdir)
    await make_file_service(hass, allowed="shared")
    bundle = await _export(monkeypatch, core_client)
    # "http" is refused, so the import fails and undoes the directories.
    bundle["entries"]["tools"] = {"allowed_dirs": ["www"], "extra_yaml_keys": ["http"]}
    save = Store.async_save

    def point_outside() -> None:
        (config / "shared").rmdir()
        (config / "shared").symlink_to(outside, target_is_directory=True)

    async def move_the_directory(store: Store, data: Any) -> None:
        await save(store, data)
        if data == {"paths": ["www"]}:
            # "shared" now leads outside the config directory.
            await asyncio.to_thread(point_outside)

    monkeypatch.setattr(Store, "async_save", move_the_directory)
    response = await post_settings(monkeypatch, core_client, IMPORT, bundle)

    assert (response.status_code, response.json()["error"]) == (
        400,
        "rollback_incomplete",
    )
