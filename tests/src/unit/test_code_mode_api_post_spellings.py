"""Code Mode's api_post refusals hold for every spelling Core routes alike.

Core decodes percent-escapes such as ``%5F`` before routing, and httpx drops a
``#`` fragment before sending, so either spelling reaches the real route unless
the guard folds the path first.
"""

from __future__ import annotations

import pytest
from homeassistant.core import Event, HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient

from ._code_mode import run_custom_tool


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/events/lovelace_updated",
        "/api/events/lovelace%5Fupdated",
        "/api/events/lovelace_updated#x",
    ],
)
async def test_sandbox_cannot_fire_an_internal_event(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    # Core fires lovelace_updated cleanly from an empty body, so a listener
    # tells a fired event from a refused one.
    fired: list[Event] = []
    hass.bus.async_listen("lovelace_updated", fired.append)

    result = await run_custom_tool(
        core_client, monkeypatch, f"await api_post({endpoint!r}, {{}})"
    )
    await hass.async_block_till_done()

    assert fired == []
    assert "HA-internal event" in result["error"]


@pytest.mark.parametrize(
    "endpoint",
    ["/api/states/sensor.ghost", "/api/%73tates/sensor.ghost", "/api/states"],
)
async def test_sandbox_cannot_write_a_raw_state(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    result = await run_custom_tool(
        core_client, monkeypatch, f"await api_post({endpoint!r}, {{'state': 'on'}})"
    )

    assert hass.states.get("sensor.ghost") is None
    assert "blocked from the sandbox" in result["error"]
