"""The settings page inside the app, where the app's options are the settings.

Supervisor replaces an app's options in full on every write, so a save reads
the current options and writes them back with the change merged in; the GET
handlers show the saved options, not the values the app started with. A save
asks for a restart and never restarts the app itself. The handlers ask the
stand-in Supervisor of ``_tools_addons_core`` directly, as the app does.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings

from ._core_client import refused_url
from ._settings_ui_app import (
    APP_TOKEN,
    SELF_INFO,
    SELF_OPTIONS,
    app_server,
    ha_mcp_tasks,
    in_app,
    settings_http,
    with_env,
)
from ._settings_ui_app import settings_world as settings_world
from ._tools_addons_core import Supervisor, app_info
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

BACKUP_CONFIG = "/api/settings/backup-config"
FEATURES = "/api/settings/features"
ADVANCED = "/api/settings/advanced"
STORED = {
    "ha_tool_concurrency": 4,
    "enable_tool_search": False,
    "verify_ssl": True,
    "auto_backup_throttle_minutes": 0,
    "auto_backup_retain_per_entity": 100,
    "enable_auto_backup": True,
}


def _saved(
    monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor, options: dict[str, Any]
) -> None:
    """Inside the app, whose saved options are ``options``."""
    in_app(monkeypatch, supervisor)
    supervisor.answer(*SELF_INFO, app_info(options=options))


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor) -> Supervisor:
    """Inside the app, with ``STORED`` as its saved options."""
    _saved(monkeypatch, supervisor, STORED)
    return supervisor


async def _post(
    monkeypatch: pytest.MonkeyPatch,
    client: HomeAssistantClient,
    path: str,
    body: Any,
) -> httpx.Response:
    async with settings_http(app_server(monkeypatch, client)) as http:
        return await http.post(path, json=body)


async def _get(
    monkeypatch: pytest.MonkeyPatch, client: HomeAssistantClient, path: str
) -> dict[str, Any]:
    async with settings_http(app_server(monkeypatch, client)) as http:
        response = await http.get(path)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _written(app: Supervisor) -> dict[str, Any]:
    [body] = app.sent(*SELF_OPTIONS)
    options: dict[str, Any] = body["options"]
    return options


def _advanced_row(body: dict[str, Any], field: str) -> dict[str, Any]:
    row: dict[str, Any] = next(f for f in body["fields"] if f["field"] == field)
    return row


class TestBackupConfigSave:
    async def test_a_save_keeps_every_other_option_and_leaves_the_restart_to_the_user(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        """The page's Restart button is the one restart; a restart fired from
        the save would race the answer and show "Restart failed"."""
        app.answer(*SELF_OPTIONS)
        tasks_before = ha_mcp_tasks()

        response = await _post(
            monkeypatch,
            core_client,
            BACKUP_CONFIG,
            {"enable_auto_backup": False, "auto_backup_throttle_minutes": 5},
        )

        body = response.json()
        assert response.status_code == 200
        assert body["mode"] == "addon"
        assert body["restart_required"] is True
        assert "restarting" not in body
        assert _written(app) == {
            **STORED,
            "enable_auto_backup": False,
            "auto_backup_throttle_minutes": 5,
        }
        assert app.requests == [SELF_INFO, SELF_OPTIONS]
        assert ha_mcp_tasks() - tasks_before == set()

    async def test_supervisor_refusing_the_options_is_a_validation_failure(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        """Supervisor's own status and reason reach the page, not a generic 502."""
        app.refuse(*SELF_OPTIONS, "value must be at most 32 for ha_tool_concurrency")

        response = await _post(
            monkeypatch, core_client, BACKUP_CONFIG, {"enable_auto_backup": True}
        )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "CONFIG_VALIDATION_FAILED"
        assert "ha_tool_concurrency" in error["message"]

    async def test_a_dropped_options_write_is_a_connection_failure(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        app.drop(*SELF_OPTIONS)

        response = await _post(
            monkeypatch, core_client, BACKUP_CONFIG, {"enable_auto_backup": True}
        )

        assert response.status_code == 502
        assert response.json()["error"]["code"] == "CONNECTION_FAILED"

    async def test_supervisor_failing_the_options_read_writes_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: Supervisor,
        core_client: HomeAssistantClient,
    ) -> None:
        """No body was sent, so it is no schema problem: a connection failure."""
        in_app(monkeypatch, supervisor)
        supervisor.reply(*SELF_INFO, status=503, text="Supervisor busy")

        response = await _post(
            monkeypatch, core_client, BACKUP_CONFIG, {"enable_auto_backup": True}
        )

        assert response.status_code == 502
        error = response.json()["error"]
        assert error["code"] == "CONNECTION_FAILED"
        assert "503" in error["message"]
        assert supervisor.requests == [SELF_INFO]

    async def test_an_unreachable_supervisor_is_a_connection_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        socket_enabled: None,
        core_client: HomeAssistantClient,
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", APP_TOKEN)
        monkeypatch.setenv("SUPERVISOR_BASE_URL", refused_url())

        response = await _post(
            monkeypatch, core_client, BACKUP_CONFIG, {"enable_auto_backup": True}
        )

        assert response.status_code == 502
        assert response.json()["error"]["code"] == "CONNECTION_FAILED"


