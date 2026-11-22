"""``ha_mcp_tools/flow_record``: what a flow in progress was started for, read
from the flow manager of a real ``hass``."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
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
    reconfigure = await entry.start_reconfigure_flow(hass)

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
        "issue_id": None,
    }
    assert records[1] == {
        "found": True,
        "handler": _DOMAIN,
        "source": "reconfigure",
        "entry_id": entry.entry_id,
        "subentry_id": None,
        "issue_id": None,
    }


async def test_a_repairs_fix_flow_names_the_issue_it_fixes(
    hass: HomeAssistant, component_ws: Any, entry: MockConfigEntry
) -> None:
    """Core's fix-flow answers name the domain only; the record also names
    the issue the flow was opened for."""
    assert await async_setup_component(hass, "repairs", {})
    ir.async_create_issue(
        hass,
        _DOMAIN,
        "probe",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="probe",
    )
    manager = repairs_flow_manager(hass)
    assert manager is not None
    fix = await manager.async_init(_DOMAIN, context={"issue_id": "probe"})

    record = await ws_result(
        component_ws,
        "ha_mcp_tools/flow_record",
        manager="repairs",
        flow_id=fix["flow_id"],
    )

    assert record["found"] is True
    assert record["handler"] == _DOMAIN
    assert record["issue_id"] == "probe"


async def test_no_fix_flow_is_found_without_repairs(component_ws: Any) -> None:
    record = await ws_result(
        component_ws, "ha_mcp_tools/flow_record", manager="repairs", flow_id="x"
    )

    assert record == {"found": False}


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
