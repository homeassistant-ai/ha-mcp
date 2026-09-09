"""Recovery references and observed options survive refusal and concurrency."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp.tools import config_entry_flow

from .test_template_helper_backup import _record, _response


@pytest.fixture
def manager(tmp_path):
    return bm.get_backup_manager(
        SimpleNamespace(),
        SimpleNamespace(
            enable_auto_backup=True,
            auto_backup_throttle_minutes=0,
            auto_backup_retain_per_entity=5,
            auto_backup_dir=str(tmp_path),
        ),
    )


def snapshot(manager, state="old", target="template-entry"):
    config = {"entry_id": target, "options": {**_record()["options"], "state": state}}
    return manager._write_snapshot("helper_template", target, config, "test")


async def test_refusal_keeps_selected_source_at_retention_one(manager, monkeypatch):
    manager._settings.auto_backup_retain_per_entity = 1
    source = snapshot(manager)
    monkeypatch.setattr(bm, "_template_entity_registry", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        bm,
        "_ws_send",
        AsyncMock(
            return_value=_response(_record({**_record()["options"], "name": "Changed"}))
        ),
    )
    with pytest.raises(bm.BackupRestoreError, match="identity changed") as caught:
        await manager.restore_snapshot(source.name)
    assert source.exists()
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert caught.value.outcome["safety_backup"] is None


async def test_active_restore_pins_source_and_safety_during_other_capture(manager):
    manager._settings.auto_backup_retain_per_entity = 1
    source = snapshot(manager)
    current = {
        "entry_id": "template-entry",
        "options": {**_record()["options"], "state": "before"},
    }
    entered, release = asyncio.Event(), asyncio.Event()

    async def restore(*_):
        entered.set()
        await release.wait()
        return {"success": True}

    manager.register(
        bm.DomainHandler("helper_template", AsyncMock(return_value=current), restore)
    )
    task = asyncio.create_task(manager.restore_snapshot(source.name))
    try:
        await entered.wait()
        safety_names = {p.name for p in manager.backup_dir.glob("*.yaml")} - {
            source.name
        }
        assert len(safety_names) == 1
        await manager.maybe_snapshot("helper_template", "template-entry", force=True)
        assert source.exists()
        (safety_name,) = safety_names
        assert manager.read_snapshot(safety_name)["config"] == current
    finally:
        release.set()
        await asyncio.gather(task)


async def test_serialized_restores_capture_immediate_predecessor(manager):
    first, second = snapshot(manager, "first"), snapshot(manager, "second")
    current = {
        "entry_id": "template-entry",
        "options": {**_record()["options"], "state": "initial"},
    }
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(*_):
        return deepcopy(current)

    async def restore(_client, _target, config):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        current.update(deepcopy(config))
        return {"success": True}

    manager.register(bm.DomainHandler("helper_template", fetch, restore))
    one = asyncio.create_task(manager.restore_snapshot(first.name))
    await entered.wait()
    two = asyncio.create_task(manager.restore_snapshot(second.name))
    # A cooperative second operation can run its initial file read before waiting.
    for _ in range(20):
        await asyncio.sleep(0.001)
    release.set()
    _, result = await asyncio.gather(one, two)
    saved = manager.read_snapshot(result["safety_backup"])
    assert saved["config"]["options"]["state"] == "first"


@pytest.mark.parametrize("value,changed", [(False, 0), (True, 1)])
async def test_verification_rejects_type_distinct_options(
    manager, monkeypatch, value, changed
):
    options = {
        "name": "Example",
        "template_type": "button",
        "press": [{"data": {"value": value}}],
    }
    before = {"entry_id": "template-entry", "options": options}
    after = deepcopy(before)
    after["options"]["press"][0]["data"]["value"] = changed
    monkeypatch.setattr(
        bm, "_fetch_template_helper", AsyncMock(side_effect=[before, after])
    )
    monkeypatch.setattr(
        config_entry_flow,
        "update_config_entry_options",
        AsyncMock(return_value={"success": True}),
    )
    with pytest.raises(bm.BackupRestoreError) as caught:
        await bm._restore_template_helper(manager._client, "template-entry", before)
    assert caught.value.outcome["apply_status"] == "applied"
    assert caught.value.outcome["verification_status"] == "mismatched"


@pytest.mark.parametrize("matched", [False, True])
async def test_lost_reply_reports_uncertainty_with_observed_readback(
    manager, monkeypatch, matched
):
    desired = {"entry_id": "template-entry", "options": _record()["options"]}
    current = deepcopy(desired)
    if not matched:
        current["options"]["state"] = "different"
    monkeypatch.setattr(
        bm, "_fetch_template_helper", AsyncMock(side_effect=[current, current])
    )
    error = config_entry_flow.OptionsFlowError(
        "Submit outcome unknown",
        apply_status="unknown",
        entry_id="template-entry",
        flow_id="flow",
    )
    monkeypatch.setattr(
        config_entry_flow, "update_config_entry_options", AsyncMock(side_effect=error)
    )
    with pytest.raises(bm.BackupRestoreError) as caught:
        await bm._restore_template_helper(manager._client, "template-entry", desired)
    assert caught.value.outcome["apply_status"] == "unknown"
    assert caught.value.outcome["verification_status"] == (
        "matched" if matched else "mismatched"
    )


async def test_safety_snapshot_can_recover_predecessor_at_retention_one(manager):
    manager._settings.auto_backup_retain_per_entity = 1
    source = snapshot(manager, "historical")
    current = {
        "entry_id": "template-entry",
        "options": {**_record()["options"], "state": "before"},
    }

    async def restore(_client, _target, config):
        current.update(deepcopy(config))
        return {"success": True}

    async def fetch(*_):
        return deepcopy(current)

    manager.register(bm.DomainHandler("helper_template", fetch, restore))
    applied = await manager.restore_snapshot(source.name)
    assert source.exists()
    assert current["options"]["state"] == "historical"
    recovered = await manager.restore_snapshot(applied["safety_backup"])
    assert current["options"]["state"] == "before"
    assert (
        manager.read_snapshot(recovered["safety_backup"])["config"]["options"]["state"]
        == "historical"
    )
    # Protection expires when calls finish; later captures resume normal retention.
    await manager.maybe_snapshot("helper_template", "template-entry", force=True)
    assert len(manager.list_snapshots(domain="helper_template")) == 1


async def test_cancellation_releases_entry_for_next_restore_and_keeps_recovery(manager):
    manager._settings.auto_backup_retain_per_entity = 1
    source = snapshot(manager)
    current = {"entry_id": "template-entry", "options": _record()["options"]}
    entered = asyncio.Event()

    async def cancelled_restore(*_):
        entered.set()
        await asyncio.Event().wait()

    manager.register(
        bm.DomainHandler(
            "helper_template", AsyncMock(return_value=current), cancelled_restore
        )
    )
    task = asyncio.create_task(manager.restore_snapshot(source.name))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(task)
    assert source.exists()
    assert len(manager.list_snapshots(domain="helper_template")) == 2
    manager.register(
        bm.DomainHandler(
            "helper_template",
            AsyncMock(return_value=current),
            AsyncMock(return_value={}),
        )
    )
    async with asyncio.timeout(2):
        result = await manager.restore_snapshot(source.name)
    assert manager.read_snapshot(result["safety_backup"])["config"] == current


async def test_alias_and_stable_id_restores_share_serialization(manager):
    first = snapshot(manager, "first")
    alias = manager._write_snapshot(
        "helper_template",
        "sensor.reused",
        manager.read_snapshot(first.name)["config"],
        "test",
    )
    current = {"entry_id": "template-entry", "options": _record()["options"]}
    entered, release = asyncio.Event(), asyncio.Event()
    targets = []

    async def restore(_client, target, _config):
        targets.append(target)
        if len(targets) == 1:
            entered.set()
            await release.wait()
        return {}

    manager.register(
        bm.DomainHandler("helper_template", AsyncMock(return_value=current), restore)
    )
    one = asyncio.create_task(manager.restore_snapshot(first.name))
    await entered.wait()
    two = asyncio.create_task(manager.restore_snapshot(alias.name))
    await asyncio.sleep(0.02)
    assert targets == ["template-entry"]
    release.set()
    await asyncio.gather(one, two)
    assert targets == ["template-entry", "template-entry"]


@pytest.mark.parametrize("status", ["applied", "unknown"])
async def test_unavailable_readback_retains_apply_knowledge(
    manager, monkeypatch, status
):
    desired = {"entry_id": "template-entry", "options": _record()["options"]}
    monkeypatch.setattr(
        bm,
        "_fetch_template_helper",
        AsyncMock(
            side_effect=[desired, bm.HomeAssistantError("private upstream payload")]
        ),
    )
    failure = config_entry_flow.OptionsFlowError(
        "private upstream payload",
        apply_status=status,
        entry_id="template-entry",
        flow_id="flow",
    )
    monkeypatch.setattr(
        config_entry_flow, "update_config_entry_options", AsyncMock(side_effect=failure)
    )
    with pytest.raises(bm.BackupRestoreError) as caught:
        await bm._restore_template_helper(manager._client, "template-entry", desired)
    assert caught.value.outcome["apply_status"] == status
    assert caught.value.outcome["verification_status"] == "unavailable"
    assert "private upstream payload" not in str(caught.value)
