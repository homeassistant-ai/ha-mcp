"""Unit tests for the ha_mcp_tools bootstrap config flow.

These drive the config-flow state machine (Supervisor-detection routing, the
add-on install/decline steps, the progress -> success/failed transitions, and
async_remove task cancellation) without a real Home Assistant. The framework
methods (async_show_form / async_show_progress / async_create_entry / ...) are
stubbed on the flow instance so each step's routing decision is asserted
directly. The real Supervisor calls live in addon.py and are covered by
test_addon_bootstrap.py; a live end-to-end check runs on the HAOS tier.
"""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


# --- Stub Home Assistant modules so config_flow imports without HA installed.
# config_flow subclasses ConfigFlow, so unlike the plain-MagicMock approach in
# test_addon_bootstrap.py we need a real, subclassable base class here.
class _FakeConfigFlowResult(dict):
    """Stand-in for homeassistant.config_entries.ConfigFlowResult."""


class _FakeConfigFlowBase:
    """Minimal subclassable stand-in for ConfigFlow (absorbs domain=...)."""

    def __init_subclass__(cls, **kwargs):
        return None


_ce = MagicMock()
_ce.ConfigFlow = _FakeConfigFlowBase
_ce.ConfigFlowResult = _FakeConfigFlowResult
sys.modules["homeassistant.config_entries"] = _ce

_core = MagicMock()
_core.callback = lambda func: func
sys.modules["homeassistant.core"] = _core

sys.modules["homeassistant.helpers.hassio"] = MagicMock()

for _mod in [
    "voluptuous",
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.persistent_notification",
    "homeassistant.helpers",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.storage",
    "homeassistant.loader",
]:
    sys.modules.setdefault(_mod, MagicMock())

from custom_components.ha_mcp_tools import config_flow as cf  # noqa: E402
from custom_components.ha_mcp_tools.addon import AddonBootstrapError  # noqa: E402


def _make_flow(*, is_hassio: bool) -> cf.HaMcpToolsConfigFlow:
    """Build a flow with the HA framework methods stubbed to return markers."""
    flow = cf.HaMcpToolsConfigFlow()
    flow.async_set_unique_id = AsyncMock(return_value=None)
    flow._abort_if_unique_id_configured = MagicMock(return_value=None)
    flow.async_show_form = MagicMock(side_effect=lambda **kw: {"type": "form", **kw})
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kw: {"type": "entry", **kw}
    )
    flow.async_show_progress = MagicMock(
        side_effect=lambda **kw: {"type": "progress", **kw}
    )
    flow.async_show_progress_done = MagicMock(
        side_effect=lambda **kw: {"type": "progress_done", **kw}
    )
    flow.hass = SimpleNamespace(
        async_create_task=lambda coro: asyncio.ensure_future(coro)
    )
    cf.is_hassio = lambda hass: is_hassio
    return flow


async def _drive_install(flow, addon_input):
    """Accept the add-on step, then pump the progress step to completion."""
    result = await flow.async_step_addon(addon_input)
    guard = 0
    while result.get("type") == "progress":
        guard += 1
        assert guard < 5, "install step did not converge"
        if flow._install_task is not None:
            await asyncio.gather(flow._install_task, return_exceptions=True)
        result = await flow.async_step_install_addon()
    return result


class TestUserStep:
    def test_non_supervisor_shows_form_then_creates_entry(self):
        flow = _make_flow(is_hassio=False)
        form = asyncio.run(flow.async_step_user(None))
        assert form["type"] == "form"
        assert form["step_id"] == "user"

        entry = asyncio.run(flow.async_step_user({}))
        assert entry["type"] == "entry"
        assert entry["title"] == cf._ENTRY_TITLE

    def test_supervisor_routes_to_addon_step(self):
        flow = _make_flow(is_hassio=True)
        form = asyncio.run(flow.async_step_user(None))
        assert form["type"] == "form"
        assert form["step_id"] == "addon"


class TestAddonStep:
    def test_decline_creates_entry_without_installing(self):
        flow = _make_flow(is_hassio=True)
        cf.async_install_and_start_addon = AsyncMock()
        result = asyncio.run(flow.async_step_addon({cf._CONF_INSTALL_ADDON: False}))
        assert result["type"] == "entry"
        cf.async_install_and_start_addon.assert_not_called()

    def test_accept_installs_then_succeeds(self):
        flow = _make_flow(is_hassio=True)
        cf.async_install_and_start_addon = AsyncMock(return_value=None)
        result = asyncio.run(_drive_install(flow, {cf._CONF_INSTALL_ADDON: True}))
        assert result["type"] == "progress_done"
        assert result["next_step_id"] == "addon_success"

    def test_accept_failure_routes_to_install_failed(self):
        flow = _make_flow(is_hassio=True)
        cf.async_install_and_start_addon = AsyncMock(
            side_effect=AddonBootstrapError("boom")
        )
        result = asyncio.run(_drive_install(flow, {cf._CONF_INSTALL_ADDON: True}))
        assert result["type"] == "progress_done"
        assert result["next_step_id"] == "install_failed"
        assert flow._install_error == "boom"


class TestTerminalSteps:
    def test_addon_success_creates_entry(self):
        flow = _make_flow(is_hassio=True)
        result = asyncio.run(flow.async_step_addon_success())
        assert result["type"] == "entry"
        assert result["title"] == cf._ENTRY_TITLE

    def test_install_failed_surfaces_error_then_creates_entry(self):
        flow = _make_flow(is_hassio=True)
        flow._install_error = "boom"
        form = asyncio.run(flow.async_step_install_failed(None))
        assert form["type"] == "form"
        assert form["step_id"] == "install_failed"
        assert form["description_placeholders"]["error"] == "boom"

        entry = asyncio.run(flow.async_step_install_failed({}))
        assert entry["type"] == "entry"

    def test_install_failed_defaults_error_placeholder(self):
        flow = _make_flow(is_hassio=True)
        form = asyncio.run(flow.async_step_install_failed(None))
        assert form["description_placeholders"]["error"] == "unknown error"


class TestAsyncRemove:
    def test_cancels_inflight_task(self):
        flow = cf.HaMcpToolsConfigFlow()
        task = MagicMock()
        task.done.return_value = False
        flow._install_task = task
        flow.async_remove()
        task.cancel.assert_called_once()

    def test_no_cancel_when_task_done(self):
        flow = cf.HaMcpToolsConfigFlow()
        task = MagicMock()
        task.done.return_value = True
        flow._install_task = task
        flow.async_remove()
        task.cancel.assert_not_called()

    def test_no_error_when_no_task(self):
        flow = cf.HaMcpToolsConfigFlow()
        flow._install_task = None
        flow.async_remove()  # must not raise
