"""Public logbook paging contracts against Core's recorder."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import LogTools

from ..e2e.utilities.component_path import component_fallback_warnings
from ._log_tool_calls import call_kwargs
from ._logbook_test_support import _recorder as _recorder
from ._logbook_test_support import logbook as logbook
from ._logbook_test_support import logbook_error, rest_reads, sent_types


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("component_enabled", [False, True])
async def test_action_calls_use_cores_admin_view(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    component_enabled: bool,
    sent: AsyncMock,
) -> None:
    from custom_components.ha_mcp_tools.websocket_api import async_register_commands

    if component_enabled:
        async_register_commands(hass)
    await hass.services.async_call(
        "logbook", "log", {"name": "action proof", "message": "record"}, blocking=True
    )
    await async_wait_recording_done(hass)
    with rest_reads(core_client) as rest:
        result = await LogTools(core_client).get_logs(**call_kwargs(action_calls=True))
    entries = result["data"]["entries"]
    assert any(e.get("service_data", {}).get("name") == "action proof" for e in entries)
    assert bool(component_fallback_warnings(result)) != component_enabled
    assert ("logbook/get_action_calls" in sent_types(sent)) != component_enabled
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("component_enabled", [False, True])
async def test_search_finds_an_action_call_by_its_action(
    hass: HomeAssistant, core_client: HomeAssistantClient, component_enabled: bool
) -> None:
    from custom_components.ha_mcp_tools.websocket_api import async_register_commands

    if component_enabled:
        async_register_commands(hass)
    await hass.services.async_call(
        "logbook", "log", {"name": "action proof", "message": "record"}, blocking=True
    )
    await async_wait_recording_done(hass)
    tools = LogTools(core_client)

    found = await tools.get_logs(**call_kwargs(action_calls=True, search="logbook.log"))
    missed = await tools.get_logs(**call_kwargs(action_calls=True, search="no.such"))

    assert [row["service"] for row in found["data"]["entries"]] == ["log"]
    assert missed["data"]["entries"] == []


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("component_enabled", [False, True])
async def test_action_calls_with_a_filter_are_refused_without_an_entity_id_hint(
    hass: HomeAssistant, core_client: HomeAssistantClient, component_enabled: bool
) -> None:
    from custom_components.ha_mcp_tools.websocket_api import async_register_commands

    if component_enabled:
        async_register_commands(hass)

    error = await logbook_error(
        LogTools(core_client), action_calls=True, entity_id="light.kitchen"
    )

    hints = [error.get("suggestion", ""), *error.get("suggestions", [])]
    assert hints != [""]
    assert "object_id" not in " ".join(hints)


async def test_action_calls_on_another_source_are_named_as_ignored(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "system_log", {})

    result = await LogTools(core_client).get_logs(
        **call_kwargs(source="system", action_calls=True)
    )

    assert any("action_calls" in warning for warning in result["warnings"])


@pytest.mark.usefixtures("logbook")
async def test_a_statement_core_shapes_differently_is_refused_not_paged(
    component_core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another Core version's query: one select where the reader knows unions."""
    from sqlalchemy import literal, select

    from custom_components.ha_mcp_tools.websocket_api import logbook_page

    monkeypatch.setattr(
        logbook_page,
        "statement_for_request",
        lambda *args, **kwargs: select(literal(1).label("row_id")),
    )
    with rest_reads(component_core_client) as rest:
        error = await logbook_error(LogTools(component_core_client))

    assert "statement shape" in error["message"]
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_a_device_and_context_conflict_points_at_the_filters(
    core_client: HomeAssistantClient,
) -> None:
    """Core's own WebSocket read fails this combination without saying why."""
    error = await logbook_error(
        LogTools(core_client), device_id="any-device", context_id=Context().id
    )

    assert any("context_id" in hint for hint in error["suggestions"])


