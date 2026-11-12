"""ha_get_history's time window, and the parser behind its time parameters."""

import json
import re
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_history
from ha_mcp.tools.tools_history import RELATIVE_TIME_UNIT_SECONDS, parse_relative_time

NOW = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the history module's current time NOW.

    Only the module's own clock: Core's ``freezer`` would also stop the event
    loop the client runs on.
    """

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(NOW.timestamp(), tz)

    monkeypatch.setattr(tools_history, "datetime", FrozenDateTime)


async def _window_sent_to_core(
    hass: HomeAssistant, core_client: HomeAssistantClient, **kwargs: Any
) -> tuple[datetime, datetime]:
    """The start and end ha_get_history asks Core's history command for."""
    assert await async_setup_component(hass, "history", {})
    with patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(wraps=core_client.send_websocket_message),
    ) as send:
        await tools_history.HistoryTools(core_client).ha_get_history(
            entity_ids="sensor.temp", **kwargs
        )
    (message,) = [
        c.args[0]
        for c in send.call_args_list
        if c.args[0]["type"] == "history/history_during_period"
    ]
    return (
        datetime.fromisoformat(message["start_time"]),
        datetime.fromisoformat(message["end_time"]),
    )


@pytest.mark.usefixtures("frozen_now")
async def test_relative_end_time_counts_back_like_start_time(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A relative end_time counts back from the same current time as start_time."""
    window = await _window_sent_to_core(
        hass, core_client, start_time="2h", end_time="1h"
    )
    assert window == (NOW - timedelta(hours=2), NOW - timedelta(hours=1))


@pytest.mark.usefixtures("frozen_now")
async def test_window_without_end_time_ends_now(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without an end_time, a look-back counts from the current time and the window ends now."""
    window = await _window_sent_to_core(hass, core_client, start_time="2h")
    assert window == (NOW - timedelta(hours=2), NOW)


@pytest.mark.parametrize(("start_time", "end_time"), [("bogus", None), ("2h", "bogus")])
async def test_rejected_time_suggests_every_relative_unit(
    core_client: HomeAssistantClient, start_time: str, end_time: str | None
) -> None:
    """A rejected start_time or end_time suggests working examples for every relative unit."""
    with pytest.raises(ToolError) as exc_info:
        await tools_history.HistoryTools(core_client).ha_get_history(
            entity_ids="sensor.temp", start_time=start_time, end_time=end_time
        )

    suggestions = json.loads(str(exc_info.value))["error"]["suggestions"]
    examples = [ex for s in suggestions for ex in re.findall(r"'([^']+)'", s)]
    for example in examples:
        parse_relative_time(example, reference_time=NOW)
    for unit in RELATIVE_TIME_UNIT_SECONDS:
        assert any(re.fullmatch(rf"\d+{unit}", ex) for ex in examples), unit


class TestParseRelativeTime:
    """The parser behind ha_get_history's start_time and end_time."""

    @pytest.mark.usefixtures("frozen_now")
    def test_without_reference_time_counts_back_from_now(self) -> None:
        """Without a reference time, a relative value counts back from the current time."""
        assert parse_relative_time("1h") == NOW - timedelta(hours=1)

    def test_none_returns_default_hours_ago(self) -> None:
        """No value looks back the caller's default number of hours."""
        result = parse_relative_time(None, default_hours=48, reference_time=NOW)
        assert result == NOW - timedelta(hours=48)

    @pytest.mark.parametrize(
        ("value", "look_back"),
        [
            ("30min", timedelta(minutes=30)),
            ("24h", timedelta(hours=24)),
            ("7d", timedelta(days=7)),
            ("2w", timedelta(weeks=2)),
            # The documented meaning of "m": 30 days, not a calendar month.
            ("1m", timedelta(days=30)),
            ("24H", timedelta(hours=24)),
            ("  7d  ", timedelta(days=7)),
            ("0h", timedelta(0)),
        ],
    )
    def test_relative_value_looks_back_from_reference_time(
        self, value: str, look_back: timedelta
    ) -> None:
        """Each unit, in any case and with surrounding spaces, looks back from the reference time."""
        assert parse_relative_time(value, reference_time=NOW) == NOW - look_back

    def test_iso_format_with_z_suffix(self) -> None:
        """An ISO time ending in Z is read as UTC."""
        result = parse_relative_time("2025-01-25T12:00:00Z")
        assert result == datetime(2025, 1, 25, 12, 0, 0, tzinfo=UTC)

    def test_iso_format_without_timezone(self) -> None:
        """ISO format without timezone stays naive for ha_get_history to resolve."""
        result = parse_relative_time("2025-01-25T12:00:00")
        assert result == datetime(2025, 1, 25, 12, 0, 0)

    @pytest.mark.parametrize("value", ["invalid", "24x", "20mins", "-24h"])
    def test_unknown_value_is_rejected(self, value: str) -> None:
        """An unknown unit ('24x', or '20mins', which is not '20min') or a negative look-back is rejected, not read as another window."""
        with pytest.raises(ValueError, match="Invalid time format"):
            parse_relative_time(value)

    @pytest.mark.parametrize("value", ["99999999d", "100000000m"])
    def test_overflowing_relative_value_raises_validation_error(
        self, value: str
    ) -> None:
        """A value too large for a datetime is rejected as out of range, not raised as OverflowError."""
        with pytest.raises(ValueError, match="is out of range"):
            parse_relative_time(value)


async def test_time_without_offset_is_home_assistant_local_time(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Like Core, ha_get_history reads a time without an offset in HA's zone."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    window = await _window_sent_to_core(
        hass,
        core_client,
        start_time="2026-07-01T12:00:00",
        end_time="2026-07-01T13:00:00Z",
    )
    assert window == (
        datetime(2026, 7, 1, 10, tzinfo=UTC),
        datetime(2026, 7, 1, 13, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("entity_count", "end_time", "options"),
    [
        # 10 entities x 25 elapsed hours = 250 entity-hours, over 240
        (10, "2026-10-25T12:00:00", {}),
        # 25 entities x 34 elapsed hours x 12 rows = 10200 rows, over 10000
        (25, "2026-10-25T21:00:00", {"source": "statistics", "period": "5minute"}),
    ],
)
async def test_workload_across_dst_end_counts_elapsed_hours(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_count: int,
    end_time: str,
    options: dict[str, Any],
) -> None:
    """A local window that ends DST is one hour longer than its wall clock."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    with (
        patch.object(
            tools_history,
            "get_global_settings",
            return_value=MagicMock(enable_history_query_guardrails=True),
        ),
        pytest.raises(ToolError, match="safe workload budget"),
    ):
        await tools_history.HistoryTools(core_client).ha_get_history(
            entity_ids=[f"sensor.s{i}" for i in range(entity_count)],
            start_time="2026-10-24T12:00:00",
            end_time=end_time,
            **options,
        )


@pytest.mark.parametrize(
    ("guardrails", "start_time", "end_time"),
    [
        # 02:45 is not a New York time; it reads as 07:45 UTC, after 03:15 = 07:15 UTC
        (False, "2026-03-08T02:45:00", "2026-03-08T03:15:00"),
        # 02:30 and 03:30 are both 07:30 UTC: a zero-length window
        (True, "2026-03-08T02:30:00", "2026-03-08T03:30:00"),
    ],
)
async def test_window_across_spring_forward_gap_is_checked_as_instants(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    guardrails: bool,
    start_time: str,
    end_time: str,
) -> None:
    """A start in the spring-forward gap is not earlier than an end after the gap."""
    await hass.config.async_set_time_zone("America/New_York")
    with (
        patch.object(
            tools_history,
            "get_global_settings",
            return_value=MagicMock(enable_history_query_guardrails=guardrails),
        ),
        pytest.raises(ToolError, match="end_time must be later than start_time"),
    ):
        await tools_history.HistoryTools(core_client).ha_get_history(
            entity_ids="sensor.temp", start_time=start_time, end_time=end_time
        )
