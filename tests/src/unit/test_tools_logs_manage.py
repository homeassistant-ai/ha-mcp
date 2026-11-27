"""ha_manage_logs runs Core's own recorder, statistics and log operations.

Every reply and refusal comes from Core's recorder, ``system_log``, ``logger``
and ``logbook`` on a real ``hass``, reached through the server's own client.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, get_args
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder import websocket_api as recorder_websocket_api
from homeassistant.components.recorder.db_schema import EventData, Events, EventTypes
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import (
    statistics_during_period,
)
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance as recorder_instance
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.components.recorder.common import (
    async_block_recorder,
    async_wait_recording_done,
)

from custom_components.ha_mcp_tools import core_contract
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.read_only import READ_ONLY_EXEMPT_TOOLS
from ha_mcp.tools import tools_logs_manage
from ha_mcp.tools.tools_logs_manage import (
    RECORDER_READS,
    LogsAction,
    LogsManageTools,
)
from ha_mcp.tools.tools_services import ServiceDiscoveryTools

from ._command_faults import before_handling, unanswered
from ._recorded_history import import_statistic, migrating, record_states

ENERGY = "sensor.energy"
START = datetime(2024, 1, 1, tzinfo=UTC)
END = START + timedelta(hours=3)
HOURS = [START + timedelta(hours=i) for i in range(3)]


def _rows(*sums: float) -> list[StatisticData]:
    return [
        {"start": start, "state": total, "sum": total}
        for start, total in zip(HOURS, sums, strict=True)
    ]


async def _manage(client: HomeAssistantClient, action: str, **kwargs: Any) -> Any:
    return await LogsManageTools(client).ha_manage_logs(action=action, **kwargs)


async def _refusal(client: HomeAssistantClient, action: str, **kwargs: Any) -> Any:
    with pytest.raises(ToolError) as raised:
        await _manage(client, action, **kwargs)
    return json.loads(str(raised.value))


async def _sums(hass: HomeAssistant, statistic_id: str) -> Any:
    """The hourly sums Core's recorder holds for ``statistic_id`` right now."""
    rows = await recorder_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        START,
        END,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return [row["sum"] for row in rows.get(statistic_id, [])]


