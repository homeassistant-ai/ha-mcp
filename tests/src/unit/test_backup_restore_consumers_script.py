"""The page's script words a restore that did not finish cleanly for the user.

Its replies come from the Settings route on a real ``hass`` wherever Home
Assistant is involved. The two replies no route sends (a response that never
arrived, a success flag that is false) are the browser's own, so they are
written out.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient

from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_restore_consumers_core import (
    SavedTemplate,
    removed_template,
    restore_fails_with,
    restore_through,
    run_restore,
)
from ._backup_restore_consumers_core import saved_template as saved_template
from ._backup_restore_consumers_core import unchanged_template as unchanged_template


@pytest.mark.parametrize(
    "reply",
    [
        {"status": 500, "body": "Internal Server Error"},
        {"throw": "connection lost after POST"},
    ],
    ids=["server-error", "connection-lost"],
)
def test_a_restore_with_no_confirmed_reply_is_not_retried_blindly(
    settings_script: str, reply: dict[str, Any]
) -> None:
    """The restore may have landed: the page says so, and asks the user to
    check before retrying."""
    result = run_restore(settings_script, reply)
    assert "could not be confirmed" in result.dom
    assert "before retrying" in result.dom
    assert len(result.fetches_to("/backups?")) == 1


def test_a_reply_that_says_it_failed_is_not_reported_as_restored(
    settings_script: str,
) -> None:
    result = run_restore(
        settings_script,
        {
            "status": 200,
            "json": {
                "success": False,
                "error": {"message": "Restore refused"},
                "data": {
                    "apply_status": "not_applied",
                    "verification_status": "not_run",
                },
            },
        },
    )
    assert "Nothing was changed" in result.alerts[0]
    assert "Restored." not in result.alerts[0]


@pytest.mark.parametrize(
    "reason", ["entity_id_collision", "entity_rename_outcome_unknown"]
)
async def test_a_recreation_that_stopped_half_way_names_the_new_helper(
    snapshot_manager: bm.BackupManager,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    settings_script: str,
    saved_template: SavedTemplate,
    reason: str,
) -> None:
    """A helper created but not yet named as saved has to be found by hand: the
    user is given its new ID, the saved entity ID and the warning to inspect it
    before retrying; nothing says no change was made."""
    detail = (
        {"conflicting_entity_id": "sensor.example"}
        if reason == "entity_id_collision"
        else {
            "entity_id_mapping": {
                "created_entity_id": "sensor.generated",
                "target_entity_id": "sensor.example",
            }
        }
    )
    restore_fails_with(
        snapshot_manager,
        "helper_template",
        bm.BackupRestoreError(
            "Recovery is incomplete; inspect the new entry before retrying",
            apply_status="applied",
            verification_status="unavailable",
            restore_mode="recreated",
            original_entry_id="original-entry",
            entry_id="new-entry",
            entity_id="new-entry",
            reason=reason,
            **detail,
        ),
    )
    reply = await restore_through(
        "settings",
        snapshot_manager,
        component_core_client,
        monkeypatch,
        saved_template.snapshot.name,
    )
    assert reply.status is not None

    rendered = run_restore(
        settings_script,
        {"status": reply.status, "json": reply.payload},
        saved_template.snapshot.name,
    )

    alert = rendered.alerts[0]
    assert "new-entry" in alert
    assert "sensor.example" in alert
    assert "before retrying" in alert
    assert "Nothing was changed" not in alert
    if reason == "entity_rename_outcome_unknown":
        assert "sensor.generated" in alert
        assert "could not be confirmed" in alert
    assert len(rendered.fetches_to("/backups?")) == 1


async def test_a_saved_entity_id_that_is_taken_refuses_the_recreation(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    entity_registry: er.EntityRegistry,
    settings_script: str,
) -> None:
    """The recreation never takes another entity's ID: nothing is created, and
    the page says which ID is in the way."""
    snapshot = await removed_template(
        hass, snapshot_manager, entity_registry, keep_mapping=True
    )
    hass.states.async_set("sensor.custom", "taken")
    reply = await restore_through(
        "settings", snapshot_manager, component_core_client, monkeypatch, snapshot.name
    )

    assert reply.status == 409
    assert reply.payload["data"]["reason"] == "entity_id_collision"
    assert reply.payload["data"]["conflicting_entity_id"] == "sensor.custom"
    assert reply.payload["data"]["apply_status"] == "not_applied"
    assert hass.config_entries.async_entries("template") == []
    rendered = run_restore(
        settings_script, {"status": reply.status, "json": reply.payload}, snapshot.name
    )
    assert "Nothing was changed" in rendered.alerts[0]
    assert "sensor.custom" in rendered.alerts[0]
