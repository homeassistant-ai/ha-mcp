"""``add_timezone_metadata`` converts Core's timestamps to Home Assistant's zone (#1199).

Home Assistant's time zone is Core's own ``/api/config`` answer, and every
converted timestamp is one Core served: a state's ``last_changed`` /
``last_updated`` / ``last_reported``, an automation's ``last_triggered``
attribute, a logbook row's ``when``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfoNotFoundError

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockModule, mock_integration
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import response_helpers
from ha_mcp.tools.response_helpers import add_timezone_metadata

NEW_YORK = "America/New_York"
# 00:06 UTC on June 12 is 20:06 EDT on June 11: the date rolls back.
SUMMER = datetime(2026, 6, 12, 0, 6, tzinfo=UTC)
WINTER = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
async def client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The server's client on a Core whose time zone is New York."""
    await hass.config.async_set_time_zone(NEW_YORK)
    return core_client


async def _state_at(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entity_id: str,
    when: datetime,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Core's state of ``entity_id`` as written at ``when``."""
    hass.states.async_set(entity_id, "heat", attributes, timestamp=when.timestamp())
    return await client.get_entity_state(entity_id)


class TestEnvelope:
    async def test_default_wraps_data_and_metadata(
        self, client: HomeAssistantClient
    ) -> None:
        data = {"success": True, "results": []}

        result = await add_timezone_metadata(client, data)

        assert result["data"] == data
        assert result["metadata"]["home_assistant_timezone"] == NEW_YORK

    async def test_without_metadata_returns_the_data_itself(
        self, client: HomeAssistantClient
    ) -> None:
        data = {"success": True, "results": []}

        result = await add_timezone_metadata(client, data, include_metadata=False)

        assert result is data
        assert "metadata" not in result

    async def test_config_without_answer_falls_back_to_utc_unconverted(
        self, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wrapper stays; the zone is UTC and the note says nothing was
        converted."""
        monkeypatch.setattr(
            client, "get_config", AsyncMock(side_effect=OSError("connection refused"))
        )

        result = await add_timezone_metadata(
            client, {"last_changed": "2026-06-12T12:00:00+00:00"}
        )

        assert result["data"] == {"last_changed": "2026-06-12T12:00:00+00:00"}
        assert result["metadata"]["home_assistant_timezone"] == "UTC"
        assert "UTC" in result["metadata"]["note"]
        assert "converted" not in result["metadata"]["note"].lower()

    async def test_note_names_the_converted_fields_and_utc_windows(
        self, client: HomeAssistantClient
    ) -> None:
        """Only per-record fields are converted; window boundaries stay UTC."""
        result = await add_timezone_metadata(client, {"success": True})

        note = result["metadata"]["note"]
        assert "last_changed" in note
        assert "UTC" in note


class TestConversion:
    @pytest.mark.parametrize(
        ("when", "local"),
        [
            (SUMMER, "2026-06-11T20:06:00-04:00"),
            (WINTER, "2026-01-15T07:00:00-05:00"),
        ],
        ids=["edt", "est"],
    )
    async def test_state_timestamps_are_local(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        when: datetime,
        local: str,
    ) -> None:
        """The zone's DST offset of that date applies; other fields stay."""
        state = await _state_at(hass, client, "climate.hall", when)

        result = await add_timezone_metadata(client, state)

        converted = result["data"]
        for field in ("last_changed", "last_updated", "last_reported"):
            assert converted[field] == local, field
        assert converted["state"] == "heat"
        assert converted["entity_id"] == "climate.hall"

    async def test_nested_last_triggered_is_local(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        """An automation's ``last_triggered`` sits in its attributes."""
        state = await _state_at(
            hass,
            client,
            "automation.wake",
            SUMMER,
            {"last_triggered": datetime(2026, 6, 12, 4, 0, tzinfo=UTC)},
        )

        result = await add_timezone_metadata(client, {"entity": state})

        attributes = result["data"]["entity"]["attributes"]
        assert attributes["last_triggered"] == "2026-06-12T00:00:00-04:00"

    async def test_every_state_in_a_list_is_local(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        states = [
            await _state_at(hass, client, "climate.hall", SUMMER),
            await _state_at(
                hass, client, "climate.den", datetime(2026, 6, 12, 4, 1, tzinfo=UTC)
            ),
        ]

        result = await add_timezone_metadata(client, {"states": states})

        assert [s["last_changed"] for s in result["data"]["states"]] == [
            "2026-06-11T20:06:00-04:00",
            "2026-06-12T00:01:00-04:00",
        ]

    async def test_logbook_when_is_local(
        self,
        recorder_mock: Recorder,
        hass: HomeAssistant,
        client: HomeAssistantClient,
    ) -> None:
        mock_integration(hass, MockModule("frontend"))
        assert await async_setup_component(hass, "logbook", {})
        hass.states.async_set("light.kitchen", "off", timestamp=SUMMER.timestamp())
        hass.states.async_set(
            "light.kitchen", "on", timestamp=(SUMMER + timedelta(minutes=1)).timestamp()
        )
        await async_wait_recording_done(hass)
        rows = await client.get_logbook(
            entity_id="light.kitchen",
            start_time=(SUMMER - timedelta(hours=1)).isoformat(),
            end_time=(SUMMER + timedelta(hours=1)).isoformat(),
        )
        assert rows

        result = await add_timezone_metadata(client, {"entries": rows})

        assert result["data"]["entries"][-1]["when"] == "2026-06-11T20:07:00-04:00"

    async def test_zone_missing_from_tzdata_falls_back_to_utc(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The server's tzdata may lack a zone Core knows: UTC, unconverted."""

        def no_tzdata(key: str) -> None:
            raise ZoneInfoNotFoundError(key)

        monkeypatch.setattr(response_helpers, "ZoneInfo", no_tzdata)
        state = await _state_at(hass, client, "climate.hall", SUMMER)

        result = await add_timezone_metadata(client, state)

        assert result["metadata"]["home_assistant_timezone"] == "UTC"
        assert result["data"]["last_changed"] == SUMMER.isoformat()
