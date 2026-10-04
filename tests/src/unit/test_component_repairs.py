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

    def async_show_form(self, *, step_id, data_schema, errors=None):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
        }

    def async_create_entry(self, *, data):
        return {"type": "create_entry", "data": data}

    def async_abort(self, *, reason):
        return {"type": "abort", "reason": reason}


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


# ---------------------------------------------------------------------------
# server_token_needed (#2427): replace the server's Home Assistant credential
# ---------------------------------------------------------------------------


def _token_hass(*, problem: str | None, entry):
    from custom_components.ha_mcp_tools import server_credentials

    hass = MagicMock()
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_schedule_reload = MagicMock()
    hass.auth.async_get_refresh_token = MagicMock(return_value=None)
    server_credentials.token_problem = MagicMock(return_value=problem)
    return hass


async def _token_flow(hass):
    repairs = _load_repairs_module()
    flow = await repairs.async_create_fix_flow(
        hass, "server_token_needed", {"entry_id": "srv1"}
    )
    flow.hass = hass
    return flow


@pytest.fixture
def _restore_token_problem():
    from custom_components.ha_mcp_tools import server_credentials

    original = server_credentials.token_problem
    yield
    server_credentials.token_problem = original


@pytest.mark.usefixtures("_restore_token_problem")
async def test_token_repair_starts_the_server_with_the_new_token():
    entry = MagicMock(entry_id="srv1", data={"webhook_id": "w"})
    hass = _token_hass(problem=None, entry=entry)
    flow = await _token_flow(hass)

    result = await flow.async_step_token({"admin_token": "  new-token "})

    assert result["type"] == "create_entry"
    update = hass.config_entries.async_update_entry.call_args
    assert update.kwargs["data"] == {"webhook_id": "w", "admin_token": "new-token"}
    hass.config_entries.async_schedule_reload.assert_called_once_with("srv1")


@pytest.mark.usefixtures("_restore_token_problem")
async def test_token_repair_keeps_asking_until_the_token_works():
    entry = MagicMock(entry_id="srv1", data={})
    hass = _token_hass(problem="token_not_admin", entry=entry)
    flow = await _token_flow(hass)

    result = await flow.async_step_token({"admin_token": "tok"})

    assert result["errors"] == {"admin_token": "token_not_admin"}
    hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.parametrize(
    "catalog_path",
    [
        "custom_components/ha_mcp_tools/strings.json",
        "custom_components/ha_mcp_tools/translations/en.json",
    ],
)
def test_token_repair_catalog_explains_every_refusal(catalog_path):
    root = Path(__file__).parents[3]
    catalog = json.loads((root / catalog_path).read_text())
    step = catalog["issues"]["server_token_needed"]["fix_flow"]
    for reason in ("invalid_token", "token_not_long_lived", "token_not_admin"):
        assert step["error"][reason]
