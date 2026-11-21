"""Settings export and import through the sidebar panel's authenticated proxy."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.ha_mcp_tools.const import (
    SERVER_CONFIG_SUBDIR,
)
from custom_components.ha_mcp_tools.tools_settings_store import ALLOWED_PATHS_KEY
from ha_mcp import settings_transfer as server_transfer

from ._config_flow_core import add_options_entry, add_tools_entry
from ._ui_panel_core import (
    _APP,
    _SettingsServer,
)
from ._ui_panel_core import (
    admin_cookies as admin_cookies,
)
from ._ui_panel_core import (
    http_ready as http_ready,
)
from ._ui_panel_core import (
    panel_views as panel_views,
)
from ._ui_panel_core import (
    running_server as running_server,
)
from ._ui_panel_core import (
    settings_server as settings_server,
)


class TestSettingsTransfer:
    async def test_backup_secrets_require_live_same_origin_admin_session(
        self,
        hass: HomeAssistant,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        add_options_entry(
            hass,
            data={
                "webhook_id": "portable-id",
                "secret_path": "/private-backup",
                "admin_token": "destination-token",
            },
        )
        client = await hass_client_no_auth()
        url = _APP + "api/settings/transfer/export"
        anonymous = await client.post(
            url, json={}, headers={"Sec-Fetch-Site": "same-origin"}
        )
        assert anonymous.status == 401
        foreign = await client.post(
            url,
            json={},
            cookies=admin_cookies,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert foreign.status == 403
        exported = await client.post(
            url,
            json={},
            cookies=admin_cookies,
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert exported.status == 200
        assert exported.headers["Cache-Control"] == "no-store"
        assert exported.headers["Content-Disposition"].startswith("attachment;")
        bundle = await exported.json()
        assert bundle["entries"]["server"]["data"] == {
            "webhook_id": "portable-id",
            "secret_path": "/private-backup",
        }
        assert not running_server.seen

    async def test_unusable_source_settings_never_download_as_a_backup(
        self,
        hass: HomeAssistant,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        # An old entry can carry a root-only direct URL, which would expose
        # a destination server without its required secret path.
        add_options_entry(hass, data={"webhook_id": "private-hook", "secret_path": "/"})
        client = await hass_client_no_auth()
        response = await client.post(
            _APP + "api/settings/transfer/export",
            cookies=admin_cookies,
            json={},
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert response.status == 400
        assert await response.json() == {"success": False, "error": "invalid_backup"}
        assert "Content-Disposition" not in response.headers

    async def test_import_errors_do_not_echo_file_credentials(
        self,
        hass: HomeAssistant,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO)
        add_options_entry(hass)
        client = await hass_client_no_auth()
        resp = await client.post(
            _APP + "api/settings/transfer/import",
            cookies=admin_cookies,
            json={"secret_path": "/sensitive"},
            headers={"Origin": str(client.make_url("/").origin())},
        )
        assert resp.status == 400
        assert await resp.json() == {"success": False, "error": "invalid_backup"}
        assert not running_server.seen
        assert "Settings import refused (invalid_backup)" in caplog.text
        assert "sensitive" not in caplog.text

    @pytest.mark.parametrize("failure", ["published", "early_value", "early_io"])
    async def test_failed_server_document_undo_reports_incomplete_and_continues(
        self,
        hass: HomeAssistant,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
        monkeypatch: pytest.MonkeyPatch,
        failure: str,
    ) -> None:
        add_options_entry(hass, data={"webhook_id": "old", "secret_path": "/old"})
        await add_tools_entry(hass, loaded=True)
        client = await hass_client_no_auth()
        headers = {"Sec-Fetch-Site": "same-origin"}
        exported = await client.post(
            _APP + "api/settings/transfer/export",
            cookies=admin_cookies,
            json={},
            headers=headers,
        )
        assert exported.status == 200
        bundle = await exported.json()
        incoming = bundle["server_settings"]
        previous = {
            **incoming,
            "tools": {},
            "features": {"fuzzy_threshold": 60},
            "backup": {"auto_backup_retain_per_entity": 12},
        }
        directory = Path(hass.config.path(SERVER_CONFIG_SUBDIR))
        await hass.async_add_executor_job(
            server_transfer.restore_server_settings, directory, previous
        )
        incoming.update(
            tools={"tools": {}},
            features={"fuzzy_threshold": 70},
            backup={"auto_backup_retain_per_entity": 13},
        )
        real_replace = os.replace
        failed_undo = "backup" if failure == "published" else "features"
        names = {
            "backup_settings.json": "backup",
            "feature_flags.json": "features",
            "tool_config.json": "tools",
        }
        undone: list[str] = []

        def replace(src: str, dst: str) -> None:
            path = Path(dst)
            if path.parent == directory and path.name in names:
                key = names[path.name]
                value = json.loads(Path(src).read_bytes())
                if failure == "early_io" and key == "backup" and value == incoming[key]:
                    raise OSError("ordinary preference write failed")
                if value == previous[key]:
                    undone.append(key)
                    if key == failed_undo:
                        raise OSError("ordinary preference undo failed")
            real_replace(src, dst)

        real_dumps = json.dumps

        def dumps(value: object, **kwargs):
            if failure == "early_value" and value == incoming["backup"]:
                raise ValueError("ordinary preference serialization failed")
            return real_dumps(value, **kwargs)

        real_load = Store.async_load

        async def load(store: Store):
            if store.key == ALLOWED_PATHS_KEY:
                raise OSError("synthetic store unavailable")
            return await real_load(store)

        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(json, "dumps", dumps)
        monkeypatch.setattr(Store, "async_load", load)
        response = await client.post(
            _APP + "api/settings/transfer/import",
            cookies=admin_cookies,
            json=bundle,
            headers=headers,
        )
        assert response.status == 500
        assert await response.json() == {
            "success": False,
            "error": "rollback_incomplete",
        }
        actual = await hass.async_add_executor_job(
            server_transfer.export_server_settings, directory
        )
        assert actual == {**previous, failed_undo: incoming[failed_undo]}
        assert set(undone) == (
            {"backup", "features", "tools"}
            if failure == "published"
            else {"features", "tools"}
        )

    @pytest.mark.parametrize("origin", ["https://other.example", None])
    async def test_transfer_without_a_same_origin_signal_is_refused(
        self,
        hass: HomeAssistant,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
        caplog: pytest.LogCaptureFixture,
        origin: str | None,
    ) -> None:
        add_options_entry(hass, data={"webhook_id": "hook", "secret_path": "/path"})
        client = await hass_client_no_auth()
        response = await client.post(
            _APP + "api/settings/transfer/export",
            cookies=admin_cookies,
            json={},
            headers={} if origin is None else {"Origin": origin},
        )
        assert response.status == 403
        assert await response.json() == {"success": False, "error": "origin_denied"}
        assert "Refused a settings transfer request from another origin" in caplog.text

    async def test_unreadable_tools_store_names_the_setting(
        self,
        hass: HomeAssistant,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        add_options_entry(hass, data={"webhook_id": "hook", "secret_path": "/path"})
        await add_tools_entry(hass, loaded=True)
        real_load = Store.async_load

        async def load(store: Store):
            if store.key == ALLOWED_PATHS_KEY:
                raise OSError("synthetic store unavailable")
            return await real_load(store)

        monkeypatch.setattr(Store, "async_load", load)
        client = await hass_client_no_auth()
        response = await client.post(
            _APP + "api/settings/transfer/export",
            cookies=admin_cookies,
            json={},
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert response.status == 503
        assert await response.json() == {
            "success": False,
            "error": "allowed_paths_store_unavailable",
        }
        assert "Settings export failed (allowed_paths_store_unavailable)" in caplog.text

    @pytest.mark.parametrize(
        ("operation", "body", "status", "code"),
        [
            ("export", {"unexpected": True}, 400, "invalid_request"),
            (
                "import",
                {"format": "ha-mcp-settings", "version": 2, "new_section": {}},
                400,
                "incompatible_version",
            ),
            ("import", {"padding": "x" * (4 * 1024 * 1024)}, 413, "too_large"),
        ],
    )
    async def test_transfer_request_errors_have_stable_codes(
        self,
        hass: HomeAssistant,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
        operation: str,
        body: dict[str, object],
        status: int,
        code: str,
    ) -> None:
        add_options_entry(hass, data={"webhook_id": "hook", "secret_path": "/path"})
        client = await hass_client_no_auth()
        response = await client.post(
            _APP + f"api/settings/transfer/{operation}",
            cookies=admin_cookies,
            json=body,
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert response.status == status
        assert await response.json() == {"success": False, "error": code}

    async def test_unexpected_import_failure_never_echoes_the_file(
        self,
        hass: HomeAssistant,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        add_options_entry(hass, data={"webhook_id": "hook", "secret_path": "/path"})
        client = await hass_client_no_auth()
        headers = {"Sec-Fetch-Site": "same-origin"}
        exported = await client.post(
            _APP + "api/settings/transfer/export",
            cookies=admin_cookies,
            json={},
            headers=headers,
        )
        bundle = await exported.json()
        bundle["server_settings"]["tools"] = {"tools": {"uploaded_marker": "pinned"}}

        def fail(directory: Path, settings: dict[str, object]) -> None:
            raise PermissionError(json.dumps(settings))

        monkeypatch.setattr(server_transfer, "restore_server_settings", fail)
        response = await client.post(
            _APP + "api/settings/transfer/import",
            cookies=admin_cookies,
            json=bundle,
            headers=headers,
        )
        assert response.status == 500
        assert await response.json() == {"success": False, "error": "restore_failed"}
        assert "Settings import failed" in caplog.text
        assert "_MessageWithheld: PermissionError" in caplog.text
        assert "uploaded_marker" not in caplog.text

    async def test_failure_after_the_browser_disconnects_is_still_logged(
        self,
        hass: HomeAssistant,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        add_options_entry(hass, data={"webhook_id": "hook", "secret_path": "/path"})
        started = threading.Event()
        release = threading.Event()

        def hold(directory: Path) -> None:
            started.set()
            assert release.wait(10)
            raise PermissionError

        monkeypatch.setattr(server_transfer, "export_server_settings", hold)
        client = await hass_client_no_auth()
        request = asyncio.ensure_future(
            client.post(
                _APP + "api/settings/transfer/export",
                cookies=admin_cookies,
                json={},
                headers={"Sec-Fetch-Site": "same-origin"},
            )
        )
        assert await hass.async_add_executor_job(started.wait, 10)
        request.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await request
        # Let the server see the closed connection and cancel its handler.
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        await hass.async_block_till_done()
        assert "_MessageWithheld: PermissionError" in caplog.text
