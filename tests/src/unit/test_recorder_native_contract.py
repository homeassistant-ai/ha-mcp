"""Native recorder fields and options survive ``ha_get_history``.

States are recorded by Core's recorder at fixed timestamps and every row the
tool shapes is the one Core's ``history/history_during_period`` returns.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder import history as recorder_history
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.tools_history import HistoryTools

from ._client_spies import sent_messages
from ._recorded_history import record_states

ENTITY_ID = "light.test"
NOVEMBER = 1700000000.25  # 2023-11-14, New York on EST (-05:00)
JULY = 1719856800.0  # 2024-07-01, New York on EDT (-04:00)
COMPACT_KEYS = {"s", "a", "lu", "lc"}


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


async def _history(
    client: HomeAssistantClient, start: float, end: float, **kwargs: Any
) -> dict[str, Any]:
    result: dict[str, Any] = await HistoryTools(client).ha_get_history(
        entity_ids=[ENTITY_ID],
        start_time=_iso(start),
        end_time=_iso(end),
        order="asc",
        **kwargs,
    )
    return result


def _history_requests(sent: AsyncMock) -> list[dict[str, Any]]:
    return [
        call.args[0]
        for call in sent.await_args_list
        if call.args[0]["type"] == "history/history_during_period"
    ]


@pytest.fixture
async def history(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """Core's history API on a recorder holding light.test's changes: on, an
    attribute-only update, then off."""
    assert await async_setup_component(hass, "history", {})
    await record_states(
        hass,
        ENTITY_ID,
        ("on", {"friendly_name": "Test"}, NOVEMBER),
        ("on", {"friendly_name": "Test", "brightness": 3}, NOVEMBER + 60),
        ("off", {"friendly_name": "Test", "brightness": 3}, NOVEMBER + 120),
    )


@pytest.mark.usefixtures("history")
@pytest.mark.parametrize("minimal", [True, False])
async def test_history_rows_use_readable_names_and_keep_cores_values(
    core_client: HomeAssistantClient, minimal: bool
) -> None:
    """Each compact key Core sends is renamed once and keeps its value; an
    omitted lc falls back to lu, as Core's own expansion does."""
    with sent_messages(core_client) as sent:
        result = await _history(
            core_client,
            NOVEMBER - 60,
            NOVEMBER + 180,
            minimal_response=minimal,
            significant_changes_only=False,
        )
    [request] = _history_requests(sent)
    core_rows = (await core_client.send_websocket_message(request))["result"][ENTITY_ID]
    rows = result["data"]["entities"][0]["states"]

    assert len(rows) == len(core_rows) > 1
    for row, core_row in zip(rows, core_rows, strict=True):
        assert not COMPACT_KEYS & row.keys()
        assert row["state"] == core_row["s"]
        assert row.get("attributes") == core_row.get("a")
        assert datetime.fromisoformat(row["last_updated"]).timestamp() == core_row["lu"]
        assert datetime.fromisoformat(row["last_changed"]).timestamp() == core_row.get(
            "lc", core_row["lu"]
        )
    if not minimal:
        # The attribute-only update: Core sends lc apart from lu.
        assert any("lc" in core_row for core_row in core_rows)


@pytest.mark.usefixtures("history")
async def test_history_rows_keep_a_field_a_later_core_adds(
    core_client: HomeAssistantClient,
) -> None:
    """A row field this Core does not send yet reaches the caller unchanged
    instead of being dropped by the renaming."""
    core_rows = recorder_history.get_significant_states

    def with_future_field(*args: Any, **kwargs: Any) -> dict[str, list[Any]]:
        rows: dict[str, list[Any]] = core_rows(*args, **kwargs)
        return {
            entity_id: [{**row, "future": {"opaque": [1]}} for row in states]
            for entity_id, states in rows.items()
        }

    with patch.object(
        recorder_history, "get_significant_states", side_effect=with_future_field
    ):
        result = await _history(core_client, NOVEMBER - 60, NOVEMBER + 180)

    rows = result["data"]["entities"][0]["states"]
    assert len(rows) > 1
    assert all(row["future"] == {"opaque": [1]} for row in rows)


@pytest.mark.usefixtures("history")
@pytest.mark.parametrize("key", ["type", "entity_ids", "no_attributes"])
async def test_native_options_cannot_bypass_query_guards(
    core_client: HomeAssistantClient, key: str
) -> None:
    with sent_messages(core_client) as sent, pytest.raises(ToolError) as raised:
        await _history(
            core_client, NOVEMBER - 60, NOVEMBER + 180, core_options={key: "override"}
        )

    assert "protected" in json.loads(str(raised.value))["error"]["message"]
    assert _history_requests(sent) == []


@pytest.mark.usefixtures("history")
async def test_native_option_the_tool_does_not_name_reaches_core(
    core_client: HomeAssistantClient,
) -> None:
    """Core's include_start_time_state has no tool parameter: it still applies."""
    window = (NOVEMBER + 90, NOVEMBER + 180)
    with_start = await _history(core_client, *window)
    with sent_messages(core_client) as sent:
        without_start = await _history(
            core_client, *window, core_options={"include_start_time_state": False}
        )

    [request] = _history_requests(sent)
    assert request["include_start_time_state"] is False

    def states(result: dict[str, Any]) -> list[str]:
        return [row["state"] for row in result["data"]["entities"][0]["states"]]

    assert states(with_start) == ["on", "off"]
    assert states(without_start) == ["off"]


@pytest.mark.parametrize(
    ("timestamp", "fetch_failed", "offset"),
    [
        (NOVEMBER, False, timedelta(hours=-5)),
        (JULY, False, timedelta(hours=-4)),
        (NOVEMBER, True, timedelta(0)),
    ],
    ids=["standard-time", "daylight-time", "timezone-unavailable"],
)
async def test_history_times_are_in_has_timezone_and_attributes_are_untouched(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    timestamp: float,
    fetch_failed: bool,
    offset: timedelta,
) -> None:
    """Event times are HA-local (UTC when HA's timezone cannot be read); an
    attribute that looks like a timestamp stays as Core sent it."""
    await hass.config.async_set_time_zone("America/New_York")
    assert await async_setup_component(hass, "history", {})
    attributes = {"last_updated": "2026-01-01T00:00:00Z"}
    await record_states(hass, ENTITY_ID, ("on", attributes, timestamp))
    if fetch_failed:
        monkeypatch.setattr(
            core_client,
            "get_config",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection refused")),
        )

    result = await _history(
        core_client, timestamp - 60, timestamp + 60, minimal_response=False
    )

    [row] = result["data"]["entities"][0]["states"]
    for key in ("last_updated", "last_changed"):
        shown = datetime.fromisoformat(row[key])
        assert shown.timestamp() == timestamp
        assert shown.utcoffset() == offset
    assert row["attributes"] == attributes
    assert result["metadata"]["timestamp_format"].startswith("ISO 8601")
    timezone_warnings = [
        warning
        for warning in result.get("warnings", [])
        if "Could not fetch Home Assistant timezone" in warning
    ]
    assert bool(timezone_warnings) is fetch_failed
