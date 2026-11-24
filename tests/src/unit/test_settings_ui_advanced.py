"""The Server Settings tab outside the app: ``/api/settings/advanced``.

Every registered setting is listed with its value, where it comes from and
whether the page may change it; a save is validated in full and merged into
``feature_flags.json``, which the feature flags share.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from ha_mcp.config import (
    ADVANCED_SETTINGS_FIELDS,
    get_global_settings,
    reset_global_settings,
)
from ha_mcp.utils import config_write_lock

from ._settings_ui_app import settings_http, with_env
from ._settings_ui_app import settings_world as settings_world

ADVANCED = "/api/settings/advanced"


async def _fields() -> dict[str, dict[str, Any]]:
    async with settings_http() as http:
        response = await http.get(ADVANCED)
    assert response.status_code == 200
    return {row["field"]: row for row in response.json()["fields"]}


async def _save(body: Any) -> httpx.Response:
    async with settings_http() as http:
        return await http.post(ADVANCED, json=body)


@pytest.fixture(autouse=True)
def _no_advanced_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for _field, env_name, *_rest in ADVANCED_SETTINGS_FIELDS:
        monkeypatch.delenv(env_name, raising=False)
    reset_global_settings()


class TestFields:
    async def test_every_registered_setting_is_listed(self) -> None:
        fields = await _fields()

        assert set(fields) == {field for field, *_rest in ADVANCED_SETTINGS_FIELDS}

    async def test_the_connection_is_shown_but_not_editable(self) -> None:
        row = (await _fields())["homeassistant_url"]

        assert row["editable"] is False
        assert row["section"] == "connection"

    async def test_the_log_level_offers_the_logging_levels(self) -> None:
        row = (await _fields())["log_level"]

        assert row["choices"] == ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    async def test_the_token_is_masked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with_env(monkeypatch, HOMEASSISTANT_TOKEN="real-secret-jwt-value")

        async with settings_http() as http:
            response = await http.get(ADVANCED)

        row = next(
            f for f in response.json()["fields"] if f["field"] == "homeassistant_token"
        )
        assert row["value"] == "*****"
        assert "real-secret-jwt-value" not in response.text

    async def test_a_field_a_variable_sets_is_locked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The page shows which variable to unset."""
        with_env(monkeypatch, HA_TIMEOUT="60")

        row = (await _fields())["timeout"]

        assert row["origin"] == "env"
        assert row["editable"] is False

    async def test_inside_the_app_its_synced_fields_are_editable_and_others_locked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The app writes every option to a variable at start; an app-synced
        field is still saved through Supervisor, any other set field is locked."""
        with_env(
            monkeypatch,
            SUPERVISOR_TOKEN="app-token",
            HA_TOOL_CONCURRENCY="0",
            HA_VERIFY_SSL="true",
            HA_TIMEOUT="60",
        )

        fields = await _fields()

        for name in ("ha_tool_concurrency", "verify_ssl"):
            assert fields[name]["origin"] == "addon"
            assert fields[name]["editable"] is True
        assert fields["timeout"]["origin"] == "env"
        assert fields["timeout"]["editable"] is False


class TestSaveRefused:
    async def test_an_unknown_field_is_a_400_naming_it(self) -> None:
        response = await _save({"made_up_field": 42})

        assert response.status_code == 400
        assert "made_up_field" in response.text

    async def test_the_connection_cannot_be_changed(self) -> None:
        assert (
            await _save({"homeassistant_url": "http://attacker"})
        ).status_code == 409

    async def test_a_field_a_variable_sets_is_refused_naming_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_env(monkeypatch, HA_TIMEOUT="60")

        response = await _save({"timeout": 90})

        assert response.status_code == 409
        assert "HA_TIMEOUT" in response.text

    async def test_a_value_out_of_bounds_is_a_400_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        response = await _save({"timeout": -5})

        assert response.status_code == 400
        assert not (tmp_path / "feature_flags.json").exists()

    async def test_a_value_that_is_no_choice_is_a_400(self) -> None:
        assert (await _save({"log_level": "BANANAS"})).status_code == 400

    async def test_a_null_byte_in_text_is_a_400(self) -> None:
        assert (await _save({"mcp_server_name": "evil\x00name"})).status_code == 400

    async def test_a_corrupt_settings_file_is_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        """It may still hold toggles saved before it broke."""
        (tmp_path / "feature_flags.json").write_text("{not valid json")

        response = await _save({"timeout": 90})

        assert response.status_code == 409
        assert (tmp_path / "feature_flags.json").read_text() == "{not valid json"


class TestSave:
    async def test_advanced_file_write_holds_the_cross_loop_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        active = False
        real_replace = os.replace

        @asynccontextmanager
        async def guard():
            nonlocal active
            active = True
            try:
                yield
            finally:
                active = False

        def checked_replace(*args, **kwargs):
            assert active
            return real_replace(*args, **kwargs)

        monkeypatch.setattr(config_write_lock, "config_write_guard", guard)
        monkeypatch.setattr(os, "replace", checked_replace)

        assert (await _save({"timeout": 90})).status_code == 200

    async def test_a_saved_value_is_read_back_after_the_restart(self) -> None:
        response = await _save({"timeout": 90})

        body = response.json()
        assert response.status_code == 200
        assert body["success"] is True
        assert body["applied"] == {"timeout": 90}
        assert body["restart_required"] is True
        reset_global_settings()
        assert get_global_settings().timeout == 90

    async def test_the_sidecar_port_takes_off_or_a_port_above_1023(self) -> None:
        """0 turns the pinned port off although it is under the port range."""
        for port in (0, 8099):
            response = await _save({"sidecar_pin_port": port})
            assert response.status_code == 200
            reset_global_settings()
            assert get_global_settings().sidecar_pin_port == port

        assert (await _save({"sidecar_pin_port": 80})).status_code == 400

    async def test_a_save_keeps_the_feature_flags_in_the_shared_file(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "feature_flags.json").write_text(
            json.dumps({"enable_tool_search": True})
        )

        response = await _save({"timeout": 90})

        assert response.status_code == 200
        assert json.loads((tmp_path / "feature_flags.json").read_text()) == {
            "enable_tool_search": True,
            "timeout": 90,
        }
