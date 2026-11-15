"""Restoring a deleted helper reports its new identity through every consumer.

The Template helper is made by Core's own config flow, snapshotted, then
removed, so its restore creates a new config entry: the reply names the new
entry and the entity ID mapping, and the page's script shows both.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient

from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_restore_consumers_core import (
    SavedTemplate,
    removed_template,
    restore_through,
    run_restore,
    watch_options_flow,
)
from ._backup_restore_consumers_core import saved_template as saved_template
from ._backup_restore_consumers_core import unchanged_template as unchanged_template
from ._template_helper_core import state_template

SAFETY_WARNING = (
    "This restore did NOT restart HA. To revert, restore the safety_backup."
)
NO_MAPPING_WARNING = "This snapshot has no entity mapping; the recreated helper may have a new entity ID."


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
