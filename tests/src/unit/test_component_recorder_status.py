"""``ha_mcp_tools/recorder_status``: what Core's recorder holds, read from its instance."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant.components.recorder.common import (
    BlockRecorderTask,
    async_block_recorder,
    async_wait_recording_done,
)
from sqlalchemy.exc import SQLAlchemyError

from ._command_faults import arrival, before_handling
from ._component_core import ws_result

START = datetime(2026, 1, 1, tzinfo=UTC)


async def test_the_status_is_the_recorder_instances_own(
    recorder_mock: Recorder, hass: HomeAssistant, component_ws: Any
) -> None:
    hass.states.async_set("sensor.outside", "5", timestamp=START.timestamp())

    status = await ws_result(
        component_ws,
        "ha_mcp_tools/recorder_status",
        entity_ids=["sensor.outside", "sensor.absent"],
        wait=5,
    )

    assert status == {
        "enabled": recorder_mock.enabled,
        "purge_keep_days": recorder_mock.keep_days,
        "auto_purge": recorder_mock.auto_purge,
        "oldest_state": START.timestamp(),
        "migration_in_progress": False,
        "entities": {
            "sensor.outside": {"recorded": True, "has_state": True},
            "sensor.absent": {"recorded": True, "has_state": False},
        },
        "settled": True,
    }


async def test_wait_answers_only_after_the_recorders_queued_work_ran(
    recorder_mock: Recorder, hass: HomeAssistant, component_ws: Any
) -> None:
    """A statistics import is queued behind whatever the recorder is doing; a
    read made before it ran finds nothing."""
    assert await recorder_mock.lock_database()
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": "sensor.energy",
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [{"start": START, "sum": 1.0}],
    )

    try:
        with arrival(hass, "ha_mcp_tools/recorder_status") as arrived:
            asked = asyncio.ensure_future(
                ws_result(
                    component_ws, "ha_mcp_tools/recorder_status", entity_ids=[], wait=5
                )
            )
            await asyncio.wait_for(arrived.wait(), 5)
        await hass.async_block_till_done()
        assert not asked.done()
    finally:
        recorder_mock.unlock_database()
    assert (await asked)["settled"] is True

    listed = await ws_result(component_ws, "recorder/list_statistic_ids")
    assert [row["statistic_id"] for row in listed] == ["sensor.energy"]


async def test_wait_covers_a_job_the_recorder_is_already_running(
    recorder_mock: Recorder, hass: HomeAssistant, component_ws: Any
) -> None:
    """The running job has left the queue, so an empty queue proves nothing."""
    assert await recorder_mock.lock_database()
    try:
        await component_ws.send_json_auto_id(
            {"type": "ha_mcp_tools/recorder_status", "entity_ids": [], "wait": 30}
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(component_ws.receive_json(), 0.3)
    finally:
        recorder_mock.unlock_database()
    assert (await component_ws.receive_json())["result"]["settled"] is True


async def test_wait_outlasts_a_purge_that_queues_itself_again(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_ws: Any,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Core purges in passes and queues the next pass behind whatever waits."""
    freezer.move_to(datetime(2026, 6, 1, tzinfo=UTC))
    # One row per batch: a pass takes a fixed number of batches, so these old
    # rows need several passes.
    monkeypatch.setattr(recorder_mock, "max_bind_vars", 1)
    now = dt_util.utcnow()
    oldest = now - timedelta(days=40)
    for hour in range(60):
        hass.states.async_set(
            "sensor.door",
            str(hour),
            timestamp=(oldest + timedelta(hours=hour)).timestamp(),
        )
    hass.states.async_set("sensor.door", "now")
    await async_wait_recording_done(hass)
    await hass.services.async_call("recorder", "purge", {"keep_days": 5}, blocking=True)

    status = await ws_result(
        component_ws, "ha_mcp_tools/recorder_status", entity_ids=[], wait=30
    )

    assert status["settled"] is True
    assert status["oldest_state"] > (now - timedelta(days=5)).timestamp()


async def test_a_wait_that_runs_out_says_the_recorder_did_not_settle(
    recorder_mock: Recorder, hass: HomeAssistant, component_ws: Any
) -> None:
    await async_block_recorder(hass, 1)

    status = await ws_result(
        component_ws, "ha_mcp_tools/recorder_status", entity_ids=[], wait=0.2
    )

    assert status["settled"] is False


