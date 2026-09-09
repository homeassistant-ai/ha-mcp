"""Regressions for Template capture trust, write coordination and identity."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp.tools import auto_backup

from .test_template_helper_backup import _record, _response


@pytest.fixture
def manager(tmp_path):
    return bm.get_backup_manager(
        SimpleNamespace(),
        SimpleNamespace(
            enable_auto_backup=True,
            auto_backup_throttle_minutes=30,
            auto_backup_retain_per_entity=5,
            auto_backup_dir=str(tmp_path),
        ),
    )


def _config(state="saved", entry_id="template-entry"):
    return {
        "entry_id": entry_id,
        "options": {**_record()["options"], "state": state},
    }


@pytest.mark.parametrize("mandatory", [False, True])
async def test_degraded_scrub_never_writes_snapshot(manager, monkeypatch, mandatory):
    response = {**_response(), "secret_scrub_degraded": True}
    monkeypatch.setattr(bm, "_ws_send", AsyncMock(return_value=response))
    if mandatory:
        with pytest.raises(bm.MandatoryBackupError, match="secret scrub"):
            await manager.maybe_snapshot(
                "helper_template", "template-entry", mandatory=True
            )
    else:
        assert await manager.maybe_snapshot("helper_template", "template-entry") is None
    assert not list(manager.backup_dir.glob("*.yaml"))


async def test_degraded_safety_capture_blocks_restore(manager, monkeypatch):
    source = manager._write_snapshot(
        "helper_template", "template-entry", _config(), "test"
    )
    monkeypatch.setattr(
        bm,
        "_ws_send",
        AsyncMock(
            side_effect=[_response(), {**_response(), "secret_scrub_degraded": True}]
        ),
    )
    restore = AsyncMock(return_value={"success": True})
    manager.register(
        bm.DomainHandler("helper_template", bm._fetch_template_helper, restore)
    )
    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(source.name)
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert caught.value.outcome["safety_backup"] is None
    restore.assert_not_awaited()
    assert list(manager.backup_dir.glob("*.yaml")) == [source]


@pytest.mark.parametrize(
    "first_target,second_target",
    [
        ("sensor.renamed_example", "template-entry"),
        ("template-entry", "sensor.renamed_example"),
    ],
)
async def test_alias_capture_uses_stable_header_and_shared_throttle(
    manager, monkeypatch, first_target, second_target
):
    monkeypatch.setattr(bm, "_ws_send", AsyncMock(return_value=_response()))
    first = await manager.maybe_snapshot("helper_template", first_target)
    assert first is not None
    assert manager.read_snapshot(first.name)["entity_id"] == "template-entry"
    assert await manager.maybe_snapshot("helper_template", second_target) is None
    assert set(manager._last_snapshot) == {"helper_template:template-entry"}
    assert set(manager._locks) == {"helper_template:template-entry"}


async def test_alias_capture_refetches_after_canonical_lock(manager, monkeypatch):
    manager._settings.auto_backup_throttle_minutes = 0
    resolved = asyncio.Event()
    current = _record()

    async def send(*_):
        result = deepcopy(_response(current))
        resolved.set()
        return result

    monkeypatch.setattr(bm, "_ws_send", send)
    lock = manager._locks.setdefault("helper_template:template-entry", asyncio.Lock())
    async with lock:
        capture = asyncio.create_task(
            manager.maybe_snapshot("helper_template", "sensor.renamed_example")
        )
        await asyncio.wait_for(resolved.wait(), 2)
        current["options"]["state"] = "after-lock"
    path = await asyncio.wait_for(capture, 2)
    assert (
        manager.read_snapshot(path.name)["config"]["options"]["state"] == "after-lock"
    )


async def test_capture_response_identity_round_trips_to_history(manager, monkeypatch):
    from ha_mcp.tools.backup import _edits_create

    monkeypatch.setattr(bm, "_ws_send", AsyncMock(return_value=_response()))
    result = await _edits_create(
        manager, "edits", "create", "helper_template", "sensor.renamed_example"
    )
    rows = manager.list_snapshots(
        domain="helper_template", entity_id=result["data"]["entity_id"]
    )
    assert [row["name"] for row in rows] == [result["data"]["backup_name"]]


@pytest.mark.parametrize(
    "target,config", [(None, _config()), ("wrong-entry", _config()), ("sensor.old", {})]
)
def test_invalid_legacy_identity_is_not_deleted(manager, target, config):
    import yaml

    path = manager._write_snapshot("helper_template", "sensor.old", config, "test")
    data = manager.read_snapshot(path.name)
    data["entity_id"] = target
    path.write_text(yaml.safe_dump(data))
    manager._settings.auto_backup_retain_per_entity = 1
    manager._write_snapshot("helper_template", "template-entry", _config(), "test")
    manager._rotate("helper_template", "template-entry")
    manager.delete_bulk(domain="helper_template", entity_id="template-entry")
    assert path.exists()


def test_legacy_alias_history_lists_and_deletes_by_stable_identity(manager):
    old = manager._write_snapshot("helper_template", "sensor.reused", _config(), "test")
    new = manager._write_snapshot(
        "helper_template", "template-entry", _config(), "test"
    )
    other = manager._write_snapshot(
        "helper_template", "sensor.reused", _config(entry_id="other-entry"), "test"
    )
    rows = manager.list_snapshots(domain="helper_template", entity_id="template-entry")
    assert {row["name"] for row in rows} == {old.name, new.name}
    assert {row["entity_id"] for row in rows} == {"template-entry"}
    assert set(
        manager.delete_bulk(domain="helper_template", entity_id="template-entry")[
            "deleted"
        ]
    ) == {old.name, new.name}
    assert other.exists()


async def test_legacy_alias_history_rotates_with_canonical_captures(
    manager, monkeypatch
):
    manager._settings.auto_backup_retain_per_entity = 1
    old = manager._write_snapshot("helper_template", "sensor.reused", _config(), "test")
    other = manager._write_snapshot(
        "helper_template", "sensor.reused", _config(entry_id="other-entry"), "test"
    )
    monkeypatch.setattr(bm, "_ws_send", AsyncMock(return_value=_response()))
    latest = await manager.maybe_snapshot(
        "helper_template", "template-entry", force=True
    )
    assert latest is not None and latest.exists()
    assert not old.exists()
    assert other.exists()


@pytest.mark.parametrize("enabled", [False, True])
async def test_alias_removal_uses_core_identity_and_waits_for_restore(
    manager, monkeypatch, enabled
):
    from ha_mcp.tools.tools_integrations import IntegrationTools

    manager._settings.enable_auto_backup = enabled
    resolved = asyncio.Event()

    async def send(message):
        if message["type"] == "config/entity_registry/get":
            assert message["entity_id"] == "sensor.secondary"
            resolved.set()
            return {"success": True, "result": {"config_entry_id": "template-entry"}}
        assert message["type"] == "config/entity_registry/list"
        return {"success": True, "result": []}

    manager._client.send_websocket_message = AsyncMock(side_effect=send)
    manager._client.delete_config_entry = AsyncMock(return_value={})
    # The native removal path works without a component or a readable scrub source.
    monkeypatch.setattr(
        bm,
        "_ws_send",
        AsyncMock(side_effect=bm.HomeAssistantError("component unavailable")),
    )
    monkeypatch.setattr(auto_backup, "get_global_settings", lambda: manager._settings)
    monkeypatch.setattr(auto_backup, "get_backup_manager", lambda *_: manager)
    task = None
    try:
        async with manager.config_entry_write_guard("template-entry"):
            task = asyncio.create_task(
                IntegrationTools(manager._client).ha_remove_helpers_integrations(
                    target="sensor.secondary",
                    helper_type="template",
                    confirm=True,
                    wait=False,
                )
            )
            await asyncio.wait_for(resolved.wait(), 2)
            await asyncio.sleep(0.02)
            manager._client.delete_config_entry.assert_not_awaited()
    finally:
        if task is not None:
            await asyncio.gather(task)
    manager._client.delete_config_entry.assert_awaited_once_with("template-entry")
    assert not list(manager.backup_dir.glob("*.yaml"))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("first_operation", ["edit", "restore"])
@pytest.mark.parametrize("edit_domain", ["helper_template", "integration"])
async def test_helper_edit_and_restore_serialize_capture_through_mutation(
    manager, monkeypatch, enabled, first_operation, edit_domain
):
    manager._settings.enable_auto_backup = enabled
    source = manager._write_snapshot(
        "helper_template", "template-entry", _config(), "test"
    )
    current = _config("initial")
    entered, release = asyncio.Event(), asyncio.Event()
    mutations = []

    async def mutate(operation, desired):
        mutations.append(operation)
        if operation == first_operation:
            entered.set()
            await release.wait()
        current.update(deepcopy(desired))

    async def fetch(*_):
        return deepcopy(current)

    async def restore(_client, _target, desired):
        await mutate("restore", desired)
        return {"success": True}

    manager.register(bm.DomainHandler("helper_template", fetch, restore))
    manager.register(bm.DomainHandler("integration", fetch, restore))
    monkeypatch.setattr(auto_backup, "get_global_settings", lambda: manager._settings)
    monkeypatch.setattr(auto_backup, "get_backup_manager", lambda *_: manager)

    @auto_backup.with_auto_backup(
        domain=edit_domain, id_param="helper_id", client=manager._client
    )
    async def edit(*, helper_id):
        await mutate("edit", _config("edited"))

    async def run(operation):
        if operation == "edit":
            return await edit(helper_id="template-entry")
        return await manager.restore_snapshot(source.name)

    first = asyncio.create_task(run(first_operation))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(
        run("restore" if first_operation == "edit" else "edit")
    )
    try:
        # Let both the async file read and the decorator run to the held lock.
        await asyncio.sleep(0.05)
        assert mutations == [first_operation]
    finally:
        release.set()
        results = await asyncio.gather(first, second)
    if first_operation == "edit":
        safety = manager.read_snapshot(results[1]["safety_backup"])
        assert safety["config"]["options"]["state"] == "edited"
        assert current["options"]["state"] == "saved"
    else:
        assert current["options"]["state"] == "edited"
