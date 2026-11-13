"""Pre-write backups of flow helpers other than template (#2632).

Every flow helper (a config entry) ha_config_set_helper edits is snapshotted
as ``helper_<type>`` from its stored options, restored through its options
flow, and recreated through its creation flow when deleted. What a form does
not offer comes from Home Assistant's own forms, not a per-type list.

The snapshots are read through the ha_mcp_tools component on a real ``hass``.
A utility meter here is a mocked integration in Core's place: its forms are
the ones each test needs. group, statistics and derivative are Core's own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, patch

import probatio
import pytest
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockEntity,
    MockModule,
    MockPlatform,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp import backup_entity_ids
from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import auto_backup, helper_flows
from ha_mcp.tools.config_entry_flow import CreationFlowError, recreate_flow_helper

_METER = "utility_meter"
_OPTIONS = {
    "name": "Energy",
    "source": "sensor.power",
    "cycle": "daily",
    "periodically_resetting": True,
}


class _MeterOptionsFlow(OptionsFlow):
    """The options form offers source and periodically_resetting; the entry's
    ``unmarked`` data leaves its last form unmarked (``last_step`` None)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={**self.config_entry.options, **user_input}
            )
        schema = probatio.Schema(
            {
                probatio.Required("source"): selector.EntitySelector(),
                probatio.Required("periodically_resetting"): bool,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
            last_step=None if self.config_entry.data.get("unmarked") else True,
        )


class _MeterFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=user_input["name"], data={}, options=user_input
            )
        schema = probatio.Schema(
            {
                probatio.Required("name"): str,
                probatio.Required("source"): selector.EntitySelector(),
                probatio.Required("cycle"): str,
                probatio.Required("periodically_resetting"): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, last_step=True)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _MeterOptionsFlow()


async def _setup_meter(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])


async def _unload_meter(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_unload_platforms(entry, ["sensor"])


async def _add_meter_sensor(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """The meter's one sensor, its unique_id derived from the entry's id."""
    async_add_entities(
        [MockEntity(unique_id=entry.entry_id, name=entry.options["name"])]
    )


@pytest.fixture
def meter_integration(hass: HomeAssistant) -> Iterator[None]:
    mock_integration(
        hass,
        MockModule(
            _METER, async_setup_entry=_setup_meter, async_unload_entry=_unload_meter
        ),
    )
    mock_platform(hass, f"{_METER}.config_flow")
    mock_platform(
        hass, f"{_METER}.sensor", MockPlatform(async_setup_entry=_add_meter_sensor)
    )
    with mock_config_flow(_METER, _MeterFlow):
        yield


@pytest.fixture
async def client(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    meter_integration: None,
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client on a ``hass`` whose component lists flow helpers."""
    assert await async_setup_component(hass, "config", {})
    yield component_core_client
    await hass.async_block_till_done()


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


async def _meter(hass: HomeAssistant, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(domain=_METER, data=data, options=dict(_OPTIONS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _handler(client: HomeAssistantClient, settings: SimpleNamespace) -> Any:
    handler = bm.get_backup_manager(client, settings).handler_for(f"helper_{_METER}")
    assert handler is not None
    return handler


async def test_capture_reads_the_stored_options(
    hass: HomeAssistant, client: HomeAssistantClient, settings: SimpleNamespace
) -> None:
    entry = await _meter(hass)

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
    entry = await _meter(hass)

    submitted, error = await _changed_then_restored(
        hass, client, settings, entry, source="sensor.other"
    )

    assert error is None
    submitted.assert_awaited_once_with(
        ANY, {"source": "sensor.power", "periodically_resetting": True}
    )
    assert entry.options == _OPTIONS


async def test_unmarked_last_form_is_applied_and_judged_by_readback(
    hass: HomeAssistant, client: HomeAssistantClient, settings: SimpleNamespace
) -> None:
    """A flow that never marks its last form (``last_step`` None) cannot be
    checked before it applies: the forms are submitted, and a snapshot option
    no form offers that differs is reported by the readback, not refused."""
    entry = await _meter(hass, unmarked=True)

    submitted, error = await _changed_then_restored(
        hass, client, settings, entry, cycle="monthly"
    )

    assert error is not None
    assert error.outcome["apply_status"] == "applied"
    assert error.outcome["verification_status"] == "mismatched"
    submitted.assert_awaited_once()
    assert entry.options["cycle"] == "monthly"


async def test_restore_refuses_a_changed_option_the_form_cannot_set(
    hass: HomeAssistant, client: HomeAssistantClient, settings: SimpleNamespace
) -> None:
    """A meter's cycle is fixed at creation: restoring an older cycle is refused
    with nothing applied, rather than reported as done."""
    entry = await _meter(hass)

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
    meter_integration: None,
    settings: SimpleNamespace,
) -> None:
    """Without the component no flow helper's options can be read: the write
    goes ahead, and the response (not only the log) says that no backup was
    taken and why."""
    entry = await _meter(hass)

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
            "_fetch_helper_flow_types",
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
    # The second entity never appears, so the restore waits out the whole window.
    monkeypatch.setattr(backup_entity_ids, "RECREATED_ENTITY_WAIT_S", 0.3)
    entry = await _meter(hass)
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
