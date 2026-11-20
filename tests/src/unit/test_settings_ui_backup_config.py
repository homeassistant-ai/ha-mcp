"""Auto-backup settings outside the app: ``backup_settings.json`` (#1288).

The Backups tab saves into the override file and the next settings read
applies it, unless an environment variable sets the field; inside the app the
file is ignored (the app's options are the settings there).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ha_mcp.config import (
    get_backup_setting_origin,
    get_global_settings,
)

from ._settings_ui_app import settings_http, with_env
from ._settings_ui_app import settings_world as settings_world

BACKUP_CONFIG = "/api/settings/backup-config"


async def _save(body: Any) -> httpx.Response:
    async with settings_http() as http:
        return await http.post(BACKUP_CONFIG, json=body)


def _override(tmp_path: Path) -> Path:
    return tmp_path / "backup_settings.json"


class TestOrigin:
    """The page labels each field with where its value comes from, and locks
    the ones an environment variable sets."""

    def test_inside_the_app_every_field_comes_from_the_app(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """start.py writes the variables from the app's options."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "app-token")
        monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")

        assert get_backup_setting_origin("ENABLE_AUTO_BACKUP") == "addon"

    def test_a_set_variable_is_the_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUTO_BACKUP_THROTTLE_MINUTES", "15")

        assert get_backup_setting_origin("AUTO_BACKUP_THROTTLE_MINUTES") == "env"

    def test_a_saved_field_comes_from_the_file(self, tmp_path: Path) -> None:
        _override(tmp_path).write_text(json.dumps({"enable_auto_backup": True}))

        assert get_backup_setting_origin("ENABLE_AUTO_BACKUP") == "file"

    def test_a_field_set_nowhere_is_the_default(self) -> None:
        assert get_backup_setting_origin("AUTO_BACKUP_RETAIN_PER_ENTITY") == "default"

    def test_a_variable_that_is_no_backup_field_is_the_default(self) -> None:
        assert get_backup_setting_origin("NOT_A_REAL_ENV_VAR") == "default"


class TestFileApplied:
    def test_saved_fields_apply_without_variables(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _override(tmp_path).write_text(
            json.dumps(
                {
                    "enable_auto_backup": True,
                    "auto_backup_throttle_minutes": 7,
                    "auto_backup_retain_per_entity": 33,
                }
            )
        )

        settings = with_env(monkeypatch)

        assert settings.enable_auto_backup is True
        assert settings.auto_backup_throttle_minutes == 7
        assert settings.auto_backup_retain_per_entity == 33

    def test_a_variable_wins_over_the_file_for_its_field_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _override(tmp_path).write_text(
            json.dumps({"enable_auto_backup": True, "auto_backup_throttle_minutes": 42})
        )

        settings = with_env(monkeypatch, ENABLE_AUTO_BACKUP="false")

        assert settings.enable_auto_backup is False
        assert settings.auto_backup_throttle_minutes == 42

    def test_inside_the_app_the_file_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _override(tmp_path).write_text(json.dumps({"enable_auto_backup": True}))

        settings = with_env(
            monkeypatch, SUPERVISOR_TOKEN="app-token", ENABLE_AUTO_BACKUP="false"
        )

        assert settings.enable_auto_backup is False

    def test_an_out_of_range_saved_value_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        defaults = with_env(monkeypatch)
        _override(tmp_path).write_text(
            json.dumps(
                {
                    "auto_backup_throttle_minutes": 9999,
                    "auto_backup_retain_per_entity": 999_999,
                }
            )
        )

        settings = with_env(monkeypatch)

        assert (
            settings.auto_backup_throttle_minutes
            == defaults.auto_backup_throttle_minutes
        )
        assert (
            settings.auto_backup_retain_per_entity
            == defaults.auto_backup_retain_per_entity
        )


class TestSave:
    async def test_a_save_writes_the_file_and_applies_at_once(
        self, tmp_path: Path
    ) -> None:
        """No restart: the next settings read sees the saved values."""
        get_global_settings()

        response = await _save(
            {"enable_auto_backup": True, "auto_backup_throttle_minutes": 9}
        )

        body = response.json()
        assert response.status_code == 200
        assert body["mode"] == "file"
        assert body["restart_required"] is False
        assert "restarting" not in body
        assert json.loads(_override(tmp_path).read_text()) == {
            "enable_auto_backup": True,
            "auto_backup_throttle_minutes": 9,
        }
        settings = get_global_settings()
        assert settings.enable_auto_backup is True
        assert settings.auto_backup_throttle_minutes == 9

    async def test_a_second_save_keeps_the_first_saves_fields(
        self, tmp_path: Path
    ) -> None:
        await _save({"enable_auto_backup": True})

        await _save({"auto_backup_throttle_minutes": 5})

        assert json.loads(_override(tmp_path).read_text()) == {
            "enable_auto_backup": True,
            "auto_backup_throttle_minutes": 5,
        }

    @pytest.mark.parametrize(
        "stored", ["not valid json {{{", "[1, 2, 3]"], ids=["corrupt", "not-an-object"]
    )
    async def test_a_file_that_holds_no_settings_is_replaced(
        self, tmp_path: Path, stored: str
    ) -> None:
        _override(tmp_path).write_text(stored)

        response = await _save({"enable_auto_backup": True})

        assert response.status_code == 200
        assert json.loads(_override(tmp_path).read_text()) == {
            "enable_auto_backup": True
        }

    async def test_a_body_that_is_not_an_object_is_a_400(self) -> None:
        assert (await _save([1, 2, 3])).status_code == 400

    async def test_a_throttle_out_of_range_is_a_400_naming_the_range(self) -> None:
        response = await _save({"auto_backup_throttle_minutes": 9999})

        assert response.status_code == 400
        assert "0..1440" in response.text

    async def test_a_retention_out_of_range_is_a_400(self) -> None:
        assert (await _save({"auto_backup_retain_per_entity": 0})).status_code == 400

    async def test_a_body_without_backup_fields_is_a_400(self) -> None:
        assert (await _save({"unrelated_key": True})).status_code == 400

    async def test_a_field_a_variable_sets_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The variable would win at the next read, so the save would be lost."""
        monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")

        response = await _save({"enable_auto_backup": False})

        assert response.status_code == 409
        rejected = response.json()["error"]["rejected"]
        assert [row["env_var"] for row in rejected] == ["ENABLE_AUTO_BACKUP"]
        assert not _override(tmp_path).exists()