@pytest.fixture
async def client(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The client on a ``hass`` with the recorder and ``system_log``."""
    assert await async_setup_component(hass, "system_log", {})
    return core_client


# --- Statistics commands ------------------------------------------------------


async def test_the_statistic_listing_pages(
    client: HomeAssistantClient, hass: HomeAssistant
) -> None:
    for name in ("a", "b", "c"):
        await import_statistic(hass, f"sensor.{name}", "kWh", "energy", _rows(1, 2, 3))

    first = await _manage(client, "recorder/list_statistic_ids", limit=2)
    rest = await _manage(client, "recorder/list_statistic_ids", limit=2, offset=2)

    listed = [row["statistic_id"] for row in first["result"] + rest["result"]]
    assert sorted(listed) == ["sensor.a", "sensor.b", "sensor.c"]
    assert (first["total_count"], first["has_more"]) == (3, True)
    assert (len(rest["result"]), rest["has_more"]) == (1, False)


async def test_the_statistic_listing_pages_in_statistic_id_order(
    client: HomeAssistantClient, hass: HomeAssistant
) -> None:
    """Core lists stored statistics before the ones only a sensor provides, so
    a clear moves a statistic within its listing; pages follow the IDs."""
    for name in ("c", "b", "a"):
        await import_statistic(hass, f"sensor.{name}", "kWh", "energy", _rows(1, 2, 3))

    first = await _manage(client, "recorder/list_statistic_ids", limit=1)

    assert [row["statistic_id"] for row in first["result"]] == ["sensor.a"]


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("KWH", ["sensor.a", "sensor.b"]),
        ("kwh sensor.b", ["sensor.b"]),
        ("m³", ["sensor.water"]),
        ("gas", []),
    ],
)
async def test_a_query_keeps_the_statistics_every_word_matches(
    client: HomeAssistantClient, hass: HomeAssistant, query: str, found: list[str]
) -> None:
    """Every word must be part of one of the row's values; without the
    component the entities' aliases are not searched, and the answer says so."""
    for name in ("a", "b"):
        await import_statistic(hass, f"sensor.{name}", "kWh", "energy", _rows(1, 2, 3))
    await import_statistic(hass, "sensor.water", "m³", "volume", _rows(1, 2, 3))

    result = await _manage(client, "recorder/list_statistic_ids", query=query)

    assert [row["statistic_id"] for row in result["result"]] == found
    assert result["total_count"] == len(found)
    assert any("aliases" in warning for warning in result["warnings"])


async def test_a_query_matches_the_names_core_gives_the_entity(
    component_client: HomeAssistantClient, hass: HomeAssistant
) -> None:
    """A meter is found by the alias of its entity, which its statistic row
    does not carry."""
    registry = er.async_get(hass)
    for name in ("a", "b"):
        registry.async_get_or_create("sensor", "demo", name, suggested_object_id=name)
        await import_statistic(hass, f"sensor.{name}", "kWh", "energy", _rows(1, 2, 3))
    registry.async_update_entity("sensor.b", aliases=["Gas meter"])

    result = await _manage(
        component_client, "recorder/list_statistic_ids", query="gas meter"
    )

    assert [row["statistic_id"] for row in result["result"]] == ["sensor.b"]
    assert "warnings" not in result


@pytest.mark.parametrize(
    ("action", "core_function"),
    [
        ("recorder/validate_statistics", "validate_statistics"),
        ("recorder/update_statistics_issues", "update_statistics_issues"),
    ],
)
async def test_a_statistics_maintenance_command_runs_cores_job(
    client: HomeAssistantClient, action: str, core_function: str
) -> None:
    real = getattr(recorder_websocket_api, core_function)
    with patch.object(recorder_websocket_api, core_function, wraps=real) as job:
        result = await _manage(client, action)

    assert result["success"] is True
    assert job.call_count == 1


# --- Services ----------------------------------------------------------------


async def _system_log(client: HomeAssistantClient) -> list[dict[str, Any]]:
    reply = await client.send_websocket_message({"type": "system_log/list"})
    entries: list[dict[str, Any]] = reply["result"]
    return entries


async def test_a_written_system_log_entry_is_listed(
    client: HomeAssistantClient,
) -> None:
    await _manage(client, "system_log.write", data={"message": "hamcp checkpoint"})

    assert ["hamcp checkpoint"] in [
        entry["message"] for entry in await _system_log(client)
    ]


# --- Core's refusals ---------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "data", "core_code", "code"),
    [
        (
            "recorder/adjust_sum_statistics",
            {
                "statistic_id": "sensor.unknown",
                "start_time": START.isoformat(),
                "adjustment": 1,
                "adjustment_unit_of_measurement": "kWh",
            },
            "unknown_statistic_id",
            "SERVICE_CALL_FAILED",
        ),
        (
            "recorder/adjust_sum_statistics",
            {
                "statistic_id": ENERGY,
                "start_time": START.isoformat(),
                "adjustment": 1,
                "adjustment_unit_of_measurement": "°C",
            },
            "invalid_units",
            "VALIDATION_INVALID_PARAMETER",
        ),
        (
            "recorder/clear_statistics",
            {
                "statistic_ids": [ENERGY],
                "start_time": HOURS[2].isoformat(),
                "end_time": HOURS[0].isoformat(),
            },
            "invalid_time_range",
            "VALIDATION_INVALID_PARAMETER",
        ),
        (
            "recorder/clear_statistics",
            {"statistic_ids": [ENERGY], "unexpected": True},
            "invalid_format",
            "VALIDATION_INVALID_PARAMETER",
        ),
        (
            "system_log.write",
            {"message": "x", "unexpected": True},
            "invalid_format",
            "VALIDATION_INVALID_PARAMETER",
        ),
    ],
)
async def test_cores_refusal_keeps_its_code(
    client: HomeAssistantClient,
    hass: HomeAssistant,
    action: str,
    data: dict[str, Any],
    core_code: str,
    code: str,
) -> None:
    await import_statistic(hass, ENERGY, "kWh", "energy", _rows(1, 2, 3))

    refusal = await _refusal(client, action, data=data)

    assert refusal["ha_error_code"] == core_code
    assert refusal["error"]["code"] == code


@pytest.mark.parametrize(
    ("action", "field"),
    [
        ("recorder/clear_statistics", "id"),
        ("recorder/clear_statistics", "type"),
        # The client takes this one out of a command and waits that long.
        ("recorder/clear_statistics", "_wait_timeout"),
    ],
)
async def test_data_cannot_carry_the_websocket_envelope(
    client: HomeAssistantClient, sent: AsyncMock, action: str, field: str
) -> None:
    refusal = await _refusal(client, action, data={field: "x", "message": "x"})

    assert refusal["error"]["code"] == "VALIDATION_FAILED"
    sent.assert_not_called()


