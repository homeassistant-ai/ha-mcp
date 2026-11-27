"""Pre-write backups of flow helpers other than template (#2632).

Every flow helper (a config entry) ha_config_set_helper edits is snapshotted
as ``helper_<type>`` from its stored options, restored through its options
flow, and recreated through its creation flow when deleted. What a form does
not offer comes from Home Assistant's own forms, not a per-type list.

The snapshots are read through the ha_mcp_tools component on a real ``hass``.
utility_meter, group, statistics and derivative are Core's own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, patch

import pytest
from homeassistant.components.utility_meter import config_flow as meter_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_entity_ids
from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import auto_backup, helper_flows
from ha_mcp.tools.config_entry_flow import CreationFlowError, recreate_flow_helper

from ._core_setup import with_config_component
from ._utility_meter_core import meter_options, set_up_meter

_METER = "utility_meter"
_OPTIONS = meter_options()


@pytest.fixture
async def client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The server's client on a ``hass`` whose component lists flow helpers."""
    return await with_config_component(hass, component_core_client)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Auto-backup on, into the test's own directory."""
    configured = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path),
    )
    monkeypatch.setattr(auto_backup, "get_global_settings", lambda: configured)
    return configured


def _handler(client: HomeAssistantClient, settings: SimpleNamespace) -> Any:
    handler = bm.get_backup_manager(client, settings).handler_for(f"helper_{_METER}")
    assert handler is not None
    return handler


async def test_capture_reads_the_stored_options(
    hass: HomeAssistant, client: HomeAssistantClient, settings: SimpleNamespace
) -> None:
    entry = await set_up_meter(hass)

    snapshot = await _handler(client, settings).fetch(client, "sensor.energy")

    assert snapshot["entry_id"] == entry.entry_id
    assert snapshot["options"] == _OPTIONS
    assert [e["entity_id"] for e in snapshot["entities"]] == ["sensor.energy"]


async def _changed_then_restored(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    settings: SimpleNamespace,
    entry: MockConfigEntry,
    **changed: Any,
) -> tuple[AsyncMock, bm.BackupRestoreError | None]:
    """Snapshot the meter, change ``changed`` in Core, restore the snapshot;
    returns the spy on the options submits and the restore's error."""
    handler = _handler(client, settings)
    snapshot = await handler.fetch(client, entry.entry_id)
    hass.config_entries.async_update_entry(entry, options={**entry.options, **changed})
    with patch.object(
        client, "submit_options_flow_step", wraps=client.submit_options_flow_step
    ) as submitted:
        try:
            await handler.restore(client, entry.entry_id, snapshot)
        except bm.BackupRestoreError as err:
            return submitted, err
        finally:
            await hass.async_block_till_done()
    return submitted, None


async def test_restore_submits_what_the_options_form_offers(
    hass: HomeAssistant, client: HomeAssistantClient, settings: SimpleNamespace
) -> None:
    entry = await set_up_meter(hass)

    submitted, error = await _changed_then_restored(
        hass, client, settings, entry, source="sensor.other"
    )

    assert error is None
    offered = {str(key) for key in meter_flow.OPTIONS_SCHEMA.schema}
    submitted.assert_awaited_once_with(ANY, {k: _OPTIONS[k] for k in offered})
    assert entry.options == _OPTIONS


async def test_restore_refuses_a_changed_option_the_form_cannot_set(
    hass: HomeAssistant, client: HomeAssistantClient, settings: SimpleNamespace
) -> None:
    """A meter's cycle is fixed at creation: restoring an older cycle is refused
    with nothing applied, rather than reported as done."""
    entry = await set_up_meter(hass)

    submitted, error = await _changed_then_restored(
        hass, client, settings, entry, cycle="monthly"
    )

    assert error is not None
    assert "no restore form offers" in str(error)
    assert error.outcome["apply_status"] == "not_applied"
    submitted.assert_not_awaited()
    assert entry.options["cycle"] == "monthly"


def _edit(
    client: HomeAssistantClient, helper_type: str
) -> Callable[..., Awaitable[dict[str, Any]]]:
    @auto_backup.with_auto_backup(
        domain=f"helper_{helper_type}", id_param="helper_id", client=client
    )
    async def edit(helper_id: str) -> dict[str, Any]:
        return {"success": True}

    return edit


async def test_edit_whose_capture_cannot_be_read_warns_the_caller(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    settings: SimpleNamespace,
) -> None:
    """Without the component no flow helper's options can be read: the write
    goes ahead, and the response (not only the log) says that no backup was
    taken and why."""
    entry = await set_up_meter(hass)

    result = await _edit(core_client, _METER)(helper_id=entry.entry_id)

    assert result["success"] is True
    (warning,) = result["warnings"]
    assert warning.startswith(
        f"No pre-write backup of helper_{_METER}:{entry.entry_id}"
    )
    assert "unknown_command" in warning


@pytest.mark.parametrize("list_read", ["answered", "no-answer"])
async def test_a_failed_read_of_core_helper_flows_is_not_logged_as_no_helper(
    client: HomeAssistantClient,
    settings: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    list_read: str,
) -> None:
    """A type the component does not cover is skipped at INFO when Core's
    helper flow list does not name it either; when that list cannot be read,
    the capture is a failed fetch at WARNING, not an INFO skip."""
    if list_read == "no-answer":
        monkeypatch.setattr(
            helper_flows,
            "fetch_helper_flow_types",
            AsyncMock(side_effect=HomeAssistantConnectionError("down")),
        )

    with caplog.at_level(logging.INFO):
        result = await _edit(client, "flow_backup_unlisted")(helper_id="entry-1")

    assert result["success"] is True
    levels = {r.levelno for r in caplog.records if r.name == bm.logger.name}
    expected = logging.WARNING if list_read == "no-answer" else logging.INFO
    assert levels == {expected}


