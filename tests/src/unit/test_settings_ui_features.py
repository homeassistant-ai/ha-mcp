"""Feature flags outside the app, and the page's restart probe.

``POST /api/settings/features`` saves into ``feature_flags.json``. A beta
feature needs the beta master switch, and strict best-practices mode needs
its parent switch: the save refuses a batch that would leave either on without
its parent, and turning a parent off keeps what was saved under it.
``GET /api/settings/info`` tells the restart cycle which process answered.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from ha_mcp.config import (
    BETA_FEATURE_FIELDS,
    get_global_settings,
    reset_global_settings,
)

from ._settings_ui_app import app_server, settings_http
from ._settings_ui_app import settings_world as settings_world

FEATURES = "/api/settings/features"


async def _save(flags: Any) -> httpx.Response:
    async with settings_http() as http:
        return await http.post(FEATURES, json={"flags": flags})


def _stored(tmp_path: Path, **flags: bool) -> None:
    (tmp_path / "feature_flags.json").write_text(json.dumps(flags))
    reset_global_settings()


def _on_disk(tmp_path: Path) -> dict[str, Any]:
    saved: dict[str, Any] = json.loads((tmp_path / "feature_flags.json").read_text())
    return saved


class TestSave:
    async def test_the_save_answers_like_every_other_save(self) -> None:
        """The page shows its restart banner from ``restart_required``."""
        response = await _save({"enable_tool_search": True})

        body = response.json()
        assert response.status_code == 200
        assert body["success"] is True
        assert body["mode"] == "file"
        assert body["restart_required"] is True
        assert body["applied"] == {"enable_tool_search": True}
        assert "restarting" not in body

    async def test_read_only_mode_turned_on_is_read_back_on(self) -> None:
        """#1569: the Tools tab switch reaches the setting the middleware reads."""
        assert get_global_settings().read_only_mode is False

        response = await _save({"read_only_mode": True})

        assert response.status_code == 200, response.json()
        reset_global_settings()
        assert get_global_settings().read_only_mode is True

    @pytest.mark.parametrize(
        "body",
        [
            {"enable_lite_docstrings": True},
            {"flags": {}},
            {"flags": ["enable_lite_docstrings"]},
        ],
        ids=["no-flags-wrapper", "empty-flags", "flags-not-an-object"],
    )
    async def test_a_body_that_sets_no_flag_is_a_400_and_saves_nothing(
        self, body: dict[str, Any], tmp_path: Path
    ) -> None:
        """#1840: a 200 here told the caller a flag changed and a restart was due."""
        async with settings_http() as http:
            response = await http.post(FEATURES, json=body)

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert not (tmp_path / "feature_flags.json").exists()

    async def test_a_body_without_the_flags_wrapper_says_how_to_send_it(self) -> None:
        async with settings_http() as http:
            response = await http.post(FEATURES, json={"enable_lite_docstrings": True})

        assert response.json()["error"]["suggestion"]


class TestBetaMasterSwitch:
    async def test_a_beta_feature_needs_the_master_switch(self) -> None:
        response = await _save({"enable_yaml_config_editing": True})

        assert response.status_code == 409
        assert "enable_yaml_config_editing" in response.text

    async def test_the_master_and_a_beta_feature_can_be_turned_on_together(
        self,
    ) -> None:
        response = await _save(
            {"enable_beta_features": True, "enable_yaml_config_editing": True}
        )

        assert response.status_code == 200

    async def test_a_beta_feature_saves_under_a_saved_master(
        self, tmp_path: Path
    ) -> None:
        _stored(tmp_path, enable_beta_features=True)

        response = await _save({"enable_filesystem_tools": True})

        assert response.status_code == 200

    async def test_a_batch_turning_the_master_off_cannot_turn_a_beta_feature_on(
        self, tmp_path: Path
    ) -> None:
        """The master is judged after the batch: off. Nothing is written."""
        _stored(
            tmp_path,
            enable_beta_features=True,
            enable_yaml_config_editing=False,
            enable_filesystem_tools=True,
        )

        response = await _save(
            {"enable_beta_features": False, "enable_yaml_config_editing": True}
        )

        assert response.status_code == 409, response.json()
        assert _on_disk(tmp_path)["enable_yaml_config_editing"] is False

    async def test_turning_the_master_off_changes_nothing_else(
        self, tmp_path: Path
    ) -> None:
        """Turning it back on later restores every beta feature as it was."""
        _stored(
            tmp_path,
            enable_beta_features=True,
            **dict.fromkeys(BETA_FEATURE_FIELDS, True),
        )

        response = await _save({"enable_beta_features": False})

        assert response.status_code == 200, response.json()
        assert response.json()["applied"] == {"enable_beta_features": False}
        assert _on_disk(tmp_path) == {
            "enable_beta_features": False,
            **dict.fromkeys(BETA_FEATURE_FIELDS, True),
        }

    async def test_turning_the_master_on_brings_the_saved_features_back(
        self, tmp_path: Path
    ) -> None:
        _stored(
            tmp_path,
            enable_beta_features=False,
            enable_yaml_config_editing=True,
            enable_filesystem_tools=True,
        )
        assert get_global_settings().enable_yaml_config_editing is False

        response = await _save({"enable_beta_features": True})

        assert response.status_code == 200, response.json()
        reset_global_settings()
        settings = get_global_settings()
        assert settings.enable_yaml_config_editing is True
        assert settings.enable_filesystem_tools is True


