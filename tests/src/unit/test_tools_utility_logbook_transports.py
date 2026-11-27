"""``ha_get_logs`` logbook transports against Core's own logbook.

The rows come from Core's ``logbook`` and ``recorder`` integrations over the
component command, Core's REST view and Core's WebSocket command. The only
stand-in is a client that gets no answer at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_logs import LogTools

from ..e2e.utilities.component_path import component_fallback_warnings
from ._command_faults import dropped_on, pooled_socket, unanswered
from ._log_tool_calls import call_kwargs
from ._logbook_test_support import _recorder as _recorder
from ._logbook_test_support import logbook as logbook
from ._logbook_test_support import logbook_error, rest_reads, sent_types

# Core closes a socket whose token it refused; the client's close reply then
# re-arms aiohttp's heartbeat timer on that closed server socket.
_REFUSED_SOCKET_TIMER = pytest.mark.parametrize("expected_lingering_timers", [True])

COMPONENT_COMMAND = "ha_mcp_tools/logbook_events"
CORE_COMMAND = "logbook/get_events"


@pytest.fixture
async def kitchen_device(
    hass: HomeAssistant,
    logbook: None,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> str:
    """The id of a registered device that owns ``light.kitchen``."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "kitchen")}
    )
    entity_registry.async_get_or_create(
        "light",
        "test",
        "kitchen",
        suggested_object_id="kitchen",
        device_id=device.id,
        config_entry=entry,
    )
    return device.id


@pytest.fixture
async def rejected_client(
    core_client: HomeAssistantClient,
) -> AsyncIterator[HomeAssistantClient]:
    """A client of the same Core whose token Core does not know."""
    async with AsyncExitStack() as stack:
        client = HomeAssistantClient(
            base_url=core_client.base_url, token="unknown-token", verify_ssl=False
        )
        stack.push_async_callback(client.close)
        yield client


def sent_command(sent: AsyncMock, command: str) -> dict[str, Any]:
    """The first message of this type the client sent."""
    return next(
        call.args[0] for call in sent.await_args_list if call.args[0]["type"] == command
    )


def by_entity(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["entity_id"]: row for row in result["data"]["entries"]}


