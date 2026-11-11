"""``ha_mcp_tools/flow_record``: what a flow in progress was started for, read
from the flow manager of a real ``hass``."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ._component_core import ws_result

_DOMAIN = "flow_record_probe"


class _Flow(ConfigFlow):
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(step_id="user")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(step_id="reconfigure")


@pytest.fixture
def entry(hass: HomeAssistant) -> Iterator[MockConfigEntry]:
    mock_integration(hass, MockModule(_DOMAIN))
    mock_platform(hass, f"{_DOMAIN}.config_flow", None)
    entry = MockConfigEntry(domain=_DOMAIN)
    entry.add_to_hass(hass)
    with mock_config_flow(_DOMAIN, _Flow):
        yield entry


async def test_a_reconfigure_flow_is_told_from_a_setup_flow_of_its_domain(
    hass: HomeAssistant, component_ws: Any, entry: MockConfigEntry
) -> None:
    """Core's REST answer to both flows names the domain and a form. Only the
    record says which of them edits an existing entry, and which entry."""
    setup = await hass.config_entries.flow.async_init(
        _DOMAIN, context={"source": SOURCE_USER}
    )
    reconfigure = await hass.config_entries.flow.async_init(
        _DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )

    records = [
        await ws_result(
            component_ws,
            "ha_mcp_tools/flow_record",
            manager="config",
            flow_id=flow["flow_id"],
        )
        for flow in (setup, reconfigure)
    ]

    assert records[0] == {
        "found": True,
        "handler": _DOMAIN,
        "source": "user",
        "entry_id": None,
        "subentry_id": None,
    }
    assert records[1] == {
        "found": True,
        "handler": _DOMAIN,
        "source": "reconfigure",
        "entry_id": entry.entry_id,
        "subentry_id": None,
    }


async def test_a_flow_of_another_manager_is_not_found(
    hass: HomeAssistant, component_ws: Any, entry: MockConfigEntry
) -> None:
    """A config flow's id is not an options flow: each manager holds its own."""
    setup = await hass.config_entries.flow.async_init(
        _DOMAIN, context={"source": SOURCE_USER}
    )

    record = await ws_result(
        component_ws,
        "ha_mcp_tools/flow_record",
        manager="options",
        flow_id=setup["flow_id"],
    )

    assert record == {"found": False}
