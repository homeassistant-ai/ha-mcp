"""The ``ha_mcp_tools/services_list`` wire language.

Both halves of the same contract: the command schema must NOT inject a
``language`` default (a voluptuous default is applied before the async prep
runs, so ``default="en"`` would pin every non-English instance to English),
and the prep must therefore read ``hass.config.language`` for an omitted value.

The command runs on a real ``hass`` whose language is German, with Core's
``homeassistant`` integration loaded; its ``turn_on`` service name is
translated by Core's own translation files, so the language that reached Core
is read from the reply.

Kept in its own module rather than in ``test_component_ws_phase2_async.py``,
which is already over the 1,000-line ratchet limit.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ._component_core import ws_result

NAME_KEY = "component.homeassistant.services.turn_on.name"
ENGLISH = "Generic turn on"
GERMAN = "Generisches Einschalten"


@pytest.fixture
async def german_hass(hass: HomeAssistant) -> HomeAssistant:
    """A real ``hass`` configured for German with ``homeassistant`` set up."""
    assert await async_setup_component(hass, "homeassistant", {})
    hass.config.language = "de"
    return hass


async def test_schema_leaves_an_omitted_language_unset(
    german_hass: HomeAssistant, component_ws: Any
) -> None:
    """A default here would beat ``hass.config.language`` for every request."""
    result = await ws_result(
        component_ws, "ha_mcp_tools/services_list", domain="homeassistant"
    )

    assert result["translations"][NAME_KEY] != ENGLISH


async def test_omitted_language_requests_the_configured_language(
    german_hass: HomeAssistant, component_ws: Any
) -> None:
    """The prep asks Core for the instance's language, not English."""
    result = await ws_result(
        component_ws, "ha_mcp_tools/services_list", domain="homeassistant"
    )

    assert result["translations"][NAME_KEY] == GERMAN


async def test_explicit_language_beats_the_configured_one(
    german_hass: HomeAssistant, component_ws: Any
) -> None:
    """A language sent on the wire is the one Core is asked for."""
    result = await ws_result(
        component_ws,
        "ha_mcp_tools/services_list",
        domain="homeassistant",
        language="en",
    )

    assert result["translations"][NAME_KEY] == ENGLISH
