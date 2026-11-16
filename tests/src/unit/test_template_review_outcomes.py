"""Review regressions for Template snapshot validation and recovery diagnostics.

The helper is a Template sensor that Core's own config flow made; snapshots
are captured from it over the server's own client, and a test edits the
stored file to get a snapshot an older release or a hand edit left behind.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import config_entry_flow

from ._command_faults import received
from ._template_backup_core import degrade_secret_scrub, real_snapshot
from ._template_backup_core import manager as manager
from ._template_backup_core import settings as settings
from ._template_backup_core import template_entry as template_entry
from ._template_helper_core import (
    create_template_sensor,
    set_template_state,
    state_template,
)
from ._template_recovery_core import (
    RENAMED,
    DeletedTemplate,
    edit_snapshot,
    recreated_entry,
    then_after,
)
from ._template_recovery_core import deleted_template as deleted_template

HELPERS_LIST = "ha_mcp_tools/helpers_list"


def _with_options(path: Path, options: Any) -> None:
    def change(data: dict[str, Any]) -> None:
        data["config"]["options"] = options

    edit_snapshot(path, change)


@pytest.mark.parametrize("field", ["domain", "entity_id", "config"])
async def test_diff_rejects_missing_snapshot_envelope_field(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    field: str,
) -> None:
    path = await real_snapshot(manager, template_entry)
    edit_snapshot(path, lambda data: data.pop(field))

    with (
        received(hass, HELPERS_LIST) as reads,
        pytest.raises(ValueError, match="Snapshot.*missing.*" + field),
    ):
        await manager.snapshot_comparison(path.name)

    assert reads[HELPERS_LIST] == []


@pytest.mark.parametrize("field", ["domain", "entity_id"])
async def test_diff_rejects_non_string_snapshot_identity(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    field: str,
) -> None:
    path = await real_snapshot(manager, template_entry)
    edit_snapshot(path, lambda data: data.update({field: None}))

    with (
        received(hass, HELPERS_LIST) as reads,
        pytest.raises(ValueError, match="Snapshot.*invalid.*" + field),
    ):
        await manager.snapshot_comparison(path.name)

    assert reads[HELPERS_LIST] == []


async def _refused_before_reading_home_assistant(
    hass: HomeAssistant, manager: bm.BackupManager, path: Path
) -> bm.BackupRestoreError:
    """Restore ``path``; it must be refused before anything is read from Core."""
    with (
        received(hass, HELPERS_LIST) as reads,
        pytest.raises(bm.BackupRestoreError) as caught,
    ):
        await manager.restore_snapshot(path.name)
    assert reads[HELPERS_LIST] == []
    assert caught.value.outcome["reason"] == "invalid_snapshot"
    assert caught.value.outcome["apply_status"] == "not_applied"
    return caught.value


@pytest.mark.parametrize("options", [None, {}], ids=["not-a-mapping", "empty"])
async def test_malformed_snapshot_is_permanent_refusal_before_live_read(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    caplog: pytest.LogCaptureFixture,
    options: Any,
) -> None:
    path = await real_snapshot(manager, template_entry)
    _with_options(path, options)

    with caplog.at_level(logging.WARNING):
        error = await _refused_before_reading_home_assistant(hass, manager, path)

    assert error.outcome["verification_status"] == "not_run"
    assert "snapshot_validation" in caplog.text


@pytest.mark.parametrize(
    "value", ["**redacted**", {"nested": "**redacted**"}, ["**redacted**"]]
)
async def test_exact_redaction_leaves_are_still_refused(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    value: Any,
) -> None:
    path = await real_snapshot(manager, template_entry)
    edit_snapshot(path, lambda data: data["config"]["options"].update(state=value))

    error = await _refused_before_reading_home_assistant(hass, manager, path)

    assert "redacted" in str(error)


async def test_redaction_marker_inside_a_template_remains_a_recoverable_value(
    hass: HomeAssistant, manager: bm.BackupManager
) -> None:
    """Only a value that IS the marker was redacted; a template that merely
    contains it is captured whole and restores."""
    entry = await create_template_sensor(hass, "**redacted**")
    path = await real_snapshot(manager, entry)
    await set_template_state(hass, entry, "edited since")

    await manager.restore_snapshot(path.name)

    assert entry.options["state"] == state_template("**redacted**")


async def test_recreated_identity_readback_mismatch_is_reported(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    deleted_template: DeletedTemplate,
) -> None:
    """Core renames the recreated entity, then another edit changes its name
    before the restore reads it back: the restore says so."""

    def rename_again() -> None:
        entity_registry.async_update_entity(RENAMED, name="Renamed meanwhile")

    with (
        then_after(hass, "config/entity_registry/update", rename_again),
        pytest.raises(bm.BackupRestoreError) as caught,
    ):
        await deleted_template.manager.restore_snapshot(deleted_template.name)

    assert caught.value.outcome["apply_status"] == "applied"
    assert caught.value.outcome["verification_status"] == "mismatched"
    assert caught.value.outcome["reason"] == "entity_identity_mismatch"
    assert caught.value.outcome["entity_id"] == recreated_entry(hass).entry_id


@pytest.mark.parametrize(
    "entities",
    ["not-a-list", "not-a-row", "duplicate"],
)
async def test_invalid_entity_metadata_is_a_snapshot_refusal(
    hass: HomeAssistant, deleted_template: DeletedTemplate, entities: str
) -> None:
    path = deleted_template.manager.backup_dir / deleted_template.name

    def change(data: dict[str, Any]) -> None:
        saved = data["config"]["entities"]
        data["config"]["entities"] = {
            "not-a-list": {},
            "not-a-row": [None],
            "duplicate": [*saved, *saved],
        }[entities]

    edit_snapshot(path, change)

    with pytest.raises(bm.BackupRestoreError) as caught:
        await deleted_template.manager.restore_snapshot(deleted_template.name)

    assert caught.value.outcome["reason"] == "invalid_snapshot"
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert not hass.config_entries.async_entries("template")


async def test_unavailable_verification_logs_safe_local_cause(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """secrets.yaml breaks after the options flow ran: the readback refuses to
    read unscrubbed options, and the log names that local cause."""
    source = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "before")
    native = config_entry_flow.restore_config_entry_options

    async def apply_then_break_secrets(*args: Any, **kwargs: Any) -> Any:
        result = await native(*args, **kwargs)
        degrade_secret_scrub(hass)
        return result

    monkeypatch.setattr(
        config_entry_flow, "restore_config_entry_options", apply_then_break_secrets
    )

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(bm.BackupRestoreError) as caught,
    ):
        await manager.restore_snapshot(source.name)

    assert caught.value.outcome["verification_status"] == "unavailable"
    assert "step=readback" in caplog.text
    assert "secret_scrub_degraded" in caplog.text


async def test_unknown_recreation_logs_step_and_type_without_remote_payload(
    hass: HomeAssistant,
    deleted_template: DeletedTemplate,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The answer to the creation flow gets no reply."""
    answered: list[tuple[str | None, Any]] = []

    async def no_reply(flow_id: str, answer: Any) -> None:
        live = {
            flow["flow_id"]: flow["handler"]
            for flow in hass.config_entries.flow.async_progress()
        }
        answered.append((live.get(flow_id), answer))
        raise HomeAssistantConnectionError("private upstream payload")

    monkeypatch.setattr(component_core_client, "submit_config_flow_step", no_reply)

    with caplog.at_level(logging.WARNING), pytest.raises(bm.BackupRestoreError):
        await deleted_template.manager.restore_snapshot(deleted_template.name)

    assert answered == [("template", {"next_step_id": "sensor"})]
    assert "step=create_entry" in caplog.text
    assert "CreationFlowError" in caplog.text
    assert "private upstream payload" not in caplog.text