@pytest.mark.usefixtures("logbook")
async def test_long_native_window_is_refused_before_read(
    core_client: HomeAssistantClient,
) -> None:
    with rest_reads(core_client) as rest:
        error = await logbook_error(LogTools(core_client), hours_back=720)
    assert "ha_mcp_tools" in error["message"]
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_tied_state_and_event_follow_native_order(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    state = hass.states.get("light.kitchen")
    assert state
    hass.bus.async_fire(
        "logbook_entry",
        {"name": "tie", "message": "marker"},
        time_fired=state.last_updated_timestamp,
    )
    await async_wait_recording_done(hass)
    end = dt_util.utcnow()
    native = await component_core_client.get_logbook(
        start_time=(end - timedelta(hours=1)).isoformat(), end_time=end.isoformat()
    )
    assert native
    params = call_kwargs(end_time=end.isoformat(), limit=1, order="oldest")
    rows = []
    tools = LogTools(component_core_client)
    for _ in range(len(native)):
        page = (await tools.get_logs(**params))["data"]
        rows.extend(page["entries"])
        if not page["has_more"]:
            break
        params["offset"] = page["next_offset"]
    for row in rows + native:
        row["when"] = datetime.fromisoformat(row["when"])
    assert rows == native


@pytest.mark.usefixtures("logbook")
async def test_a_cursor_reaches_rows_recorded_after_its_first_page(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The recorder commits a few seconds behind: rows fired before the first
    page can reach the database after it."""
    for name in ("one", "two", "three", "four"):
        hass.bus.async_fire("logbook_entry", {"name": name, "message": "probe"})
    await async_wait_recording_done(hass)
    late = dt_util.utcnow().timestamp()
    tools = LogTools(component_core_client)
    params = call_kwargs(search="probe", limit=2, order="oldest")
    page = (await tools.get_logs(**params))["data"]
    names = [row["name"] for row in page["entries"]]
    assert page["total_entries"] == 4

    for name in ("five", "six"):
        hass.bus.async_fire(
            "logbook_entry", {"name": name, "message": "probe"}, time_fired=late
        )
    await async_wait_recording_done(hass)
    while page["has_more"]:
        page = (await tools.get_logs(**params, offset=page["next_offset"]))["data"]
        names += [row["name"] for row in page["entries"]]

    assert names == ["one", "two", "three", "four", "five", "six"]
    assert page["total_entries"] == 6


@pytest.mark.usefixtures("logbook")
async def test_partial_count_continues_after_empty_search_page(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    sent: AsyncMock,
) -> None:
    from custom_components.ha_mcp_tools.websocket_api import logbook_page
    from ha_mcp.tools import logbook_cursor

    monkeypatch.setattr(logbook_page, "RAW_PAGE_ROWS", 1)
    monkeypatch.setattr(logbook_cursor, "MAX_PAGE_READS", 1)
    for name in ("noise", "target", "target"):
        hass.bus.async_fire("logbook_entry", {"name": name, "message": "record"})
    await async_wait_recording_done(hass)
    params = call_kwargs(hours_back=720, search="target", limit=1, order="oldest")
    tools = LogTools(component_core_client)
    entries = []
    for index in range(12):
        sent.reset_mock()
        result = await tools.get_logs(**params)
        assert sent_types(sent).count("ha_mcp_tools/logbook_events") == 1
        data = result["data"]
        if index == 0:
            assert data["entries"] == []
            assert data["total_entries"] is None
            assert data["total_entries_lower_bound"] == 0
        entries.extend(data["entries"])
        if not data["has_more"]:
            assert data["total_entries"] == len(entries) == 2
            break
        params["offset"] = data["next_offset"]
    else:
        pytest.fail("The bounded search did not reach the final match")


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("component_enabled", [False, True])
async def test_context_conflict_reaches_native_reader(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    component_enabled: bool,
    sent: AsyncMock,
) -> None:
    from custom_components.ha_mcp_tools.websocket_api import async_register_commands

    if component_enabled:
        async_register_commands(hass)
    with rest_reads(core_client) as rest:
        error = await logbook_error(
            LogTools(core_client), entity_id="light.kitchen", context_id=Context().id
        )
    assert error["code"] == (
        "SERVICE_CALL_FAILED" if component_enabled else "VALIDATION_FAILED"
    )
    if component_enabled:
        assert "ha_mcp_tools/logbook_events" in sent_types(sent)
        rest.assert_not_awaited()
    else:
        rest.assert_awaited_once()


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("component_enabled", [False, True])
async def test_search_does_not_match_null_names(
    hass: HomeAssistant, core_client: HomeAssistantClient, component_enabled: bool
) -> None:
    from custom_components.ha_mcp_tools.websocket_api import async_register_commands

    if component_enabled:
        async_register_commands(hass)
    hass.bus.async_fire("logbook_entry", {"name": None, "message": "null marker"})
    await async_wait_recording_done(hass)
    tools = LogTools(core_client)
    marker = await tools.get_logs(**call_kwargs(search="null marker"))
    assert marker["data"]["entries"][0].get("name") is None
    result = await tools.get_logs(**call_kwargs(search="none"))
    assert result["data"]["entries"] == []