async def test_another_waits_probe_is_not_taken_for_recorder_work(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_ws: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waits made together are each answered by their first probe."""
    queue_task = recorder_mock.queue_task
    probes = 0
    arrived = 0
    all_arrived = asyncio.Event()

    def counted(task: Any) -> None:
        nonlocal probes
        probes += type(task).__name__ == "IdleProbe"
        queue_task(task)

    def count_arrival(msg: dict[str, Any]) -> None:
        nonlocal arrived
        arrived += 1
        if arrived == 4:
            all_arrived.set()

    monkeypatch.setattr(recorder_mock, "queue_task", counted)
    assert await recorder_mock.lock_database()
    try:
        with before_handling(hass, "ha_mcp_tools/recorder_status", count_arrival):
            for _ in range(4):
                await component_ws.send_json_auto_id(
                    {
                        "type": "ha_mcp_tools/recorder_status",
                        "entity_ids": [],
                        "wait": 30,
                    }
                )
            await all_arrived.wait()
        # Each handler runs up to its first wait: for the recorder, or for its turn.
        for _ in range(8):
            await asyncio.sleep(0)
    finally:
        recorder_mock.unlock_database()
    replies = [await component_ws.receive_json() for _ in range(4)]

    assert [reply["result"]["settled"] for reply in replies] == [True] * 4
    assert probes == 4


# With a commit interval the recorded state is still uncommitted when the probe runs.
@pytest.mark.parametrize("recorder_config", [{"commit_interval": 60}])
async def test_a_probe_core_skipped_does_not_end_a_later_wait_early(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_ws: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core skips a task when the commit before it fails; a wait made after
    that still outlasts the recorder's work."""
    queue_task = recorder_mock.queue_task
    probe_queued = asyncio.Event()

    def watched(task: Any) -> None:
        if type(task).__name__ == "IdleProbe":
            probe_queued.set()
        queue_task(task)

    session = recorder_mock.event_session
    assert session is not None
    commit = session.commit
    failed = False

    def commit_fails_once() -> None:
        nonlocal failed
        if failed:
            commit()
            return
        failed = True
        raise SQLAlchemyError("commit failed")

    monkeypatch.setattr(recorder_mock, "queue_task", watched)
    assert await recorder_mock.lock_database()
    try:
        hass.states.async_set("sensor.door", "open")
        await hass.async_block_till_done()
        monkeypatch.setattr(session, "commit", commit_fails_once)
        await component_ws.send_json_auto_id(
            {"type": "ha_mcp_tools/recorder_status", "entity_ids": [], "wait": 0.3}
        )
        await asyncio.wait_for(probe_queued.wait(), 5)
    finally:
        recorder_mock.unlock_database()
    assert (await component_ws.receive_json())["result"]["settled"] is False

    probe_queued.clear()
    assert await recorder_mock.lock_database()
    try:
        await component_ws.send_json_auto_id(
            {"type": "ha_mcp_tools/recorder_status", "entity_ids": [], "wait": 30}
        )
        await asyncio.wait_for(probe_queued.wait(), 5)
        queue_task(BlockRecorderTask(asyncio.Event(), 1.0))
    finally:
        recorder_mock.unlock_database()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(component_ws.receive_json(), 0.3)
    assert (await component_ws.receive_json())["result"]["settled"] is True


async def test_a_wait_made_during_startup_outlasts_the_startup_batch(
    async_test_recorder: Any,
    hass: HomeAssistant,
    component_ws: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core runs what was queued before it started as one batch, outside the
    queue a probe reads."""
    probe_queued = asyncio.Event()
    hass.set_state(CoreState.not_running)
    async with async_test_recorder(hass) as recorder:
        queue_task = recorder.queue_task

        def watched(task: Any) -> None:
            if type(task).__name__ == "IdleProbe":
                probe_queued.set()
            queue_task(task)

        monkeypatch.setattr(recorder, "queue_task", watched)
        await component_ws.send_json_auto_id(
            {"type": "ha_mcp_tools/recorder_status", "entity_ids": [], "wait": 30}
        )
        await asyncio.wait_for(probe_queued.wait(), 5)
        queue_task(BlockRecorderTask(asyncio.Event(), 1.0))
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(component_ws.receive_json(), 0.3)
        assert (await component_ws.receive_json())["result"]["settled"] is True
