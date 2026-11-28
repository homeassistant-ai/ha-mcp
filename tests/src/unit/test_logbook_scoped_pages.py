"""Scoped Logbook pages through the component, against Core's own rows."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import LogTools

from ..e2e.utilities.component_path import component_fallback_warnings
from ._log_tool_calls import call_kwargs
from ._logbook_test_support import _recorder as _recorder
from ._logbook_test_support import kitchen_device as kitchen_device
from ._logbook_test_support import logbook as logbook
from ._logbook_test_support import logbook_error, sent_types

COMPONENT_COMMAND = "ha_mcp_tools/logbook_events"
CORE_COMMAND = "logbook/get_events"


@pytest.mark.parametrize("order", ["oldest", "newest"])
@pytest.mark.parametrize("scope", ["entity", "both"])
async def test_scoped_pages_are_cores_own_rows_with_what_caused_them(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    kitchen_device: str,
    scope: str,
    order: str,
) -> None:
    """A user's action call causes each kitchen change through a child
    context, which only Core's lookups tie together; the last change has no
    cause but itself."""
    for state in ("off", "on", "off"):
        action = Context(user_id=uuid4().hex)
        hass.bus.async_fire(
            "call_service",
            {"domain": "light", "service": f"turn_{state}"},
            context=action,
        )
        hass.states.async_set("light.hall", state)
        hass.states.async_set(
            "light.kitchen", state, context=Context(parent_id=action.id)
        )
        await async_wait_recording_done(hass)
    hass.states.async_set("light.kitchen", "on")
    await async_wait_recording_done(hass)
    end = dt_util.utcnow()
    filters: dict[str, Any] = {"entity_ids": ["light.kitchen"]}
    if scope == "both":
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
    assert any("context_user_id" in row for row in native)

    params = call_kwargs(
        source="logbook",
        entity_id="light.kitchen",
        device_id=kitchen_device if scope == "both" else None,
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
