"""Deleted Template helpers recreate without losing references or another entity."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantError
from ha_mcp.tools import config_entry_flow

OPTIONS = {"name": "Example", "template_type": "sensor", "state": "{{ 12 }}"}
ENTITY = {
    "entity_id": "sensor.renamed_example",
    "unique_id": "old-entry",
    "name": "Custom name",
    "original_name": "Example",
}


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    state = SimpleNamespace(entries=[], registry=[], records=[], states=[])

    async def send(client, message):
        match message["type"]:
            case "ha_mcp_tools/helpers_list":
                return {
                    "covered_types": ["template"],
                    "helpers": deepcopy(state.records),
                }
            case "config/entity_registry/list":
                return deepcopy(state.registry)
            case "config/entity_registry/update":
                row = next(
                    r for r in state.registry if r["entity_id"] == message["entity_id"]
                )
                row["entity_id"] = message.get("new_entity_id", row["entity_id"])
                row["name"] = message.get("name", row.get("name"))
                return deepcopy(row)
        raise AssertionError(message)

    async def create(client, helper_type, options):
        assert helper_type == "template"
        assert options == {
            "name": "Example",
            "next_step_id": "sensor",
            "state": "{{ 12 }}",
        }
        state.entries.append({"entry_id": "new-entry", "domain": "template"})
        state.records.append(
            {
                "entry_id": "new-entry",
                "entity_id": "sensor.example",
                "kind": "flow",
                "helper_type": "template",
                "options": deepcopy(OPTIONS),
            }
        )
        state.registry.append(
            {
                **ENTITY,
                "entity_id": "sensor.example",
                "unique_id": "new-entry",
                "config_entry_id": "new-entry",
                "platform": "template",
                "name": None,
            }
        )
        return {"success": True, "entry_id": "new-entry"}

    client = SimpleNamespace(
        _request=AsyncMock(side_effect=lambda *_: deepcopy(state.entries)),
        get_states=AsyncMock(side_effect=lambda: deepcopy(state.states)),
    )
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=1,
        auto_backup_dir=str(tmp_path),
    )
    manager = bm.get_backup_manager(client, settings)
    monkeypatch.setattr(bm, "_ws_send", AsyncMock(side_effect=send))
    creator = AsyncMock(side_effect=create)
    native_create = config_entry_flow.create_flow_helper
    monkeypatch.setattr(config_entry_flow, "create_flow_helper", creator)
    config = {
        "entry_id": "old-entry",
        "options": deepcopy(OPTIONS),
        "entities": [deepcopy(ENTITY)],
    }
    name = manager._write_snapshot("helper_template", "old-entry", config, "test").name
    return SimpleNamespace(
        manager=manager,
        state=state,
        create=creator,
        name=name,
        config=config,
        native_create=native_create,
    )


async def test_recreates_deleted_helper_and_restores_renamed_reference(recovery):
    original = recovery.manager.read_snapshot(recovery.name)
    result = await recovery.manager.restore_snapshot(recovery.name)
    assert result["entity_id"] == "new-entry"
    assert result["original_entry_id"] == "old-entry"
    assert result["restore_mode"] == "recreated"
    assert result["verification_status"] == "matched"
    assert result["safety_backup"] is None
    assert recovery.state.registry[0]["entity_id"] == ENTITY["entity_id"]
    assert recovery.state.registry[0]["name"] == "Custom name"
    assert recovery.manager.read_snapshot(recovery.name) == original
    recovery.create.assert_awaited_once()


@pytest.mark.parametrize("occupied", ["registry", "state"])
async def test_collision_refuses_before_creating_and_preserves_occupant(
    recovery, occupied
):
    occupant = {
        "entity_id": ENTITY["entity_id"],
        "config_entry_id": "other",
        "unique_id": "other",
    }
    rows = recovery.state.registry if occupied == "registry" else recovery.state.states
    rows.append(occupant)
    with pytest.raises(bm.BackupRestoreError) as error:
        await recovery.manager.restore_snapshot(recovery.name)
    assert error.value.outcome["apply_status"] == "not_applied"
    assert error.value.outcome["reason"] == "entity_id_collision"
    assert rows == [occupant]
    recovery.create.assert_not_awaited()


async def test_component_absence_cannot_duplicate_existing_native_entry(recovery):
    recovery.state.entries = [
        {"entry_id": "old-entry", "domain": "template", "disabled_by": "user"}
    ]
    with pytest.raises(bm.BackupRestoreError) as error:
        await recovery.manager.restore_snapshot(recovery.name)
    assert error.value.outcome["reason"] == "entry_still_exists"
    recovery.create.assert_not_awaited()


@pytest.mark.parametrize("entries", [None, [None], [{"domain": "template"}]])
async def test_ambiguous_native_listing_refuses_recreation(recovery, entries):
    recovery.state.entries = entries
    with pytest.raises(bm.BackupRestoreError) as error:
        await recovery.manager.restore_snapshot(recovery.name)
    assert error.value.outcome["apply_status"] == "not_applied"
    recovery.create.assert_not_awaited()


async def test_lost_creation_reply_is_unknown_and_never_retried(recovery):
    recovery.create.side_effect = HomeAssistantError("reply lost")
    with pytest.raises(bm.BackupRestoreError) as error:
        await recovery.manager.restore_snapshot(recovery.name)
    assert error.value.outcome["apply_status"] == "unknown"
    assert error.value.outcome["reason"] == "creation_outcome_unknown"
    recovery.create.assert_awaited_once()


async def test_legacy_snapshot_recreates_with_explicit_reference_limit(recovery):
    recovery.config.pop("entities")
    path = recovery.manager._write_snapshot(
        "helper_template", "old-entry", recovery.config, "test"
    )
    result = await recovery.manager.restore_snapshot(path.name)
    assert result["entity_id"] == "new-entry"
    assert result["result"]["entity_ids_restored"] is False
    assert result["result"]["warnings"]


async def test_capture_persists_only_entity_recovery_metadata(recovery):
    recovery.state.records = [
        {
            "entry_id": "old-entry",
            "entity_id": ENTITY["entity_id"],
            "kind": "flow",
            "helper_type": "template",
            "options": OPTIONS,
        }
    ]
    recovery.state.registry = [
        {
            **ENTITY,
            "config_entry_id": "old-entry",
            "platform": "template",
            "labels": ["private"],
            "device_id": "not-needed",
        }
    ]
    snapshot = await bm._fetch_template_helper(recovery.manager._client, "old-entry")
    assert snapshot["entities"] == [ENTITY]


@pytest.mark.parametrize("changed_options", [False, True])
async def test_existing_entry_diff_only_previews_restorable_options(
    recovery, changed_options
):
    options = {**OPTIONS, "state": "{{ 99 }}"} if changed_options else OPTIONS
    recovery.state.records = [
        {
            "entry_id": "old-entry",
            "entity_id": "sensor.now_renamed",
            "kind": "flow",
            "helper_type": "template",
            "options": options,
        }
    ]
    recovery.state.registry = [
        {
            **ENTITY,
            "entity_id": "sensor.now_renamed",
            "name": "Renamed later",
            "config_entry_id": "old-entry",
        }
    ]
    diff = await recovery.manager.diff_snapshot(recovery.name)
    assert diff["unchanged"] is not changed_options
    assert diff["patch"] == (
        [{"op": "replace", "path": "/options/state", "value": "{{ 12 }}"}]
        if changed_options
        else []
    )
    data, current = await recovery.manager.snapshot_comparison(recovery.name)
    assert "entities" not in data["config"]
    assert "entities" not in current
    assert recovery.manager.read_snapshot(recovery.name)["config"]["entities"] == [
        ENTITY
    ]


async def test_racing_collision_preserves_created_helper_and_reports_identity(recovery):
    creator = recovery.create.side_effect

    async def race(*args):
        result = await creator(*args)
        recovery.state.registry.append(
            {
                "entity_id": ENTITY["entity_id"],
                "unique_id": "other",
                "config_entry_id": "other",
            }
        )
        return result

    recovery.create.side_effect = race
    with pytest.raises(bm.BackupRestoreError) as error:
        await recovery.manager.restore_snapshot(recovery.name)
    assert error.value.outcome["apply_status"] == "applied"
    assert error.value.outcome["entity_id"] == "new-entry"
    assert error.value.outcome["original_entry_id"] == "old-entry"
    assert error.value.outcome["reason"] == "entity_id_collision"
    assert len(recovery.state.registry) == 2
    assert recovery.state.registry[0]["entity_id"] == "sensor.example"
    assert recovery.state.registry[1]["config_entry_id"] == "other"
    assert recovery.manager.read_snapshot(recovery.name)
    recovery.create.assert_awaited_once()


async def test_lost_rename_reply_preserves_created_identity_without_retry(
    recovery, monkeypatch
):
    sender = bm._ws_send.side_effect

    async def lost_reply(client, message):
        result = await sender(client, message)
        if message["type"] == "config/entity_registry/update":
            raise HomeAssistantError("reply lost")
        return result

    send = AsyncMock(side_effect=lost_reply)
    monkeypatch.setattr(bm, "_ws_send", send)
    with pytest.raises(bm.BackupRestoreError) as error:
        await recovery.manager.restore_snapshot(recovery.name)
    assert error.value.outcome["apply_status"] == "applied"
    assert error.value.outcome["entity_id"] == "new-entry"
    assert error.value.outcome["verification_status"] == "unavailable"
    assert error.value.outcome["reason"] == "entity_rename_outcome_unknown"
    assert recovery.state.registry[0]["entity_id"] == ENTITY["entity_id"]
    assert (
        sum(
            call.args[1]["type"] == "config/entity_registry/update"
            for call in send.await_args_list
        )
        == 1
    )


@pytest.mark.parametrize(
    "records",
    [
        [{"kind": "flow", "helper_type": "template"}],
        [{"kind": "flow", "helper_type": "template", "entry_id": "other"}] * 2,
    ],
)
async def test_ambiguous_component_listing_never_starts_creation(recovery, records):
    recovery.state.records = records
    with pytest.raises(bm.BackupRestoreError):
        await recovery.manager.restore_snapshot(recovery.name)
    recovery.create.assert_not_awaited()


async def test_entry_reappearing_after_absence_check_is_never_updated(
    recovery, monkeypatch
):
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(bm, "_fetch_template_helper", fetch)
    # The manager's handler already captured the original function; replace it
    # to model absence during the safety decision and a reappearing native entry.
    recovery.manager.register(
        bm.DomainHandler("helper_template", fetch, bm._restore_template_helper)
    )
    recovery.state.entries = [{"entry_id": "old-entry", "domain": "template"}]
    updater = AsyncMock()
    monkeypatch.setattr(config_entry_flow, "update_config_entry_options", updater)
    with pytest.raises(bm.BackupRestoreError) as error:
        await recovery.manager.restore_snapshot(recovery.name)
    assert error.value.outcome["reason"] == "entry_still_exists"
    updater.assert_not_awaited()
    recovery.create.assert_not_awaited()


@pytest.mark.parametrize(
    "mapping",
    [
        [ENTITY, ENTITY],
        [{**ENTITY, "unique_id": "other"}],
        [{**ENTITY, "entity_id": "switch.other"}],
    ],
)
async def test_unsupported_snapshot_entity_mapping_refuses_creation(recovery, mapping):
    recovery.config["entities"] = mapping
    name = recovery.manager._write_snapshot(
        "helper_template", "old-entry", recovery.config, "test"
    ).name
    with pytest.raises(bm.BackupRestoreError):
        await recovery.manager.restore_snapshot(name)
    recovery.create.assert_not_awaited()


async def test_recreation_drives_native_template_menu_and_form(recovery, monkeypatch):
    client = recovery.manager._client
    client.start_config_flow = AsyncMock(
        return_value={
            "type": "menu",
            "flow_id": "create-flow",
            "step_id": "user",
            "menu_options": ["sensor", "binary_sensor"],
        }
    )
    submissions = []

    async def submit(flow_id, payload):
        assert flow_id == "create-flow"
        submissions.append(payload)
        if len(submissions) == 1:
            assert payload == {"next_step_id": "sensor"}
            return {
                "type": "form",
                "flow_id": "create-flow",
                "step_id": "sensor",
                "data_schema": [
                    {"name": "name", "required": True, "type": "string"},
                    {"name": "state", "required": True, "type": "string"},
                ],
            }
        assert payload == {"name": "Example", "state": "{{ 12 }}"}
        await recovery.create.side_effect(
            client,
            "template",
            {"name": "Example", "next_step_id": "sensor", "state": "{{ 12 }}"},
        )
        return {
            "type": "create_entry",
            "result": {"entry_id": "new-entry", "title": "Example"},
        }

    client.submit_config_flow_step = AsyncMock(side_effect=submit)
    client.abort_config_flow = AsyncMock()
    monkeypatch.setattr(config_entry_flow, "create_flow_helper", recovery.native_create)
    result = await recovery.manager.restore_snapshot(recovery.name)
    assert result["entity_id"] == "new-entry"
    assert len(submissions) == 2
    assert "warnings" not in result["result"]
    client.abort_config_flow.assert_not_awaited()