async def _guard_is_free(manager: bm.BackupManager, entry_id: str) -> bool:
    """Whether another task can take ``entry_id``'s config-entry write guard."""

    async def enter() -> None:
        async with manager.config_entry_write_guard(entry_id):
            pass

    task = asyncio.create_task(enter())
    done, _ = await asyncio.wait({task}, timeout=0.1)
    if not done:
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return bool(done)


@pytest.mark.parametrize(
    ("target", "held"),
    [("meter-entry", True), ("sensor.alias", False)],
    ids=["entry-id", "alias"],  # an alias target resolves on its own path
)
async def test_every_flow_helper_edit_runs_in_the_restore_critical_section(
    client: HomeAssistantClient, settings: SimpleNamespace, target: str, held: bool
) -> None:
    """The guard that keeps an edit and a restore of one entry apart is keyed
    by domain family, not by the template type alone."""
    manager = bm.get_backup_manager(client, settings)
    seen: list[bool] = []

    @auto_backup.with_auto_backup(
        domain=f"helper_{_METER}", id_param="helper_id", client=client
    )
    async def edit(helper_id: str) -> dict[str, Any]:
        seen.append(not await _guard_is_free(manager, target))
        return {"success": True}

    await edit(helper_id=target)

    assert seen == [held]


async def test_recreation_answers_the_menu_from_the_snapshot(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """A group stores its branch as group_type; the creation menu's option
    with that value is chosen, and no form is asked for the key."""
    options = {"group_type": "light", "name": "Hall", "entities": ["light.a"]}

    with patch.object(
        client, "submit_config_flow_step", wraps=client.submit_config_flow_step
    ) as submitted:
        result = await recreate_flow_helper(client, "group", options)

    (created,) = hass.config_entries.async_entries("group")
    assert result["entry_id"] == created.entry_id
    menu, form = [c.args[1] for c in submitted.await_args_list]
    assert menu == {"next_step_id": "light"}
    assert {key: form[key] for key in ("name", "entities")} == {
        "name": "Hall",
        "entities": ["light.a"],
    }
    assert "group_type" not in form


async def test_recreation_fills_every_form_of_a_multi_step_flow(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """statistics asks for its characteristic on a second form."""
    hass.states.async_set("sensor.t", "1", {"state_class": "measurement"})
    options = {
        "name": "Mean",
        "entity_id": "sensor.t",
        "state_characteristic": "mean",
        "sampling_size": 20,
    }

    with patch.object(
        client, "submit_config_flow_step", wraps=client.submit_config_flow_step
    ) as submitted:
        result = await recreate_flow_helper(client, "statistics", options)

    (created,) = hass.config_entries.async_entries("statistics")
    assert result["entry_id"] == created.entry_id
    answers = [c.args[1] for c in submitted.await_args_list]
    # Each of Core's three forms got the snapshot's values for its own fields.
    assert len(answers) == 3
    assert all(answer == {k: options[k] for k in answer} for answer in answers)
    assert {k for answer in answers for k in answer} == set(options)
    assert {k: created.options[k] for k in options} == options


async def test_recreation_refuses_a_snapshot_key_no_form_takes(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    with (
        patch.object(
            client, "submit_config_flow_step", wraps=client.submit_config_flow_step
        ) as submitted,
        pytest.raises(CreationFlowError) as caught,
    ):
        await recreate_flow_helper(client, "derivative", {"name": "D", "extra": 1})

    assert caught.value.reason == "unsupported_fields"
    assert caught.value.apply_status == "not_applied"
    submitted.assert_not_awaited()
    assert hass.config_entries.flow.async_progress() == []


async def test_entities_renamed_before_a_later_one_fails_are_reported(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    settings: SimpleNamespace,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A meter whose entry held a second entity is recreated with only its
    own sensor; the outcome still names the first entity's restore when the
    second is never found."""
    # The second entity never appears, so the restore waits out the whole
    # window. The window is three polls, not a span of real time, so a slow
    # answer from Core for the first entity cannot use it up.
    polls: list[float] = []

    async def poll_sleep(delay: float) -> None:
        polls.append(delay)
        if len(polls) > 3:
            raise TimeoutError
        await asyncio.sleep(delay)

    monkeypatch.setattr(
        backup_entity_ids,
        "asyncio",
        SimpleNamespace(sleep=poll_sleep, timeout=lambda _delay: asyncio.timeout(None)),
    )
    entry = await set_up_meter(hass)
    entity_registry.async_get_or_create(
        "sensor", _METER, f"{entry.entry_id}_peak", config_entry=entry
    )
    handler = _handler(client, settings)
    snapshot = await handler.fetch(client, entry.entry_id)
    assert len(snapshot["entities"]) == 2
    assert await hass.config_entries.async_remove(entry.entry_id)

    with pytest.raises(bm.BackupRestoreError) as caught:
        await handler.restore(client, entry.entry_id, snapshot)
    await hass.async_block_till_done()

    (restored,) = caught.value.outcome["entity_id_mapping"]
    assert restored["restored_entity_id"] == "sensor.energy"
    assert caught.value.outcome["verification_status"] == "unavailable"
