"""The restore preview shows only what a restore would change, for both consumers.

A Template helper is the snapshot's subject, made by Core's own config flow. A
preview compares the snapshot with the entry it came from, whatever entity ID
that entry has now, and shows the same effects in Settings and in the MCP tool.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient

from ._backup_core import backup_settings as backup_settings
from ._backup_core import place_snapshot
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_restore_consumers_core import (
    SavedTemplate,
    diff_through,
    settings_route,
)
from ._backup_restore_consumers_core import unchanged_template as unchanged_template
from ._template_helper_core import create_template_sensor, set_template_state


def _rename_entity(registry: er.EntityRegistry, new_entity_id: str) -> None:
    registry.async_update_entity("sensor.example", new_entity_id=new_entity_id)


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
