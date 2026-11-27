"""What the running recorder holds, for ``ha_mcp_tools/recorder_status``.

Core's REST and WebSocket APIs return recorded rows but not how far back the
recorder keeps them: ``recorder/info`` carries neither its purge settings nor
its oldest state. The recorder instance knows both, and whether its filter
records an entity. With ``wait`` the reply is held until the recorder has
finished its work, which Core's API cannot tell either.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_RECORDER_STATUS = "ha_mcp_tools/recorder_status"
# Advertised by websocket_api's info; without it a short or empty history read
# says only what Core's own commands tell, and a recorder write is not waited for.
CAPABILITY = "recorder_status"
# The longest a caller may ask the bridge to wait for the recorder.
_MAX_WAIT_SECONDS = 120
_WAITS_KEY = "ha_mcp_tools_recorder_waits"


def recorder_status(
    hass: HomeAssistant,
    msg: dict[str, Any],
    settled: bool | None = None,
    first_statistics: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Whether the recorder is enabled, its purge settings and oldest state, per
    entity whether it is recorded and has a state now, and what the caller's
    wait and first-statistic lookup found."""
    from homeassistant.components.recorder.entity_options import is_entity_recorded
    from homeassistant.helpers.recorder import get_instance

    instance = get_instance(hass)
    status: dict[str, Any] = {
        # What recorder.disable turns off; recorder/info's `recording` is not it.
        "enabled": instance.enabled,
        "purge_keep_days": instance.keep_days,
        "auto_purge": instance.auto_purge,
        "oldest_state": instance.states_manager.oldest_ts,
        # Core reads its oldest state only once a migration has finished.
        "migration_in_progress": instance.migration_in_progress,
        "entities": {
            entity_id: {
                "recorded": is_entity_recorded(hass, entity_id),
                "has_state": hass.states.get(entity_id) is not None,
            }
            for entity_id in msg["entity_ids"]
        },
    }
    if settled is not None:
        status["settled"] = settled
    if first_statistics is not None:
        status["first_short_term_statistics"] = first_statistics
    return status


@functools.cache
def _idle_probe() -> Any:
    """A recorder task that reports whether anything is queued behind it.

    By the time it runs, in the recorder's own thread, every job queued before
    it has finished and the queue holds everything still to come: a purge
    queues its next pass before it returns. Built on first use, because the
    recorder's task class can only be imported where the recorder is installed.
    """
    from homeassistant.components.recorder.tasks import RecorderTask

    @dataclasses.dataclass(slots=True)
    class IdleProbe(RecorderTask):
        done: asyncio.Future[bool]

        def run(self, instance: Any) -> None:
            # Core runs what was queued before it started as one batch outside
            # its queue, and keeps this flag for its own block_till_done.
            idle = instance.backlog == 0 and getattr(
                instance, "_processed_startup_batch", True
            )
            instance.hass.loop.call_soon_threadsafe(self._answer, idle)

        def _answer(self, idle: bool) -> None:
            if not self.done.done():
                self.done.set_result(idle)

    return IdleProbe


async def _settle(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """With ``wait`` (seconds), let the recorder finish its work before it is read.

    ``recorder/info``'s backlog and the recorder's own ``async_block_till_done``
    cannot tell: the recorder takes a job off its queue before running it.
    ``settled`` is False when the time ran out first.
    """
    if "wait" not in msg:
        return {}
    from homeassistant.helpers.recorder import get_instance

    instance = get_instance(hass)
    # One wait probes at a time: a probe then finds only recorder work behind
    # it, never another wait's probe, wherever the recorder holds its tasks.
    one_at_a_time: asyncio.Lock = hass.data.setdefault(_WAITS_KEY, asyncio.Lock())
    try:
        async with asyncio.timeout(msg["wait"]), one_at_a_time:
            while True:
                done: asyncio.Future[bool] = hass.loop.create_future()
                instance.queue_task(_idle_probe()(done))
                if await done:
                    return {"settled": True}
    except TimeoutError:
        return {"settled": False}


def _first_statistics(hass: HomeAssistant, ids: list[str]) -> dict[str, float]:
    from homeassistant.components.recorder.db_schema import StatisticsShortTerm
    from homeassistant.components.recorder.statistics import (
        _first_statistic,
        get_metadata_with_session,
    )
    from homeassistant.components.recorder.util import session_scope
    from homeassistant.helpers.recorder import get_instance

    with session_scope(hass=hass, read_only=True) as session:
        records = get_metadata_with_session(
            get_instance(hass), session, statistic_ids=set(ids)
        )
        return {
            statistic_id: first.timestamp()
            for statistic_id, (metadata_id, _) in records.items()
            if (first := _first_statistic(session, StatisticsShortTerm, metadata_id))
            is not None
        }


async def _prepare(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    from homeassistant.helpers.recorder import get_instance

    result = await _settle(hass, msg)
    if ids := msg.get("statistic_ids"):
        result["first_statistics"] = await get_instance(hass).async_add_executor_job(
            _first_statistics, hass, ids
        )
    return result


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""
    return [
        (
            {
                vol.Required("type"): WS_RECORDER_STATUS,
                vol.Required("entity_ids"): [str],
                vol.Optional("statistic_ids"): [str],
                vol.Optional("wait"): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=_MAX_WAIT_SECONDS)
                ),
            },
            recorder_status,
            _prepare,
        )
    ]