# --- describe ----------------------------------------------------------------


async def test_describe_returns_cores_service_description_without_calling_it(
    client: HomeAssistantClient, sent: AsyncMock
) -> None:
    catalog = await ServiceDiscoveryTools(client).ha_list_services(
        domain="system_log", detail_level="full"
    )

    result = await _manage(client, "system_log.write", describe=True)

    assert result["result"] == catalog["services"]["system_log.write"]
    assert all(
        call.args[0].get("type") != "call_service" for call in sent.call_args_list
    )


async def test_describe_of_a_command_without_fields_is_its_envelope(
    recorder_mock: Recorder, component_core_client: HomeAssistantClient
) -> None:
    """Core registers a command without fields with the schema False."""
    result = await _manage(
        component_core_client, "recorder/validate_statistics", describe=True
    )

    schema = result["result"]["schema"]
    assert schema["properties"]["type"] == {"const": "recorder/validate_statistics"}


async def test_describe_returns_cores_command_schema_through_the_component(
    recorder_mock: Recorder, component_core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    result = await _manage(
        component_core_client, "recorder/adjust_sum_statistics", describe=True
    )

    assert result["result"]["status"] == "available"
    assert not result.get("warnings")
    assert "adjustment" in result["result"]["schema"]["properties"]
    sent.assert_not_called()


async def test_describe_without_the_component_says_why_it_cannot(
    client: HomeAssistantClient, sent: AsyncMock
) -> None:
    result = await _manage(client, "recorder/adjust_sum_statistics", describe=True)

    assert result["result"]["status"] == "unavailable"
    assert result["result"]["reason"] in result["warnings"]
    sent.assert_not_called()


@pytest.mark.parametrize(
    ("bridge", "action", "kwargs"),
    [
        ("entity_aliases", "recorder/list_statistic_ids", {"query": "energy"}),
        ("core_contract", "recorder/clear_statistics", {"describe": True}),
        ("recorder_status", "recorder.enable", {}),
    ],
)
async def test_failed_component_read_warns_after_core_answers(
    component_client: HomeAssistantClient,
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    bridge: str,
    action: str,
    kwargs: dict[str, Any],
) -> None:
    from ha_mcp.tools.component_api import COMPONENT_VIA_MARKETPLACE

    monkeypatch.delitem(hass.data["websocket_api"], "ha_mcp_tools/" + bridge)
    result = await _manage(component_client, action, **kwargs)
    assert result["success"]
    assert any(COMPONENT_VIA_MARKETPLACE in w for w in result["warnings"])


# --- Waiting for the recorder ------------------------------------------------

EXTERNAL = {
    "metadata": {
        "has_sum": True,
        "mean_type": 0,
        "name": None,
        "source": "test",
        "statistic_id": "test:meter",
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    },
    "stats": [{"start": START.isoformat(), "sum": 1.0}],
}


@pytest.fixture
async def component_client(
    recorder_mock: Recorder, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The client on a ``hass`` with the recorder and the component's commands."""
    return component_core_client


async def test_a_recorder_write_returns_after_the_recorder_finished_it(
    component_client: HomeAssistantClient, hass: HomeAssistant
) -> None:
    """An import is queued behind whatever the recorder is doing."""
    await async_block_recorder(hass, 0.5)

    result = await _manage(
        component_client, "recorder/import_statistics", data=EXTERNAL
    )

    assert await _sums(hass, "test:meter") == [1.0]
    assert [row["statistic_id"] for row in result["statistics_metadata"]] == [
        "test:meter"
    ]
    assert "warnings" not in result


# A fixed instant for the states and Core's purge cutoff; ticking, since the
# client and the recorder wait on the clock.
@pytest.mark.freeze_time("2026-06-01T00:00:00+00:00", tick=True)
async def test_a_purge_returns_the_recorders_oldest_state(
    recorder_mock: Recorder, hass: HomeAssistant, component_client: HomeAssistantClient
) -> None:
    now = dt_util.utcnow()
    old = (now - timedelta(days=30)).timestamp()
    await record_states(
        hass, "sensor.door", ("open", None, old), ("closed", None, now.timestamp())
    )

    result = await _manage(component_client, "recorder.purge", data={"keep_days": 5})

    oldest = recorder_mock.states_manager.oldest_ts
    assert oldest is not None and oldest > old
    assert result["recorder"] == {
        **(await component_client.send_websocket_message({"type": "recorder/info"}))[
            "result"
        ],
        "purge_keep_days": recorder_mock.keep_days,
        "auto_purge": recorder_mock.auto_purge,
        "oldest_state": datetime.fromtimestamp(oldest, UTC).isoformat(
            timespec="seconds"
        ),
    }
    assert "warnings" not in result


async def test_without_the_component_the_wait_names_what_it_cannot_see(
    client: HomeAssistantClient,
) -> None:
    """Core's API shows the recorder's queue, not the job it is running."""
    result = await _manage(client, "recorder.purge", data={"keep_days": 5})

    assert result["success"] is True
    assert result["recorder"]["recording"] is True
    assert any("may still be running" in warning for warning in result["warnings"])


async def test_a_statistics_listing_waits_for_nothing(
    component_client: HomeAssistantClient, hass: HomeAssistant
) -> None:
    """A read changes nothing, so a busy recorder does not hold it up."""
    await async_block_recorder(hass, 5)

    result = await asyncio.wait_for(
        _manage(component_client, "recorder/list_statistic_ids"), 2
    )

    assert result["success"] is True


# --- what happens after the operation ran ------------------------------------------


async def test_a_readback_without_an_answer_does_not_fail_a_write_that_ran(
    client: HomeAssistantClient, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error here invites a retry, and an adjustment applied twice is wrong."""
    await import_statistic(hass, ENERGY, "kWh", "energy", _rows(1, 2, 3))

    async with unanswered(hass, "recorder/get_statistics_metadata", monkeypatch):
        result = await _manage(
            client,
            "recorder/adjust_sum_statistics",
            data={
                "statistic_id": ENERGY,
                "start_time": HOURS[1].isoformat(),
                "adjustment": 10,
                "adjustment_unit_of_measurement": "kWh",
            },
        )
    await async_wait_recording_done(hass)

    assert result["success"] is True
    assert any("do not repeat it" in warning for warning in result["warnings"])
    assert await _sums(hass, ENERGY) == [1, 12, 13]


def _unit_change(statistic_id: str) -> dict[str, str]:
    return {
        "statistic_id": statistic_id,
        "new_unit_of_measurement": "MWh",
        "old_unit_of_measurement": "kWh",
    }


async def test_a_unit_change_core_applied_returns_the_new_unit(
    component_client: HomeAssistantClient, hass: HomeAssistant
) -> None:
    await import_statistic(hass, ENERGY, "kWh", "energy", _rows(1, 2, 3))

    result = await _manage(
        component_client, "recorder/change_statistics_unit", data=_unit_change(ENERGY)
    )

    (row,) = result["statistics_metadata"]
    assert row["statistics_unit_of_measurement"] == "MWh"
    assert "warnings" not in result


async def test_a_unit_change_core_did_not_apply_is_said(
    component_client: HomeAssistantClient, hass: HomeAssistant
) -> None:
    """Core changes the unit only of a statistic its own recorder compiles, and
    answers the command with success either way."""
    await _manage(component_client, "recorder/import_statistics", data=EXTERNAL)

    result = await _manage(
        component_client,
        "recorder/change_statistics_unit",
        data=_unit_change("test:meter"),
    )

    assert result["warnings"] == [
        "Core accepted the command, but test:meter is stored in kWh, not MWh."
    ]


async def test_a_unit_change_still_queued_is_not_called_unapplied(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core queues the unit change and answers; read back before it ran, the
    old unit says nothing."""
    await import_statistic(hass, ENERGY, "kWh", "energy", _rows(1, 2, 3))
    monkeypatch.setattr(tools_logs_manage, "RECORDER_DRAIN_SECONDS", 0.3)
    await async_block_recorder(hass, 2)

    result = await _manage(
        component_client, "recorder/change_statistics_unit", data=_unit_change(ENERGY)
    )
    await async_wait_recording_done(hass)

    assert not any("is stored in" in warning for warning in result["warnings"])
    assert any("still working" in warning for warning in result["warnings"])


async def test_a_recorder_busy_for_the_whole_wait_still_returns_its_status(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client's own wait for a reply is as long as the wait asked of the
    recorder, and starts first."""
    monkeypatch.setattr(tools_logs_manage, "RECORDER_DRAIN_SECONDS", 0.5)
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 0.5)
    assert await recorder_mock.lock_database()
    try:
        result = await _manage(component_client, "recorder.enable")
    finally:
        recorder_mock.unlock_database()
    await async_wait_recording_done(hass)

    assert "recorder" in result
    assert any("still working" in warning for warning in result["warnings"])


async def test_cores_own_timeout_on_a_clear_is_not_a_refusal(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core stops waiting for its clear job after a while and answers
    ``timeout``; the job stays queued and runs."""
    await import_statistic(hass, ENERGY, "kWh", "energy", _rows(1, 2, 3))
    monkeypatch.setattr(recorder_websocket_api, "CLEAR_STATISTICS_TIME_OUT", 0.2)
    await async_block_recorder(hass, 1)

    result = await _manage(
        component_client, "recorder/clear_statistics", data={"statistic_ids": [ENERGY]}
    )

    assert result["success"] is True
    assert result["statistics_metadata"] == []
    assert await _sums(hass, ENERGY) == []


async def test_without_the_component_a_busy_recorder_is_a_warning(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools_logs_manage, "RECORDER_DRAIN_SECONDS", 0.5)
    assert await recorder_mock.lock_database()
    try:
        result = await _manage(client, "recorder.purge")
    finally:
        recorder_mock.unlock_database()
    await async_wait_recording_done(hass)

    assert result["success"] is True
    assert any("may still be running" in warning for warning in result["warnings"])


async def test_describe_of_a_service_core_does_not_have_says_so(
    core_client: HomeAssistantClient,
) -> None:
    """``system_log`` is not set up on this Home Assistant."""
    refusal = await _refusal(core_client, "system_log.write", describe=True)

    assert refusal["error"]["code"] == "SERVICE_NOT_FOUND"


def test_every_command_action_can_be_described_by_the_component() -> None:
    """The component's bridge describes only the commands on its own list."""
    commands = {action for action in get_args(LogsAction) if "/" in action}

    assert commands <= set(core_contract.COMMANDS)


def test_read_only_mode_allows_exactly_the_actions_that_wait_for_nothing() -> None:
    rule = READ_ONLY_EXEMPT_TOOLS["ha_manage_logs"].blocked_write
    reads = {
        action for action in get_args(LogsAction) if rule({"action": action}) is None
    }

    assert reads == set(RECORDER_READS)


async def test_a_write_whose_answer_is_lost_is_reported_as_possibly_applied(
    client: HomeAssistantClient, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core queues the adjustment before it answers; a plain connection error
    would invite a second one."""
    await import_statistic(hass, ENERGY, "kWh", "energy", _rows(1, 2, 3))

    async with unanswered(hass, "recorder/adjust_sum_statistics", monkeypatch):
        refusal = await _refusal(
            client,
            "recorder/adjust_sum_statistics",
            data={
                "statistic_id": ENERGY,
                "start_time": HOURS[1].isoformat(),
                "adjustment": 10,
                "adjustment_unit_of_measurement": "kWh",
            },
        )
    await async_wait_recording_done(hass)

    assert refusal["status"] == "applied_but_unverified"
    assert await _sums(hass, ENERGY) == [1, 12, 13]


async def test_a_read_whose_answer_is_lost_is_not_called_possibly_applied(
    client: HomeAssistantClient, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a write may have run; a lost read is a plain connection failure."""
    async with unanswered(hass, "recorder/list_statistic_ids", monkeypatch):
        refusal = await _refusal(client, "recorder/list_statistic_ids")

    assert refusal["error"]["code"] == "CONNECTION_TIMEOUT"
    assert refusal.get("status") != "applied_but_unverified"
    assert "may have run" not in refusal["error"]["message"]


async def test_a_recorder_write_during_a_live_migration_names_it(
    async_test_recorder: Any,
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorder's oldest state is unread while it migrates, not absent."""
    monkeypatch.setattr(tools_logs_manage, "RECORDER_DRAIN_SECONDS", 0.5)
    async with migrating(async_test_recorder, hass, monkeypatch):
        result = await _manage(component_core_client, "recorder.enable")

    assert result["recorder"]["oldest_state"] is None
    assert any("migrating its database" in warning for warning in result["warnings"])


async def test_a_unit_change_another_writer_beat_is_said(
    component_client: HomeAssistantClient, hass: HomeAssistant
) -> None:
    """Another change lands between the caller's read and its command; Core
    skips the caller's, whose old unit no longer holds."""
    await import_statistic(hass, ENERGY, "kWh", "energy", _rows(1, 2, 3))

    def other_writer(_: dict[str, Any]) -> None:
        recorder_instance(hass).async_change_statistics_unit(
            ENERGY,
            new_unit_of_measurement="Wh",
            old_unit_of_measurement="kWh",
        )

    with before_handling(hass, "recorder/change_statistics_unit", other_writer):
        result = await _manage(
            component_client,
            "recorder/change_statistics_unit",
            data=_unit_change(ENERGY),
        )

    assert result["warnings"] == [
        f"Core accepted the command, but {ENERGY} is stored in Wh, not MWh."
    ]


async def test_service_data_may_use_the_websocket_envelope_keys(
    recorder_mock: Recorder, hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """A service's data goes to Core as its service data, so a logger named
    ``type`` is a logger like any other."""
    named = logging.getLogger("type")
    level, logger_class = named.level, logging.getLoggerClass()
    named.setLevel(logging.WARNING)
    try:
        assert await async_setup_component(hass, "logger", {})

        await _manage(client, "logger.set_level", data={"type": "debug"})

        assert named.level == logging.DEBUG
        await _manage(
            client,
            "logger/log_level",
            data={"module": "type", "level": "info", "persistence": "none"},
        )
        assert named.level == logging.INFO
    finally:
        # Core's logger setup changes process-wide state.
        named.setLevel(level)
        logging.setLoggerClass(logger_class)


async def test_a_timed_out_clear_says_it_may_not_have_run(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's ``timeout`` answer also comes when the recorder skipped the job."""
    await import_statistic(hass, ENERGY, "kWh", "energy", _rows(1, 2, 3))
    monkeypatch.setattr(recorder_websocket_api, "CLEAR_STATISTICS_TIME_OUT", 0.2)
    await async_block_recorder(hass, 1)

    result = await _manage(
        component_client, "recorder/clear_statistics", data={"statistic_ids": [ENERGY]}
    )

    assert any("may not have run" in warning for warning in result["warnings"])


async def test_the_queue_watch_ends_at_its_deadline(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorder/info question still open at the deadline does not extend it."""
    monkeypatch.setattr(tools_logs_manage, "RECORDER_DRAIN_SECONDS", 0.3)
    with patch.object(
        client, "send_websocket_message", wraps=client.send_websocket_message
    ) as sent:
        async with unanswered(hass, "recorder/info", monkeypatch):
            result = await _manage(client, "recorder.enable")

    budgets = [
        call.args[0]["_wait_timeout"]
        for call in sent.call_args_list
        if call.args[0].get("type") == "recorder/info"
    ]
    assert budgets == [0.3]
    assert any("gave no answer" in warning for warning in result["warnings"])


async def test_a_logbook_entry_is_recorded_when_the_call_returns(
    recorder_mock: Recorder, hass: HomeAssistant, component_client: HomeAssistantClient
) -> None:
    """The recorder writes the entry's event after Core answers the call."""
    assert await async_setup_component(hass, "logbook", {})
    await async_block_recorder(hass, 0.5)

    await _manage(
        component_client,
        "logbook.log",
        data={"name": "Door", "message": "opened", "entity_id": "sensor.door"},
    )

    assert await recorder_instance(hass).async_add_executor_job(
        _logbook_entries, hass
    ) == ["opened"]


def _logbook_entries(hass: HomeAssistant) -> list[str]:
    """The messages of the logbook entries the recorder has written."""
    with session_scope(hass=hass, read_only=True) as session:
        rows = (
            session.query(EventData.shared_data)
            .join(Events, Events.data_id == EventData.data_id)
            .join(EventTypes, Events.event_type_id == EventTypes.event_type_id)
            .filter(EventTypes.event_type == "logbook_entry")
            .all()
        )
    return [json.loads(row.shared_data)["message"] for row in rows]


async def test_a_timed_out_clear_whose_outcome_read_is_lost_is_not_called_run(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await import_statistic(hass, ENERGY, "kWh", "energy", _rows(1, 2, 3))
    monkeypatch.setattr(recorder_websocket_api, "CLEAR_STATISTICS_TIME_OUT", 0.2)
    await async_block_recorder(hass, 1)

    async with unanswered(hass, "recorder/get_statistics_metadata", monkeypatch):
        result = await _manage(
            component_client,
            "recorder/clear_statistics",
            data={"statistic_ids": [ENERGY]},
        )

    assert any("may not have run" in warning for warning in result["warnings"])
    assert not any(" ran, " in warning for warning in result["warnings"])