class TestStrictModeParent:
    """#1779: strict best-practices mode does nothing unless its parent is on."""

    async def test_a_batch_turning_the_parent_off_cannot_turn_strict_mode_on(
        self,
    ) -> None:
        response = await _save(
            {"enable_mandatory_bps": False, "enable_strict_mandatory_bps": True}
        )

        assert response.status_code == 409
        assert "enable_strict_mandatory_bps" in response.text

    async def test_strict_mode_needs_a_parent_that_is_on(self, tmp_path: Path) -> None:
        _stored(tmp_path, enable_mandatory_bps=False)

        response = await _save({"enable_strict_mandatory_bps": True})

        assert response.status_code == 409

    async def test_strict_mode_saves_under_the_default_parent(self) -> None:
        response = await _save({"enable_strict_mandatory_bps": True})

        assert response.status_code == 200

    async def test_the_parent_and_strict_mode_can_be_turned_on_together(
        self, tmp_path: Path
    ) -> None:
        _stored(tmp_path, enable_mandatory_bps=False)

        response = await _save(
            {"enable_mandatory_bps": True, "enable_strict_mandatory_bps": True}
        )

        assert response.status_code == 200

    async def test_turning_the_parent_off_keeps_strict_mode_saved(
        self, tmp_path: Path
    ) -> None:
        _stored(tmp_path, enable_mandatory_bps=True, enable_strict_mandatory_bps=True)

        response = await _save({"enable_mandatory_bps": False})

        assert response.status_code == 200, response.json()
        assert _on_disk(tmp_path) == {
            "enable_mandatory_bps": False,
            "enable_strict_mandatory_bps": True,
        }


class TestRestartProbe:
    """The restart cycle reloads the page once the answering process or server
    differs; a polled value that never flips times the cycle out, and one that
    flips on every poll reloads at once."""

    async def _info(self, server: Any) -> dict[str, Any]:
        async with settings_http(server) as http:
            response = await http.get("/api/settings/info")
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return body

    async def test_the_process_is_named_with_its_start_time(self) -> None:
        body = await self._info(app_server())

        assert isinstance(body["instance_id"], str)
        assert body["instance_id"]
        assert 0 < body["started_at"] <= time.time()
        assert "is_addon" in body
        assert "is_sidecar" in body

    async def test_the_process_answers_the_same_on_every_poll(self) -> None:
        server = app_server()

        first, second = await self._info(server), await self._info(server)

        assert first["instance_id"] == second["instance_id"]
        assert first["started_at"] == second["started_at"]
        assert first["worker_id"] == second["worker_id"]
        assert first["worker_id"]

    async def test_a_new_server_in_the_same_process_answers_a_new_worker(
        self,
    ) -> None:
        """#2279: the in-process server restarts as a new server object inside
        Home Assistant's process, whose id stays the same."""
        first = await self._info(app_server())

        second = await self._info(app_server())

        assert first["worker_id"] != second["worker_id"]
        assert first["instance_id"] == second["instance_id"]
