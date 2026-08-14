"""Regression tests for the HA-MCP component's actionable restart repair.

Issue #2210: the legacy OAuth restart warning must offer a fix flow that
restarts Home Assistant instead of only allowing the issue to be ignored.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()


class _RepairsFlow:
    """Small HA RepairsFlow stand-in with real flow-result behavior."""

    def async_show_form(self, *, step_id, data_schema):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema}

    def async_create_entry(self, *, data):
        return {"type": "create_entry", "data": data}


data_entry_flow = ModuleType("homeassistant.data_entry_flow")
data_entry_flow.FlowResult = dict
sys.modules["homeassistant.data_entry_flow"] = data_entry_flow
sys.modules["homeassistant"].data_entry_flow = data_entry_flow

repairs_platform = ModuleType("homeassistant.components.repairs")
repairs_platform.RepairsFlow = _RepairsFlow
sys.modules["homeassistant.components.repairs"] = repairs_platform


def _load_repairs_module():
    from custom_components.ha_mcp_tools import repairs

    return repairs


async def test_legacy_oauth_fix_flow_restarts_home_assistant_blocking():
    """A missing/wrong restart service call would leave the repair unresolved."""
    repairs = _load_repairs_module()
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    flow = await repairs.async_create_fix_flow(
        hass,
        "legacy_oauth_restart",
        None,
    )
    flow.hass = hass

    result = await flow.async_step_confirm({})

    hass.services.async_call.assert_awaited_once_with(
        "homeassistant",
        "restart",
        {},
        blocking=True,
    )
    assert result == {"type": "create_entry", "data": {}}


async def test_legacy_oauth_fix_flow_prompts_before_restart():
    """Opening the repair must show confirmation without restarting HA."""
    repairs = _load_repairs_module()
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    flow = await repairs.async_create_fix_flow(
        hass,
        "legacy_oauth_restart",
        None,
    )
    flow.hass = hass

    result = await flow.async_step_init()

    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    hass.services.async_call.assert_not_awaited()


async def test_legacy_oauth_fix_flow_does_not_complete_rejected_restart():
    """A rejected restart must leave the repair flow—and issue—unfinished."""
    repairs = _load_repairs_module()
    hass = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("restart rejected"))
    flow = await repairs.async_create_fix_flow(
        hass,
        "legacy_oauth_restart",
        None,
    )
    flow.hass = hass
    flow.async_create_entry = MagicMock()

    with pytest.raises(RuntimeError, match="restart rejected"):
        await flow.async_step_confirm({})

    flow.async_create_entry.assert_not_called()


@pytest.mark.parametrize(
    "catalog_path",
    [
        "custom_components/ha_mcp_tools/strings.json",
        "custom_components/ha_mcp_tools/translations/en.json",
    ],
)
def test_legacy_oauth_repair_catalog_has_fix_flow(catalog_path):
    """Both HA English catalogs must render the actionable confirmation flow."""
    root = Path(__file__).parents[3]
    catalog = json.loads((root / catalog_path).read_text())

    issue = catalog["issues"]["legacy_oauth_restart"]
    assert "description" not in issue
    confirm = issue["fix_flow"]["step"]["confirm"]
    assert confirm["title"]
    assert confirm["description"]
