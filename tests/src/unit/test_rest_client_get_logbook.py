"""``get_logbook`` against Core's own logbook endpoint.

The rows come from Core's recorder through its REST view; a client that gets no
answer raises instead of reading as an empty logbook.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockModule, mock_integration
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError


@pytest.fixture(autouse=True)
async def core_logbook(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """Core's logbook integration, served by the real in-memory recorder."""
    mock_integration(hass, MockModule("frontend"))
    assert await async_setup_component(hass, "logbook", {})


def _start_time() -> str:
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat()


@pytest.mark.asyncio
async def test_get_logbook_returns_core_s_rows_for_the_context(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The endpoint's row list, filtered by the context Core recorded."""
    context = Context()
    hass.states.async_set("light.kitchen", "initial")
    hass.states.async_set("light.kitchen", "on", context=context)
    hass.states.async_set("light.hall", "initial")
    hass.states.async_set("light.hall", "on", context=Context())
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    rows = await core_client.get_logbook(
        start_time=_start_time(), context_id=context.id
    )

    assert rows is not None
    assert [(row["entity_id"], row["state"]) for row in rows] == [
        ("light.kitchen", "on")
    ]


@pytest.mark.asyncio
async def test_a_dropped_connection_surfaces_instead_of_an_empty_logbook(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that gets no answer must not read as "no entries"."""
    monkeypatch.setattr(
        core_client,
        "_request",
        AsyncMock(side_effect=HomeAssistantConnectionError("down")),
    )

    with pytest.raises(HomeAssistantConnectionError):
        await core_client.get_logbook(start_time=_start_time())
