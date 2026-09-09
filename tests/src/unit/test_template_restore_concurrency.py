"""Restore protection across queued retention and pre-submit failures."""

import asyncio
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    manager = bm.BackupManager(
        SimpleNamespace(
            auto_backup_dir=str(tmp_path),
            enable_auto_backup=True,
            auto_backup_throttle_minutes=0,
            auto_backup_retain_per_entity=1,
        ),
        SimpleNamespace(),
    )
    bm.register_default_handlers(manager, manager._client)
    return manager


def config(state):
    return {
        "entry_id": "entry",
        "options": {"name": "Example", "template_type": "sensor", "state": state},
    }


@pytest.mark.asyncio
async def test_rotation_dispatched_before_restore_must_honor_later_pin(
    manager, monkeypatch
):
    source = manager._write_snapshot(
        "helper_template", "entry", config("saved"), "test"
    )
    current = config("current")
    manager.register(
        bm.DomainHandler(
            "helper_template",
            AsyncMock(side_effect=lambda *_: deepcopy(current)),
            AsyncMock(return_value={"success": True}),
        )
    )
    entered, release = threading.Event(), threading.Event()
    original_rotate = manager._rotate
    calls = 0

    def delayed_rotate(domain, entity_id, protected=frozenset()):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert not protected
            entered.set()
            assert release.wait(5)
        return original_rotate(domain, entity_id, protected)

    monkeypatch.setattr(manager, "_rotate", delayed_rotate)
    pinned = asyncio.Event()
    original_protect = manager._protect_snapshot

    def observe_protection(name):
        original_protect(name)
        if name == source.name:
            pinned.set()

    monkeypatch.setattr(manager, "_protect_snapshot", observe_protection)
    capture = asyncio.create_task(
        manager.maybe_snapshot("helper_template", "entry", force=True)
    )
    assert await asyncio.to_thread(entered.wait, 2)
    restore = asyncio.create_task(manager.restore_snapshot(source.name))
    try:
        await asyncio.wait_for(pinned.wait(), timeout=2)
        assert not restore.done()
        assert source.exists()
        release.set()
        await capture
        result = await restore
        assert manager.read_snapshot(result["safety_backup"])["config"] == current
        assert source.exists(), (
            "earlier executor rotation ignored the active source pin"
        )
        assert manager.read_snapshot(source.name)["config"] == config("saved"), (
            "pruned source name was reused for the safety backup"
        )
    finally:
        release.set()
        await asyncio.gather(capture, restore, return_exceptions=True)


@pytest.mark.asyncio
async def test_failure_before_options_flow_keeps_not_applied_knowledge(
    manager, monkeypatch
):
    source = manager._write_snapshot(
        "helper_template", "entry", config("saved"), "test"
    )
    current = config("current")
    from ha_mcp.tools import config_entry_flow

    flow = AsyncMock()
    monkeypatch.setattr(config_entry_flow, "update_config_entry_options", flow)
    # Manager preflight and mandatory capture succeed; handler's pre-submit fetch fails.
    response = {
        "covered_types": ["template"],
        "helpers": [
            {
                "kind": "flow",
                "helper_type": "template",
                "entry_id": "entry",
                "entity_id": "sensor.example",
                "options": current["options"],
            }
        ],
    }
    monkeypatch.setattr(
        bm,
        "_ws_send",
        AsyncMock(
            side_effect=[
                response,
                response,
                bm.HomeAssistantError("offline before starting the options flow"),
            ]
        ),
    )
    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(source.name)
    flow.assert_not_awaited()
    assert caught.value.outcome["safety_backup"] is not None
    assert caught.value.outcome["apply_status"] == "not_applied"
