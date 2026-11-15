"""Settings shows what a restore reports: its warnings, and why a write failed.

The failed writes run through the Settings restore route on a real server over
``core_client``, and its reply goes to the page's script.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient

from ._backup_core import backup_settings as backup_settings
from ._backup_core import file_snapshot_manager as file_snapshot_manager
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_restore_helpers import run_restore
from ._backup_target_identity_core import (
    older_yaml_snapshot,
    rejected_script_snapshot,
    settings_restore,
)


def test_settings_shows_the_restore_warnings(settings_script: str) -> None:
    """Settings showed only the safety backup's name, never why it is missing."""
    reply = {
        "status": 200,
        "json": {
            "success": True,
            "data": {
                "safety_backup": None,
                "warnings": ["No safety backup was taken: nothing was found"],
                "result": {"warnings": ["The zone keeps its stored icon"]},
            },
        },
    }

    rendered = run_restore(settings_script, reply)

    assert "nothing was found" in rendered.alerts[0]
    assert "keeps its stored icon" in rendered.alerts[0]


async def test_settings_reports_a_failed_write_with_its_warnings(
    settings_script: str,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The route answered a failed write with a plain-text HTTP 500. The reply
    carries only the error's message, so the traceback is logged."""
    name = rejected_script_snapshot(snapshot_manager.backup_dir)

    with caplog.at_level(logging.ERROR):
        status, body = await settings_restore(core_client, name)
    rendered = run_restore(settings_script, {"status": status, "json": body}, name)

    assert status == 409
    assert body["data"]["apply_status"] == "unknown"
    assert "nothing was found under hall_lights" in rendered.alerts[0]
    logged = [r.exc_info[1] for r in caplog.records if r.exc_info]
    assert len(logged) == 1
    assert isinstance(logged[0], HomeAssistantAPIError)
    assert logged[0].status_code == 400


async def test_settings_keeps_a_structured_tool_error(
    hass: HomeAssistant,
    settings_script: str,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ToolError already says what failed and how to fix it; re-classified,
    Settings showed "An unexpected error occurred"."""
    name = await older_yaml_snapshot(
        hass, file_snapshot_manager, backup_settings, monkeypatch
    )

    status, body = await settings_restore(core_client, name)
    rendered = run_restore(settings_script, {"status": status, "json": body}, name)

    assert status == 409
    assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    assert body["data"]["apply_status"] == "unknown"
    assert "requires >= 1.2.4" in " ".join(rendered.alerts)


def test_settings_names_a_recreated_helper_s_mapping_warning_once(
    settings_script: str,
) -> None:
    """Its outcome message already renders the result's warning."""
    note = "This snapshot has no entity mapping; the recreated helper may have a new entity ID."
    outcome = {
        "restore_mode": "recreated",
        "entity_id": "new-entry",
        "apply_status": "applied",
        "verification_status": "matched",
        "safety_backup": None,
        "result": {
            "entry_id": "new-entry",
            "entity_ids_restored": False,
            "warnings": [note],
        },
    }

    rendered = run_restore(
        settings_script, {"status": 200, "json": {"success": True, "data": outcome}}
    )

    assert rendered.alerts[0].count("may have a new entity ID") == 1


@pytest.mark.parametrize(
    "outcome",
    [
        # A flow helper's recreation carries restore_mode at the top level and
        # today returns no other warning; this pins that one would show.
        {
            "restore_mode": "recreated",
            "apply_status": "applied",
            "result": {"entry_id": "new-entry", "entity_ids_restored": True},
        },
        # A zone's recreation keeps restore_mode inside the result, as sent.
        {"result": {"id": "hall", "restore_mode": "recreated"}},
    ],
    ids=["flow-helper-with-mapping", "zone"],
)
def test_settings_keeps_a_recreated_item_s_own_warnings(
    settings_script: str, outcome: dict[str, Any]
) -> None:
    """Only the no-mapping warning is rendered by the outcome message."""
    outcome = {
        **outcome,
        "safety_backup": None,
        "result": {**outcome["result"], "warnings": ["Created with a warning"]},
    }

    rendered = run_restore(
        settings_script, {"status": 200, "json": {"success": True, "data": outcome}}
    )

    assert "Created with a warning" in rendered.alerts[0]
