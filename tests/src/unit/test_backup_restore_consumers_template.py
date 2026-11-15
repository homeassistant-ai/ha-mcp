"""Restoring a Template helper reports the same outcome through every consumer.

The helper is made by Core's own config flow and restored through its options
flow, on a real ``hass``. Settings answers the outcome as JSON, the MCP tool
as a structured error, and the page's script words it for the user.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError

from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_restore_consumers_core import (
    SavedTemplate,
    fill_the_disk,
    restore_through,
    run_restore,
    watch_options_flow,
)
from ._backup_restore_consumers_core import saved_template as saved_template
from ._backup_restore_consumers_core import unchanged_template as unchanged_template
from ._template_helper_core import state_template


@pytest.mark.parametrize("consumer", ["settings", "mcp", "script"])
async def test_a_restore_refused_for_a_changed_name_says_which_field_and_changes_nothing(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    settings_script: str,
    saved_template: SavedTemplate,
    consumer: str,
) -> None:
    """The options form does not offer the name, so a name changed since the
    snapshot cannot be put back: the user is told so, not that it worked."""
    entry = saved_template.entry
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "name": "secret-marker-changed"}
    )
    with watch_options_flow(core_client) as flow:
        reply = await restore_through(
            "settings" if consumer == "script" else consumer,
            snapshot_manager,
            core_client,
            monkeypatch,
            saved_template.snapshot.name,
        )

    payload = reply.payload
    assert payload["success"] is False
    assert payload["data"]["apply_status"] == "not_applied"
    assert payload["data"]["verification_status"] == "not_run"
    assert payload["data"]["reason"] == "identity_changed"
    assert payload["data"]["fields"] == ["name"]
    assert "name" in payload["error"]["message"]
    assert "secret-marker" not in json.dumps(payload)
    assert entry.options["state"] == state_template("edited")
    flow.start.assert_awaited_once()
    flow.submit.assert_not_awaited()
    flow.abort.assert_awaited_once()
    if consumer == "script":
        assert reply.status is not None
        rendered = run_restore(
            settings_script,
            {"status": reply.status, "json": payload},
            saved_template.snapshot.name,
        )
        assert "Nothing was changed" in rendered.alerts[0]
        assert "not offered by any restore form" in rendered.alerts[0]
        assert "secret-marker" not in rendered.alerts[0]


@pytest.mark.parametrize("consumer", ["settings", "mcp", "script"])
async def test_a_restore_that_cannot_take_its_safety_snapshot_starts_no_flow(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    settings_script: str,
    saved_template: SavedTemplate,
    consumer: str,
) -> None:
    """A helper restore can apply partly, so without its recovery point it is
    not started: nothing is changed and the reply says so."""
    fill_the_disk(snapshot_manager, monkeypatch)
    with watch_options_flow(core_client) as flow:
        reply = await restore_through(
            "settings" if consumer == "script" else consumer,
            snapshot_manager,
            core_client,
            monkeypatch,
            saved_template.snapshot.name,
        )

    payload = reply.payload
    if reply.status is not None:
        assert reply.status == 409
    assert payload["error"]["code"] == "BACKUP_CAPTURE_FAILED"
    assert payload["data"]["apply_status"] == "not_applied"
    assert payload["data"]["verification_status"] == "not_run"
    assert saved_template.entry.options["state"] == state_template("edited")
    flow.start.assert_not_awaited()
    if consumer == "script":
        assert reply.status is not None
        rendered = run_restore(
            settings_script,
            {"status": reply.status, "json": payload},
            saved_template.snapshot.name,
        )
        assert "Nothing was changed" in rendered.alerts[0]


@pytest.mark.parametrize("consumer", ["settings", "mcp", "script"])
async def test_a_restore_whose_submit_gets_no_answer_names_the_safety_snapshot(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    settings_script: str,
    saved_template: SavedTemplate,
    consumer: str,
) -> None:
    """When the restore may or may not have landed, the user is told so and is
    given the snapshot of what was there before; nothing says it succeeded."""
    monkeypatch.setattr(
        core_client,
        "submit_options_flow_step",
        AsyncMock(side_effect=HomeAssistantConnectionError("secret-marker lost")),
    )
    reply = await restore_through(
        "settings" if consumer == "script" else consumer,
        snapshot_manager,
        core_client,
        monkeypatch,
        saved_template.snapshot.name,
    )

    payload = reply.payload
    assert payload["success"] is False
    assert payload["data"]["apply_status"] == "unknown"
    assert "secret-marker" not in json.dumps(payload)
    safety = payload["data"]["safety_backup"]
    assert safety in {row["name"] for row in snapshot_manager.list_snapshots()}
    if consumer == "script":
        assert reply.status is not None
        rendered = run_restore(
            settings_script,
            {"status": reply.status, "json": payload},
            saved_template.snapshot.name,
        )
        assert safety in rendered.alerts[0]
        assert "Nothing was changed" not in rendered.alerts[0]
        assert len(rendered.fetches_to("/backups?")) == 1
