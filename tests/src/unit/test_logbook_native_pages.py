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
    assert "context_id" in error["message"]
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
