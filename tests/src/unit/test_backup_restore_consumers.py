"""Restore outcomes, previews and recreations reach every backup consumer alike.

The manager raises one typed outcome; Settings answers it as JSON, the MCP tool
raises it as a structured error, and the page's script words it for the user.
Labels, Template helpers (made by Core's own config flow) and zones are real, on
a real ``hass``. The two replies no route sends (a response that never arrived,
a success flag that is false) are the browser's own, so they are written out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError

from ._backup_core import add_label, place_snapshot, snapshot_of
from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_restore_consumers_core import (
    SavedTemplate,
    diff_through,
    fill_the_disk,
    removed_template,
    restore_fails_with,
    restore_through,
    run_restore,
    settings_route,
    watch_options_flow,
)
from ._backup_restore_consumers_core import saved_template as saved_template
from ._backup_restore_consumers_core import unchanged_template as unchanged_template
from ._template_helper_core import (
    create_template_sensor,
    set_template_state,
    state_template,
)

Applied = Literal["not_applied", "unknown", "applied"]
Verified = Literal["not_run", "matched", "mismatched", "unavailable"]

FAILURES: list[tuple[Applied, Verified, str]] = [
    ("not_applied", "not_run", "Nothing was changed"),
    ("unknown", "unavailable", "could not be confirmed"),
    ("applied", "mismatched", "does not match"),
    ("applied", "unavailable", "verification is unavailable"),
]

SAFETY_WARNING = (
    "This restore did NOT restart HA. To revert, restore the safety_backup."
)
NO_MAPPING_WARNING = "This snapshot has no entity mapping; the recreated helper may have a new entity ID."


@pytest.fixture
async def saved_label(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> Path:
    """A snapshot of a label that still exists, so its restore takes a safety
    snapshot first."""
    return await snapshot_of(snapshot_manager, add_label(label_registry, "Kitchen"))


def _rename_entity(registry: er.EntityRegistry, new_entity_id: str) -> None:
    registry.async_update_entity("sensor.example", new_entity_id=new_entity_id)


def _without_entity_mapping(manager: bm.BackupManager, template: SavedTemplate) -> str:
    """Rewrite the snapshot as a release before the entity mapping stored it."""
    data = manager.read_snapshot(template.snapshot.name)
    config = {key: value for key, value in data["config"].items() if key != "entities"}
    legacy = place_snapshot(
        manager.backup_dir,
        f"helper_template.{template.entry.entry_id}.20260101_000000.yaml",
        template.entry.entry_id,
        domain="helper_template",
        held=config,
        text=False,
    )
    return legacy.name


# -- a failed restore keeps what it did -------------------------------------


@pytest.mark.parametrize("consumer", ["mcp", "script"])
@pytest.mark.parametrize(("applied", "verified", "words"), FAILURES)
async def test_a_failed_restore_tells_the_user_what_was_changed(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    settings_script: str,
    saved_label: Path,
    consumer: str,
    applied: Applied,
    verified: Verified,
    words: str,
) -> None:
    """Whether the restore changed anything, and the safety snapshot to revert
    with, are what the user needs after it fails."""
    error = bm.BackupRestoreError(
        "Restore could not be completed.",
        apply_status=applied,
        verification_status=verified,
    )
    restore_fails_with(snapshot_manager, "label", error)
    reply = await restore_through(
        "settings" if consumer == "script" else consumer,
        snapshot_manager,
        core_client,
        monkeypatch,
        saved_label.name,
    )

    payload = reply.payload
    assert payload["success"] is False
    assert payload["error"]["message"] == str(error)
    assert payload["data"]["apply_status"] == applied
    assert payload["data"]["verification_status"] == verified
    assert payload["data"]["restored_from"] == saved_label.name
    safety = payload["data"]["safety_backup"]
    assert safety != saved_label.name
    assert safety in {row["name"] for row in snapshot_manager.list_snapshots()}
    if consumer == "script":
        assert reply.status is not None and reply.status >= 400
        rendered = run_restore(
            settings_script,
            {"status": reply.status, "json": payload},
            saved_label.name,
        )
        assert len(rendered.alerts) == 1
        assert words in rendered.alerts[0]
        assert safety in rendered.alerts[0]
        assert len(rendered.fetches_to("/backups?")) == 1


@pytest.mark.parametrize("consumer", ["mcp", "settings"])
@pytest.mark.parametrize("cause", ["no-answer", "disk"])
async def test_a_restore_without_its_safety_snapshot_is_refused_with_the_backup_code(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    label_registry: lr.LabelRegistry,
    saved_label: Path,
    consumer: str,
    cause: str,
) -> None:
    """Restoring over the current label without its recovery point would lose
    it: both consumers refuse with the backup error code and change nothing."""
    label_id = snapshot_manager.read_snapshot(saved_label.name)["entity_id"]
    label_registry.async_update(label_id, name="Renamed since")
    if cause == "no-answer":
        monkeypatch.setattr(
            "ha_mcp.tools.helpers.get_connected_ws_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("secret-marker down")),
        )
    else:
        fill_the_disk(snapshot_manager, monkeypatch)
    reply = await restore_through(
        consumer, snapshot_manager, core_client, monkeypatch, saved_label.name
    )

    if consumer == "settings":
        assert reply.status == 409
    payload = reply.payload
    assert payload["error"]["code"] == "BACKUP_CAPTURE_FAILED"
    assert payload["data"]["apply_status"] == "not_applied"
    assert "secret-marker" not in json.dumps(payload)
    if cause == "no-answer":
        assert "HomeAssistantConnectionError" in payload["error"]["message"]
    else:
        assert "No space left on device" in payload["error"]["message"]
    live = label_registry.async_get_label(label_id)
    assert live is not None and live.name == "Renamed since"


@pytest.mark.parametrize("consumer", ["settings", "mcp"])
async def test_a_deleted_integration_entry_is_reported_as_not_found(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
) -> None:
    """A snapshot holds an entry's options, not the entry: once Core has
    deleted it, the restore says it is gone and changed nothing."""
    entry = MockConfigEntry(domain="test", title="Gone")
    entry.add_to_hass(hass)
    snapshot = await snapshot_manager.maybe_snapshot(
        "integration", entry.entry_id, force=True
    )
    assert snapshot is not None
    assert await hass.config_entries.async_remove(entry.entry_id)

    reply = await restore_through(
        consumer, snapshot_manager, core_client, monkeypatch, snapshot.name
    )

    if consumer == "settings":
        assert reply.status == 404
    assert reply.payload["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert reply.payload["data"]["reason"] == "entry_deleted"
    assert reply.payload["data"]["apply_status"] == "not_applied"


# -- the restore preview ----------------------------------------------------


async def test_a_preview_follows_the_original_entry_after_its_entity_id_is_reused(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    entity_registry: er.EntityRegistry,
    unchanged_template: SavedTemplate,
) -> None:
    """The entity ID the snapshot was taken under can now belong to another
    helper; comparing with that one would show the wrong changes."""
    entry = unchanged_template.entry
    _rename_entity(entity_registry, "sensor.renamed")
    await set_template_state(hass, entry, "changed")
    replacement = await create_template_sensor(hass, "replacement")
    assert replacement.entry_id != entry.entry_id
    assert entity_registry.async_get("sensor.example") is not None

    for consumer in ("settings", "mcp"):
        reply = await diff_through(
            consumer,
            snapshot_manager,
            core_client,
            monkeypatch,
            unchanged_template.snapshot.name,
        )
        assert "changed" in json.dumps(reply.payload)
        assert "replacement" not in json.dumps(reply.payload)
        if consumer == "settings":
            assert reply.status == 200
            assert f"current:{entry.entry_id}" in reply.payload["diff"]


@pytest.mark.parametrize(
    "scenario", ["registry", "options", "legacy", "legacy_options", "deleted"]
)
async def test_a_preview_shows_only_what_a_restore_would_change(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    entity_registry: er.EntityRegistry,
    unchanged_template: SavedTemplate,
    scenario: str,
) -> None:
    """A restore puts options back, not entity IDs: a renamed entity is no
    difference, an edited state is, and a deleted helper is a recreation."""
    name = unchanged_template.snapshot.name
    if scenario.startswith("legacy"):
        name = _without_entity_mapping(snapshot_manager, unchanged_template)
    _rename_entity(entity_registry, "sensor.renamed")
    changed = scenario in {"options", "legacy_options"}
    if changed:
        await set_template_state(hass, unchanged_template.entry, "edited")
    missing = scenario == "deleted"
    if missing:
        assert await hass.config_entries.async_remove(unchanged_template.entry.entry_id)

    settings = await settings_route("diff_backup", snapshot_manager, monkeypatch, name)
    tool = await diff_through("mcp", snapshot_manager, core_client, monkeypatch, name)

    assert settings.status == 200
    assert tool.payload["success"]
    assert settings.payload["backup_present"] is not missing
    assert tool.payload["data"]["entity_missing"] is missing
    diff = settings.payload["diff"]
    if missing:
        assert "entities:" in diff
        assert "sensor.example" in diff
    else:
        assert "entities:" not in diff
        assert "sensor.renamed" not in diff
        assert bool(diff) is changed
        assert tool.payload["data"]["unchanged"] is not changed
        if changed:
            assert "saved" in diff and "edited" in diff
    viewed = await settings_route("view_backup", snapshot_manager, monkeypatch, name)
    stored: dict[str, Any] = yaml.safe_load(
        (snapshot_manager.backup_dir / name).read_text()
    )
    assert viewed.payload["data"]["config"] == stored["config"]


# -- a recreated helper reports its new identity ----------------------------


@pytest.mark.parametrize("keep_mapping", [True, False], ids=["mapped", "unmapped"])
async def test_a_recreated_helper_shows_its_new_identity_in_the_page(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    entity_registry: er.EntityRegistry,
    settings_script: str,
    keep_mapping: bool,
) -> None:
    """The recreated helper has a new entry ID, and the user needs it, and what
    became of the saved entity ID, to find it."""
    snapshot = await removed_template(
        hass, snapshot_manager, entity_registry, keep_mapping=keep_mapping
    )
    reply = await restore_through(
        "settings", snapshot_manager, component_core_client, monkeypatch, snapshot.name
    )

    assert reply.status == 200
    outcome = reply.payload["data"]
    assert outcome["restore_mode"] == "recreated"
    assert outcome["verification_status"] == "matched"
    assert outcome["safety_backup"] is None
    created = outcome["entity_id"]
    assert created != outcome["original_entry_id"]
    assert outcome["result"]["entity_ids_restored"] is keep_mapping
    rendered = run_restore(
        settings_script,
        {"status": reply.status, "json": reply.payload},
        snapshot.name,
    )

    alert = rendered.alerts[0]
    assert created in alert
    assert "verified" in alert
    if keep_mapping:
        mapping = outcome["result"]["entity_id_mapping"]
        assert mapping, "the entity was saved under another ID than the new one's"
        assert mapping[0]["restored_entity_id"] == "sensor.custom"
        assert mapping[0]["created_entity_id"] in alert
        assert "sensor.custom" in alert
    else:
        assert "may have a new entity ID" in alert
    assert "Safety backup: (none)" not in alert
    assert (
        "Existing Template helpers require a fresh safety backup"
        in rendered.confirms[0]
    )
    assert len(rendered.fetches_to("/backups?")) == 1


@pytest.mark.parametrize("keep_mapping", [True, False], ids=["mapped", "unmapped"])
async def test_the_tool_moves_a_recreation_warning_to_its_envelope(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    entity_registry: er.EntityRegistry,
    keep_mapping: bool,
) -> None:
    """A warning the handler returns is shown once, beside the result and not
    inside it; a recreation that kept every ID has none."""
    snapshot = await removed_template(
        hass, snapshot_manager, entity_registry, keep_mapping=keep_mapping
    )
    reply = await restore_through(
        "mcp", snapshot_manager, component_core_client, monkeypatch, snapshot.name
    )

    assert reply.payload["success"] is True
    assert "warnings" not in reply.payload["data"]["result"]
    if keep_mapping:
        assert "warnings" not in reply.payload
    else:
        assert reply.payload["warnings"] == [NO_MAPPING_WARNING]


@pytest.mark.parametrize("consumer", ["settings", "mcp"])
async def test_a_restore_over_an_existing_helper_warns_that_home_assistant_was_not_restarted(
    snapshot_manager: bm.BackupManager,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    saved_template: SavedTemplate,
    consumer: str,
) -> None:
    """The tool's reply points at the safety snapshot to revert with; Settings
    shows that snapshot in the result and adds no warning of its own."""
    with watch_options_flow(component_core_client) as flow:
        reply = await restore_through(
            consumer,
            snapshot_manager,
            component_core_client,
            monkeypatch,
            saved_template.snapshot.name,
        )

    assert reply.payload["success"] is True
    assert saved_template.entry.options["state"] == state_template("saved")
    flow.submit.assert_awaited_once()
    safety = reply.payload["data"]["safety_backup"]
    assert safety in {row["name"] for row in snapshot_manager.list_snapshots()}
    if consumer == "mcp":
        assert reply.payload["warnings"] == [SAFETY_WARNING]
    else:
        assert "warnings" not in reply.payload


@pytest.mark.parametrize("consumer", ["settings", "mcp"])
async def test_a_handler_warning_and_the_safety_warning_reach_the_tool_together(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
) -> None:
    """A zone's icon stored after the snapshot cannot be removed by a restore:
    Core's warning stays beside the result in Settings, and the tool lists it
    with the one about the safety snapshot, each once."""
    assert await async_setup_component(hass, "zone", {})
    zone = {"name": "Work", "latitude": 40.0, "longitude": -74.0, "radius": 100}
    created = await core_client.send_websocket_message({"type": "zone/create", **zone})
    zone_id = created["result"]["id"]
    snapshot = await snapshot_manager.maybe_snapshot("zone", zone_id, force=True)
    assert snapshot is not None
    await core_client.send_websocket_message(
        {"type": "zone/update", "zone_id": zone_id, **zone, "icon": "mdi:briefcase"}
    )

    reply = await restore_through(
        consumer, snapshot_manager, core_client, monkeypatch, snapshot.name
    )

    assert reply.payload["success"] is True
    result = reply.payload["data"]["result"]
    if consumer == "settings":
        assert "mdi:briefcase" in result["warnings"][0]
        assert "warnings" not in reply.payload
    else:
        assert "warnings" not in result
        warnings = reply.payload["warnings"]
        assert "mdi:briefcase" in warnings[0]
        assert warnings[-1] == SAFETY_WARNING
        assert len(warnings) == len(set(warnings))


# -- the page's script words an unfinished restore ---------------------------


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


# -- a Template helper restore reports the same outcome everywhere ----------


@pytest.mark.parametrize("consumer", ["mcp", "script"])
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


@pytest.mark.parametrize("consumer", ["mcp", "script"])
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


@pytest.mark.parametrize("consumer", ["mcp", "script"])
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