class TestFeatureFlagSave:
    async def test_a_save_merges_the_flag_and_leaves_the_restart_to_the_user(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        app.answer(*SELF_OPTIONS)
        tasks_before = ha_mcp_tasks()

        response = await _post(
            monkeypatch, core_client, FEATURES, {"flags": {"enable_tool_search": True}}
        )

        body = response.json()
        assert response.status_code == 200
        assert body["mode"] == "addon"
        assert body["restart_required"] is True
        assert _written(app) == {**STORED, "enable_tool_search": True}
        assert ha_mcp_tasks() - tasks_before == set()

    async def test_supervisor_refusing_the_flag_is_a_validation_failure(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        app.refuse(*SELF_OPTIONS, "unknown option")

        response = await _post(
            monkeypatch, core_client, FEATURES, {"flags": {"enable_tool_search": True}}
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "CONFIG_VALIDATION_FAILED"

    async def test_turning_beta_back_on_keeps_the_beta_features_saved_under_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: Supervisor,
        core_client: HomeAssistantClient,
    ) -> None:
        """Turning the master off kept the sub-flags; turning it on restores them."""
        stored = {
            "enable_beta_features": False,
            "enable_yaml_config_editing": True,
            "enable_filesystem_tools": True,
            "verify_ssl": True,
        }
        _saved(monkeypatch, supervisor, stored)
        supervisor.answer(*SELF_OPTIONS)
        # The dev app writes the master's variable from its options.
        with_env(monkeypatch, ENABLE_BETA_FEATURES="false")

        response = await _post(
            monkeypatch,
            core_client,
            FEATURES,
            {"flags": {"enable_beta_features": True}},
        )

        assert response.status_code == 200, response.json()
        assert _written(supervisor) == {**stored, "enable_beta_features": True}

    async def test_the_sidecar_with_an_inherited_token_cannot_save_to_supervisor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stdio sidecar inherits its parent's environment but has no server."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", APP_TOKEN)

        async with settings_http(is_sidecar=True) as http:
            response = await http.post(
                FEATURES, json={"flags": {"enable_tool_search": True}}
            )

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"


class TestAdvancedSave:
    async def test_an_app_synced_field_is_saved_to_supervisor(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        """The page promises the app's Configuration tab and itself stay in step."""
        app.answer(*SELF_OPTIONS)
        with_env(monkeypatch, HA_TOOL_CONCURRENCY="0", HA_VERIFY_SSL="true")

        response = await _post(
            monkeypatch, core_client, ADVANCED, {"ha_tool_concurrency": 8}
        )

        body = response.json()
        assert response.status_code == 200, body
        assert body["mode"] == "addon"
        assert body["applied"] == {"ha_tool_concurrency": 8}
        assert body["restart_required"] is True
        assert _written(app) == {**STORED, "ha_tool_concurrency": 8}

    async def test_supervisor_refusing_the_field_is_a_validation_failure(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        app.refuse(*SELF_OPTIONS, "bad value")
        with_env(monkeypatch, HA_TOOL_CONCURRENCY="0")

        response = await _post(
            monkeypatch, core_client, ADVANCED, {"ha_tool_concurrency": 8}
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "CONFIG_VALIDATION_FAILED"


class TestSavedOptionsShown:
    """Editing the app's Configuration tab or the page without a restart must
    not leave the page showing the values the app started with."""

    async def test_a_flag_shows_its_saved_option(
        self,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: Supervisor,
        core_client: HomeAssistantClient,
    ) -> None:
        _saved(monkeypatch, supervisor, {"enable_tool_search": True})
        with_env(monkeypatch, ENABLE_TOOL_SEARCH="false")

        row = (await _get(monkeypatch, core_client, FEATURES))["flags"][
            "enable_tool_search"
        ]

        assert row["value"] is True
        assert row["origin"] == "addon"

    async def test_an_app_synced_advanced_field_shows_its_saved_option(
        self,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: Supervisor,
        core_client: HomeAssistantClient,
    ) -> None:
        _saved(monkeypatch, supervisor, {"ha_tool_concurrency": 4, "verify_ssl": False})
        with_env(monkeypatch, HA_TOOL_CONCURRENCY="8", HA_VERIFY_SSL="true")

        body = await _get(monkeypatch, core_client, ADVANCED)

        concurrency = _advanced_row(body, "ha_tool_concurrency")
        assert concurrency["value"] == 4
        assert concurrency["origin"] == "addon"
        assert concurrency["editable"] is True
        assert _advanced_row(body, "verify_ssl")["value"] is False

    async def test_an_advanced_field_shows_its_start_value_when_supervisor_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: Supervisor,
        core_client: HomeAssistantClient,
    ) -> None:
        in_app(monkeypatch, supervisor)
        supervisor.reply(*SELF_INFO, status=503, text="Supervisor busy")
        with_env(monkeypatch, HA_TOOL_CONCURRENCY="8")

        body = await _get(monkeypatch, core_client, ADVANCED)

        assert _advanced_row(body, "ha_tool_concurrency")["value"] == 8

    async def test_a_flag_shows_its_start_value_when_supervisor_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: Supervisor,
        core_client: HomeAssistantClient,
    ) -> None:
        in_app(monkeypatch, supervisor)
        supervisor.reply(*SELF_INFO, status=503, text="Supervisor busy")
        with_env(monkeypatch, ENABLE_TOOL_SEARCH="false")

        row = (await _get(monkeypatch, core_client, FEATURES))["flags"][
            "enable_tool_search"
        ]

        assert row["value"] is False
        assert row["origin"] == "addon"

    async def test_an_advanced_field_supervisor_does_not_manage_ignores_the_options(
        self,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: Supervisor,
        core_client: HomeAssistantClient,
    ) -> None:
        _saved(monkeypatch, supervisor, {"fuzzy_threshold": 99})

        row = _advanced_row(
            await _get(monkeypatch, core_client, ADVANCED), "fuzzy_threshold"
        )

        assert row["origin"] == "default"
        assert row["value"] == get_global_settings().fuzzy_threshold != 99

    async def test_a_flag_saved_before_the_restart_shows_the_saved_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: Supervisor,
        core_client: HomeAssistantClient,
    ) -> None:
        """Codex P2: saved in the Configuration tab but not restarted yet, the
        master has no variable at this boot, so its origin is the default."""
        _saved(monkeypatch, supervisor, {"enable_beta_features": True})

        row = (await _get(monkeypatch, core_client, FEATURES))["flags"][
            "enable_beta_features"
        ]

        assert row["origin"] == "default"
        assert row["value"] is True

    async def test_outside_the_app_supervisor_is_not_asked(
        self,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: Supervisor,
        core_client: HomeAssistantClient,
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_BASE_URL", f"http://{supervisor.url}")

        await _get(monkeypatch, core_client, FEATURES)

        assert supervisor.requests == []