@pytest.mark.usefixtures("logbook")
async def test_component_rows_are_cores_own_and_skip_the_rest_view(
    component_core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """With the component the logbook is read in-process: names and ISO times
    come from Core and the REST view is never asked."""
    with rest_reads(component_core_client) as rest:
        result = await LogTools(component_core_client).get_logs(
            **call_kwargs(source="logbook")
        )

    command = sent_command(sent, COMPONENT_COMMAND)
    assert command["start_time"] < command["end_time"]
    rest.assert_not_awaited()
    assert not component_fallback_warnings(result)
    kitchen = by_entity(result)["light.kitchen"]
    assert kitchen["name"] == "Kitchen Light"
    assert dt_util.parse_datetime(kitchen["when"]) is not None


@pytest.mark.parametrize("order", ["oldest", "newest"])
@pytest.mark.parametrize("scope", ["entity", "device", "both"])
async def test_scoped_pages_are_cores_own_rows_with_what_caused_them(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    kitchen_device: str,
    scope: str,
    order: str,
) -> None:
    """Each kitchen change is caused by an action call that only Core's
    context lookup ties to it; the hall changes sit between the pages."""
    for state in ("off", "on", "off"):
        context = Context()
        hass.bus.async_fire(
            "call_service",
            {"domain": "light", "service": f"turn_{state}"},
            context=context,
        )
        hass.states.async_set("light.hall", state)
        hass.states.async_set("light.kitchen", state, context=context)
        await async_wait_recording_done(hass)
    end = dt_util.utcnow()
    filters: dict[str, Any] = {}
    if scope != "device":
        filters["entity_ids"] = ["light.kitchen"]
    if scope != "entity":
        filters["device_ids"] = [kitchen_device]
    native = (
        await component_core_client.send_websocket_message(
            {
                "type": CORE_COMMAND,
                "start_time": (end - timedelta(hours=1)).isoformat(),
                "end_time": end.isoformat(),
                **filters,
            }
        )
    )["result"]
    assert native
    if scope != "device":
        assert any("context_service" in row for row in native)

    params = call_kwargs(
        source="logbook",
        entity_id="light.kitchen" if scope != "device" else None,
        device_id=kitchen_device if scope != "entity" else None,
        end_time=end.isoformat(),
        limit=1,
        order=order,
    )
    tools = LogTools(component_core_client)
    rows: list[dict[str, Any]] = []
    for _ in range(len(native) + 1):
        result = await tools.get_logs(**params)
        assert not component_fallback_warnings(result)
        rows.extend(result["data"]["entries"])
        if not result["data"]["has_more"]:
            break
        params["offset"] = result["data"]["next_offset"]

    def comparable(row: dict[str, Any]) -> dict[str, Any]:
        when = row["when"]
        stamp = (
            when
            if isinstance(when, float)
            else datetime.fromisoformat(when).timestamp()
        )
        # Core's WebSocket rows carry no entity name; the component's do.
        rest = {key: value for key, value in row.items() if key not in ("when", "name")}
        return {**rest, "when": round(stamp, 3)}

    expected = native if order == "oldest" else native[::-1]
    assert [comparable(row) for row in rows] == [comparable(row) for row in expected]


@pytest.mark.usefixtures("logbook")
async def test_a_cursor_is_refused_with_another_scope(
    component_core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    tools = LogTools(component_core_client)
    first = await tools.get_logs(
        **call_kwargs(source="logbook", entity_id="light.kitchen,light.hall", limit=1)
    )
    cursor = first["data"]["next_offset"]
    sent.reset_mock()

    for other in ({"entity_id": "light.hall"}, {}):
        error = await logbook_error(tools, offset=cursor, limit=1, **other)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert COMPONENT_COMMAND not in sent_types(sent)


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize(
    "window", [{"entity_id": "sensor.outdoor"}, {"end_time": "2999-01-01T00:00:00Z"}]
)
async def test_a_read_core_answers_with_nothing_is_an_empty_page(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    window: dict[str, str],
) -> None:
    """Core's logbook leaves a measurement sensor out, and holds nothing yet
    for a window that starts in the future."""
    hass.states.async_set("sensor.outdoor", "5", {"unit_of_measurement": "°C"})
    await async_wait_recording_done(hass)

    result = await LogTools(component_core_client).get_logs(
        **call_kwargs(source="logbook", **window)
    )

    assert not component_fallback_warnings(result)
    assert result["data"]["entries"] == []
    assert result["data"]["total_entries"] == 0
    assert result["data"]["has_more"] is False


@pytest.mark.parametrize("order", ["oldest", "newest"])
async def test_component_receives_the_entity_and_device_filters(
    component_core_client: HomeAssistantClient,
    kitchen_device: str,
    sent: AsyncMock,
    order: str,
) -> None:
    params = call_kwargs(
        source="logbook",
        entity_id="light.kitchen, light.hall",
        device_id=kitchen_device,
        limit=1,
        order=order,
    )
    tools = LogTools(component_core_client)
    with rest_reads(component_core_client) as rest:
        result = await tools.get_logs(**params)
        second = await tools.get_logs(
            **(params | {"offset": result["data"]["next_offset"]})
        )
    rest.assert_not_awaited()
    assert not component_fallback_warnings(result)
    assert not component_fallback_warnings(second)
    assert not second["data"]["has_more"]

    command = sent_command(sent, COMPONENT_COMMAND)
    assert command["entity_ids"] == ["light.kitchen", "light.hall"]
    assert command["device_ids"] == [kitchen_device]
    assert command["page"]["limit"] > 0
    assert set(by_entity(result)) | set(by_entity(second)) == {
        "light.kitchen",
        "light.hall",
    }


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("search", [None, "Kitchen Light"])
async def test_component_that_lost_the_command_falls_back_to_rest(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    sent: AsyncMock,
    search: str | None,
) -> None:
    """An older component answers ``unknown_command``: the read still succeeds
    through Core's REST view."""
    tools = LogTools(component_core_client)
    await tools.get_logs(**call_kwargs(source="logbook"))
    assert COMPONENT_COMMAND in sent_types(sent)

    del hass.data[websocket_api.DOMAIN][COMPONENT_COMMAND]
    sent.reset_mock()
    with rest_reads(component_core_client) as rest:
        result = await tools.get_logs(**call_kwargs(source="logbook", search=search))

    assert COMPONENT_COMMAND in sent_types(sent)
    rest.assert_awaited_once()
    assert component_fallback_warnings(result)
    assert by_entity(result)["light.kitchen"]["name"] == "Kitchen Light"


async def test_component_rejection_is_authoritative(
    component_core_client: HomeAssistantClient,
) -> None:
    """Core's logbook is not set up: the component's refusal reaches the caller
    and the REST view is not tried."""
    with rest_reads(component_core_client) as rest:
        error = await logbook_error(LogTools(component_core_client))

    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "logbook integration is not set up" in error["message"]
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_component_invalid_entity_id_is_a_validation_error(
    component_core_client: HomeAssistantClient,
) -> None:
    with rest_reads(component_core_client) as rest:
        error = await logbook_error(
            LogTools(component_core_client), entity_id="kitchen_light"
        )

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "invalid entity ID" in error["message"]
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_component_timeout_surfaces_without_a_rest_fallback(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorder query still running inside Core is not run a second time."""
    with rest_reads(component_core_client) as rest:
        async with unanswered(hass, COMPONENT_COMMAND, monkeypatch):
            error = await logbook_error(LogTools(component_core_client))

    assert error["code"] == "TIMEOUT_OPERATION"
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("scope", [{}, {"entity_id": "light.kitchen"}])
async def test_dropped_paged_connection_does_not_repeat_a_dense_read(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    scope: dict[str, str],
) -> None:
    socket = await pooled_socket(component_core_client)
    with (
        rest_reads(component_core_client) as rest,
        dropped_on(hass, COMPONENT_COMMAND, socket),
    ):
        error = await logbook_error(LogTools(component_core_client), **scope)
    assert error["message"]
    rest.assert_not_awaited()


@_REFUSED_SOCKET_TIMER
@pytest.mark.usefixtures("logbook", "component_core_client")
async def test_component_rejected_token_ends_in_an_auth_error(
    rejected_client: HomeAssistantClient,
) -> None:
    """The capability probe and the REST fallback both carry the token Core
    refused, so the caller is told about the token."""
    error = await logbook_error(LogTools(rejected_client))

    assert error["code"] == "AUTH_INVALID_TOKEN"


@pytest.mark.usefixtures("logbook")
async def test_without_the_component_rest_rows_carry_names(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    result = await LogTools(core_client).get_logs(**call_kwargs(source="logbook"))

    assert COMPONENT_COMMAND not in sent_types(sent)
    assert CORE_COMMAND not in sent_types(sent)
    assert by_entity(result)["light.kitchen"]["name"] == "Kitchen Light"


async def test_device_filter_reads_cores_websocket_rows_without_names(
    core_client: HomeAssistantClient, kitchen_device: str, sent: AsyncMock
) -> None:
    """Core's WebSocket rows have epoch times and no names: the tool converts
    the times and warns that names are missing."""
    with rest_reads(core_client) as rest:
        result = await LogTools(core_client).get_logs(
            **call_kwargs(
                source="logbook", device_id=kitchen_device, entity_id="light.kitchen"
            )
        )

    assert sent_command(sent, CORE_COMMAND)["device_ids"] == [kitchen_device]
    rest.assert_not_awaited()
    assert set(by_entity(result)) == {"light.kitchen"}
    kitchen = by_entity(result)["light.kitchen"]
    assert datetime.fromisoformat(kitchen["when"]).tzinfo is not None
    assert "name" not in kitchen
    assert any("carry no 'name'" in warning for warning in result["warnings"])
    assert "warnings" not in result["data"]


async def test_device_without_events_has_no_name_warning(
    core_client: HomeAssistantClient, kitchen_device: str
) -> None:
    result = await LogTools(core_client).get_logs(
        **call_kwargs(
            source="logbook", device_id=kitchen_device, entity_id="light.quiet"
        )
    )

    assert result["data"]["entries"] == []
    assert component_fallback_warnings(result)
    assert not any("carry no 'name'" in warning for warning in result["warnings"])


async def test_device_rows_that_carry_a_name_have_no_name_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient, kitchen_device: str
) -> None:
    """A logbook entry (name and message, no state) needs no component names."""
    await hass.services.async_call(
        "logbook",
        "log",
        {"name": "Movie night", "message": "started", "entity_id": "light.porch"},
        blocking=True,
    )
    await async_wait_recording_done(hass)

    result = await LogTools(core_client).get_logs(
        **call_kwargs(
            source="logbook", device_id=kitchen_device, entity_id="light.porch"
        )
    )

    movie = [e for e in result["data"]["entries"] if e.get("message") == "started"]
    assert movie[0]["name"] == "Movie night"
    assert component_fallback_warnings(result)
    assert not any("carry no 'name'" in warning for warning in result["warnings"])


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("lowercase", [False, True])
async def test_context_filter_reads_the_rest_view(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    lowercase: bool,
) -> None:
    context = Context()
    hass.states.async_set("light.porch", "off")
    hass.states.async_set("light.porch", "on", context=context)
    await async_wait_recording_done(hass)

    context_id = context.id.lower() if lowercase else context.id
    with rest_reads(core_client) as rest:
        result = await LogTools(core_client).get_logs(
            **call_kwargs(source="logbook", hours_back=2, context_id=context_id)
        )

    assert result["data"]["context_filter"] == context_id
    assert set(by_entity(result)) == {"light.porch"}
    assert rest.await_args_list[-1].kwargs["context_id"] == context_id
    assert CORE_COMMAND not in sent_types(sent)


@pytest.mark.usefixtures("logbook")
async def test_device_filter_without_the_websocket_is_a_connection_error(
    core_client: HomeAssistantClient,
) -> None:
    """No REST transport has a device filter, so a dead WebSocket is surfaced
    instead of read as "no entries"."""
    with (
        rest_reads(core_client) as rest,
        patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("no WS")),
        ),
    ):
        error = await logbook_error(LogTools(core_client), device_id="dev1")

    assert error["code"] == "CONNECTION_FAILED"
    assert "Last error: no WS" in error["message"]
    rest.assert_not_awaited()


@_REFUSED_SOCKET_TIMER
@pytest.mark.usefixtures("logbook")
async def test_device_filter_with_a_rejected_token_is_an_auth_error(
    rejected_client: HomeAssistantClient,
) -> None:
    with rest_reads(rejected_client) as rest:
        error = await logbook_error(LogTools(rejected_client), device_id="dev1")

    assert error["code"] == "AUTH_INVALID_TOKEN"
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_device_query_timeout_is_not_reported_as_a_missing_websocket(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with rest_reads(core_client) as rest:
        async with unanswered(hass, CORE_COMMAND, monkeypatch):
            error = await logbook_error(LogTools(core_client), device_id="dev1")

    assert error["code"] == "TIMEOUT_OPERATION"
    rest.assert_not_awaited()


async def test_device_filter_when_core_has_no_logbook_is_not_an_empty_success(
    core_client: HomeAssistantClient,
) -> None:
    """Core answers ``unknown_command`` for ``logbook/get_events``."""
    with rest_reads(core_client) as rest:
        error = await logbook_error(LogTools(core_client), device_id="dev1")

    assert error["code"] == "CONNECTION_FAILED"
    assert "device_id" in error["message"]
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("hours_back", [None, 125])
async def test_invalid_context_cannot_turn_into_an_unfiltered_read(
    component_core_client: HomeAssistantClient, sent: AsyncMock, hours_back: int | None
) -> None:
    with rest_reads(component_core_client) as rest:
        error = await logbook_error(
            LogTools(component_core_client),
            hours_back=hours_back,
            context_id="invalid-context",
        )
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert COMPONENT_COMMAND not in sent_types(sent)
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("order", ["oldest", "newest"])
@pytest.mark.parametrize("component_enabled", [False, True])
async def test_a_search_over_several_hours_counts_every_match(
    hass: HomeAssistant,
    component_enabled: bool,
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    order: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools.websocket_api import (
        async_register_commands,
        logbook_page,
    )

    if component_enabled:
        async_register_commands(hass)

    end = dt_util.utcnow().replace(microsecond=0)
    seam = (end - timedelta(hours=1)).timestamp()
    times = [seam - 3601, seam - 0.000001, seam, seam, seam + 0.000001, seam + 30]
    names = [f"range marker {i}" for i in range(len(times))]
    for name, stamp in zip(names, times, strict=True):
        hass.bus.async_fire(
            "logbook_entry", {"name": name, "message": "recorded"}, time_fired=stamp
        )
    await async_wait_recording_done(hass)
    monkeypatch.setattr(logbook_page, "RAW_PAGE_ROWS", 2)
    params = call_kwargs(
        source="logbook",
        hours_back=3,
        search="range marker",
        order=order,
        end_time=end.isoformat(),
        limit=2,
    )
    actual: list[str] = []
    with rest_reads(core_client) as rest:
        for _ in range(len(names)):
            data = (await LogTools(core_client).get_logs(**params))["data"]
            assert data["total_entries"] == len(names)
            assert data["offset"] == len(actual)
            actual.extend(row["name"] for row in data["entries"])
            if not data["has_more"]:
                break
            params["offset"] = data["next_offset"]
            if isinstance(params["offset"], str):
                params["hours_back"] = None
                params["end_time"] = None
    assert actual == (names if order == "oldest" else names[::-1])
    if component_enabled:
        rest.assert_not_awaited()
        commands = [
            call.args[0]
            for call in sent.await_args_list
            if call.args[0]["type"] == COMPONENT_COMMAND
        ]
        assert all("page" in cmd for cmd in commands)
    else:
        assert len(rest.await_args_list) == (len(names) + 1) // 2
        for call in rest.await_args_list:
            start = datetime.fromisoformat(call.kwargs["start_time"])
            finish = datetime.fromisoformat(call.kwargs["end_time"])
            assert finish - start == timedelta(hours=3)


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("order", ["oldest", "newest"])
async def test_phrase_pages_fill_across_raw_batches(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    from custom_components.ha_mcp_tools.websocket_api import logbook_page

    monkeypatch.setattr(logbook_page, "RAW_PAGE_ROWS", 2)
    stamp = dt_util.utcnow().timestamp() - 60
    for i in range(12):
        hass.bus.async_fire(
            "logbook_entry",
            {"name": "Home Assistant" if i % 3 == 0 else "noise", "message": "started"},
            time_fired=stamp + i,
        )
    await async_wait_recording_done(hass)
    params = call_kwargs(
        source="logbook", search="Home Assistant started", limit=3, order=order
    )
    tools = LogTools(component_core_client)
    first = (await tools.get_logs(**params))["data"]
    pages = [
        c.args[0]["page"]
        for c in sent.await_args_list
        if c.args[0]["type"] == COMPONENT_COMMAND
    ]
    assert len(pages) > 1
    assert all("after" in page for page in pages[1:])
    assert first["total_entries"] == 4
    assert first["returned_entries"] == 3
    sent.reset_mock()
    last = (await tools.get_logs(**(params | {"offset": first["next_offset"]})))["data"]
    assert last["returned_entries"] == 1
    assert not last["has_more"]
    times = [
        datetime.fromisoformat(row["when"])
        for row in first["entries"] + last["entries"]
    ]
    expected = [datetime.fromtimestamp(stamp + i, UTC) for i in (0, 3, 6, 9)]
    assert times == sorted(expected, reverse=order == "newest")


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("change", ["rename", "purge"])
async def test_a_search_cursor_continues_after_the_logbook_changed(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    change: str,
) -> None:
    entities = [f"light.selection_{i}" for i in range(4)]
    for entity in entities:
        for state in ("off", "on"):
            hass.states.async_set(entity, state, {"friendly_name": "selection race"})
    await async_wait_recording_done(hass)
    params = call_kwargs(
        source="logbook", order="oldest", limit=2, search="selection race"
    )
    tools = LogTools(component_core_client)
    first = (await tools.get_logs(**params))["data"]
    assert first["total_entries"] == 4
    returned = {row["entity_id"] for row in first["entries"]}
    remaining = sorted(set(entities) - returned)
    if change == "rename":
        for entity in remaining:
            hass.states.async_set(entity, "on", {"friendly_name": "renamed"})
    else:
        await hass.services.async_call(
            "recorder", "purge_entities", {"entity_id": remaining}, blocking=True
        )
    await async_wait_recording_done(hass)
    last = (await tools.get_logs(**(params | {"offset": first["next_offset"]})))["data"]
    assert last["entries"] == []
    assert not last["has_more"]


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("order", ["oldest", "newest"])
async def test_deep_numeric_offset_inside_one_tied_timestamp(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, order: str
) -> None:
    stamp = dt_util.utcnow().timestamp() - 10
    names = [f"tied match {i}" for i in range(520)]
    for name in names:
        hass.bus.async_fire(
            "logbook_entry", {"name": name, "message": "offset"}, time_fired=stamp
        )
    await async_wait_recording_done(hass)
    params = call_kwargs(
        source="logbook", search="tied match", order=order, offset=508, limit=8
    )
    tools = LogTools(component_core_client)
    first = (await tools.get_logs(**params))["data"]
    second = (await tools.get_logs(**(params | {"offset": first["next_offset"]})))[
        "data"
    ]
    expected = names if order == "oldest" else list(reversed(names))
    assert first["total_entries"] == 520
    assert first["offset"] == 508
    assert [r["name"] for r in first["entries"]] == expected[508:516]
    assert [r["name"] for r in second["entries"]] == expected[516:]
    assert not second["has_more"]


@pytest.mark.usefixtures("logbook")
async def test_cursor_reads_matches_committed_after_the_initial_count(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
) -> None:
    # Only a window that ends in the future can still gain matches.
    end = dt_util.utcnow() + timedelta(minutes=2)
    start = dt_util.utcnow().timestamp() - 60

    def record(name: str, offset: int) -> None:
        hass.bus.async_fire(
            "logbook_entry",
            {"name": name, "message": "late-count fixture"},
            time_fired=start + offset,
        )

    record("first", 1)
    record("second", 2)
    await async_wait_recording_done(hass)
    params = call_kwargs(
        source="logbook",
        order="oldest",
        end_time=end.isoformat(),
        search="late-count fixture",
        limit=1,
    )
    tools = LogTools(component_core_client)
    first = (await tools.get_logs(**params))["data"]
    assert first["total_entries"] == 2
    assert [row["name"] for row in first["entries"]] == ["first"]
    record("late", 3)
    await async_wait_recording_done(hass)
    params["offset"] = first["next_offset"]
    with patch("ha_mcp.tools.log_sources_logbook.datetime", wraps=datetime) as clock:
        clock.now.return_value = end + timedelta(seconds=1)
        second = (await tools.get_logs(**params))["data"]
    assert second["total_entries"] == 2
    assert [row["name"] for row in second["entries"]] == ["second"]
    assert second["has_more"]
    params["offset"] = second["next_offset"]
    last = (await tools.get_logs(**params))["data"]
    assert [row["name"] for row in last["entries"]] == ["late"]
    assert last["total_entries"] == 2


@pytest.mark.usefixtures("logbook")
async def test_oversized_component_reply_never_repeats_the_read_over_rest(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await hass.services.async_call(
        "logbook", "log", {"name": "Size test", "message": "m" * 12000}, blocking=True
    )
    await async_wait_recording_done(hass)
    monkeypatch.setattr(websocket_client, "MAX_WS_MESSAGE_BYTES", 8192)
    with rest_reads(component_core_client) as rest:
        error = await logbook_error(LogTools(component_core_client))

    assert "too large" in error["message"].lower()
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("order", ["oldest", "newest"])
@pytest.mark.parametrize(("limit", "page_bytes"), [(3, None), (10, 8192)])
async def test_dense_logbook_pages_return_each_tied_event_once(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    order: str,
    limit: int,
    page_bytes: int | None,
) -> None:
    """With a byte budget at the transport limit, a page of large entries ends
    early instead of closing the WebSocket, and the cursor still visits each once."""
    from custom_components.ha_mcp_tools.websocket_api import logbook_page

    if page_bytes is not None:
        monkeypatch.setattr(logbook_page, "PAGE_BYTES", page_bytes)
    stamp = dt_util.utcnow().timestamp() - 5
    names = [f"page marker {i:02d}" for i in range(41)]
    for name in names:
        hass.bus.async_fire(
            "logbook_entry",
            {"name": name, "message": "x" * 1800},
            time_fired=stamp,
        )
    await async_wait_recording_done(hass)
    monkeypatch.setattr(websocket_client, "MAX_WS_MESSAGE_BYTES", 8192)
    params = call_kwargs(
        source="logbook", limit=limit, order=order, search="page marker"
    )
    actual: list[str] = []
    with rest_reads(component_core_client) as rest:
        for _ in range(len(names)):
            data = (await LogTools(component_core_client).get_logs(**params))["data"]
            if data["total_entries"] is None:
                assert 0 < data["total_entries_lower_bound"] <= len(names)
            else:
                assert data["total_entries"] == len(names)
            assert len(data["entries"]) <= limit
            actual.extend(row["name"] for row in data["entries"])
            if not data["has_more"]:
                break
            params["offset"] = data["next_offset"]
        else:
            pytest.fail("The last page must terminate")
    assert actual == (names if order == "oldest" else names[::-1])
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("order", ["oldest", "newest"])
@pytest.mark.parametrize("component_enabled", [False, True])
async def test_sparse_search_across_context_and_page_boundaries(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    component_enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    from custom_components.ha_mcp_tools.websocket_api import (
        async_register_commands,
        logbook_page,
    )

    if component_enabled:
        async_register_commands(hass)

    end = dt_util.utcnow().replace(microsecond=0)
    stamp = (end - timedelta(hours=1, seconds=3)).timestamp()
    parent = Context()
    hass.bus.async_fire(
        "call_service",
        {"domain": "light", "service": "turn_on", "service_data": {}},
        context=parent,
        time_fired=stamp,
    )
    for i in range(6):
        hass.bus.async_fire(
            "logbook_entry",
            {"name": "sparse child" if i == 3 else "noise", "message": "test"},
            context=Context(parent_id=parent.id),
            time_fired=stamp + i + 1,
        )
    await async_wait_recording_done(hass)
    params = call_kwargs(
        source="logbook",
        hours_back=2,
        end_time=end.isoformat(),
        order=order,
        search="sparse child",
        compact=False,
    )
    # Core's unpaged REST renderer is the reference for every field.
    native = (
        await core_client.get_logbook(
            start_time=dt_util.utc_from_timestamp(stamp - 1).isoformat()
        )
    ) or []
    expected = [entry for entry in native if entry.get("name") == "sparse child"]
    assert len(expected) == 1 and expected[0]["context_service"] == "turn_on"
    monkeypatch.setattr(logbook_page, "RAW_PAGE_ROWS", 2)
    actual = []
    pages = 0
    with rest_reads(core_client) as rest:
        for _ in range(20):
            result = await LogTools(core_client).get_logs(**params)
            pages += 1
            data = result["data"]
            actual.extend(data["entries"])
            if not data["has_more"]:
                assert data["total_entries"] == 1
                break
            assert data["entries"]
            assert data["total_entries"] == 1
            params["offset"] = data["next_offset"]
        else:
            pytest.fail("Sparse search did not terminate")
    # The page holding the only match says nothing more follows.
    assert pages == 1
    if not component_enabled:
        assert component_fallback_warnings(result)
        rest.assert_awaited_once()
    for entry in actual + expected:
        entry["when"] = datetime.fromisoformat(entry["when"])
    assert actual == expected
    if component_enabled:
        rest.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"), [("entity_id", ", "), ("device_id", " , ")]
)
async def test_blank_filter_is_rejected_before_any_read(
    core_client: HomeAssistantClient, sent: AsyncMock, field: str, value: str
) -> None:
    with rest_reads(core_client) as rest:
        error = await logbook_error(LogTools(core_client), **{field: value})

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert COMPONENT_COMMAND not in sent_types(sent)
    assert CORE_COMMAND not in sent_types(sent)
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_rest_read_restricts_to_the_listed_entities(
    core_client: HomeAssistantClient,
) -> None:
    """The positive twin of the blank-filter rejection: listed ids are read."""
    with rest_reads(core_client) as rest:
        result = await LogTools(core_client).get_logs(
            **call_kwargs(source="logbook", entity_id="light.kitchen, light.hall")
        )

    assert rest.await_args_list[-1].kwargs["entity_id"] == "light.kitchen,light.hall"
    assert set(by_entity(result)) == {"light.kitchen", "light.hall"}


@pytest.mark.usefixtures("logbook")
async def test_rest_read_of_a_malformed_entity_id_suggests_the_id_form(
    core_client: HomeAssistantClient,
) -> None:
    """Core's REST view fails on a malformed id with a 500: the hint names the
    input shape and the error log."""
    error = await logbook_error(LogTools(core_client), entity_id="kitchen_light")

    suggestions = error["suggestions"]
    assert any("domain.object_id" in s for s in suggestions)
    assert any("ha_get_logs(source='error_log')" in s for s in suggestions)


@pytest.mark.usefixtures("logbook")
async def test_rest_read_that_gets_no_answer_keeps_the_scope_hints(
    core_client: HomeAssistantClient,
) -> None:
    with patch.object(
        core_client,
        "get_logbook",
        AsyncMock(side_effect=HomeAssistantConnectionError("boom")),
    ):
        error = await logbook_error(LogTools(core_client))

    assert any("hours_back" in s for s in error["suggestions"])


@pytest.mark.usefixtures("logbook")
async def test_omitted_end_time_covers_the_requested_window_up_to_now(
    core_client: HomeAssistantClient,
) -> None:
    before = dt_util.utcnow()
    result = await LogTools(core_client).get_logs(
        **call_kwargs(
            source="logbook", entity_id="light.kitchen,light.hall", hours_back=24
        )
    )

    start = datetime.fromisoformat(result["data"]["start_time"])
    end = datetime.fromisoformat(result["data"]["end_time"])
    assert end >= before
    assert end - start == timedelta(hours=24)


@pytest.mark.usefixtures("logbook")
async def test_explicit_end_time_honors_the_callers_window(
    core_client: HomeAssistantClient,
) -> None:
    """The rows are recorded now, so a window that ended earlier has none and
    one that ends later has them."""
    tools = LogTools(core_client)
    past = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    future = (dt_util.utcnow() + timedelta(hours=1)).isoformat()

    earlier = await tools.get_logs(
        **call_kwargs(
            source="logbook",
            entity_id="light.kitchen,light.hall",
            hours_back=24,
            end_time=past,
        )
    )
    later = await tools.get_logs(
        **call_kwargs(
            source="logbook",
            entity_id="light.kitchen,light.hall",
            hours_back=24,
            end_time=future,
        )
    )

    start = datetime.fromisoformat(earlier["data"]["start_time"])
    end = datetime.fromisoformat(earlier["data"]["end_time"])
    assert end - start == timedelta(hours=24)
    assert earlier["data"]["total_entries"] == 0
    assert "light.kitchen" in by_entity(later)


@pytest.mark.usefixtures("logbook")
async def test_omitted_and_explicit_end_time_return_the_same_rows(
    core_client: HomeAssistantClient,
) -> None:
    """Passing back the end the tool reported reads the same window (#2767)."""
    tools = LogTools(core_client)
    omitted = await tools.get_logs(
        **call_kwargs(
            source="logbook", entity_id="light.kitchen,light.hall", hours_back=24
        )
    )
    explicit = await tools.get_logs(
        **call_kwargs(
            source="logbook",
            entity_id="light.kitchen,light.hall",
            hours_back=24,
            end_time=omitted["data"]["end_time"],
        )
    )

    assert explicit["data"]["start_time"] == omitted["data"]["start_time"]
    assert explicit["data"]["entries"] == omitted["data"]["entries"]
    assert omitted["data"]["total_entries"] >= 2


@pytest.mark.usefixtures("logbook")
async def test_pagination_hint_keeps_pages_in_the_same_window(
    core_client: HomeAssistantClient,
) -> None:
    result = await LogTools(core_client).get_logs(
        **call_kwargs(source="logbook", limit=1)
    )

    hint = result["data"]["pagination_hint"]
    assert f"end_time='{result['data']['end_time']}'" in hint
