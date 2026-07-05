"""Unit tests for the merged HA-MCP config + options flow (issues #1527, tile fold).

One config flow serves two entry types under the shared domain:

* the ``user`` step is a menu (``tools`` / ``server``);
* ``tools`` drives the Supervisor-detection / add-on bootstrap state machine and
  creates the services entry (``entry_type="tools"``);
* ``server`` is a single confirm step that creates the in-process server entry
  (``entry_type="server"``);
* ``async_get_options_flow`` dispatches on ``entry_type`` — the server entry gets
  the port/auth/pip options flow, the tools entry gets a flow that aborts with
  ``no_options``.

The HA framework methods (async_show_menu / async_show_form / async_create_entry
/ ...) are stubbed on the flow instance so each routing decision is asserted
directly. The real Supervisor calls live in addon.py and are covered by
test_addon_bootstrap.py; a live end-to-end check runs on the HAOS tier.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


# --- Stub Home Assistant so the merged config_flow imports without HA installed.
# config_flow subclasses ConfigFlow AND OptionsFlow, so both need real,
# subclassable bases (a plain MagicMock attribute can't be subclassed).
class _ConfigFlowResult(dict):
    """Stand-in for homeassistant.config_entries.ConfigFlowResult."""


class _ConfigFlowBase:
    """Subclassable stand-in for ConfigFlow (absorbs the domain= kwarg)."""

    def __init_subclass__(cls, **kwargs):
        return None


class _OptionsFlowBase:
    """Subclassable stand-in for OptionsFlow (config_entry stays settable)."""


_ce = MagicMock()
_ce.ConfigFlow = _ConfigFlowBase
_ce.OptionsFlow = _OptionsFlowBase
_ce.ConfigFlowResult = _ConfigFlowResult
_ce.ConfigEntry = MagicMock
sys.modules["homeassistant.config_entries"] = _ce

_core = MagicMock()
_core.callback = lambda func: func  # identity so async_get_options_flow builds
sys.modules["homeassistant.core"] = _core

sys.modules["homeassistant.helpers.hassio"] = MagicMock()


# Inert selector stand-ins: the options flow builds SelectSelector dropdowns,
# but these tests hand user_input straight to the handler, so the selector
# never validates - it only needs to construct (and expose .config for the
# schema-shape assertions below).
class _SelectSelectorConfig:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _SelectSelector:
    def __init__(self, config=None):
        self.config = config

    def __call__(self, value):
        return value


class _SelectSelectorMode:
    DROPDOWN = "dropdown"
    LIST = "list"


_sel = MagicMock()
_sel.SelectSelector = _SelectSelector
_sel.SelectSelectorConfig = _SelectSelectorConfig
_sel.SelectSelectorMode = _SelectSelectorMode
sys.modules["homeassistant.helpers.selector"] = _sel

for _mod in [
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.persistent_notification",
    "homeassistant.config",
    "homeassistant.helpers",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.storage",
    "homeassistant.loader",
]:
    sys.modules.setdefault(_mod, MagicMock())

# Determinism under pytest-xdist: peer unit files may register a MagicMock
# voluptuous via sys.modules.setdefault, and whether that or the real package is
# bound is import-order dependent. The options tests below inspect REAL vol.Schema
# markers (marker.schema / marker.default()), so evict any stub — plus a cached
# config_flow that may have bound it — so config_flow's own ``import voluptuous``
# re-binds the real package (a hard dependency, always installed).
for _name in [
    n for n in sys.modules if n == "voluptuous" or n.startswith("voluptuous.")
]:
    del sys.modules[_name]
sys.modules.pop("custom_components.ha_mcp_tools.config_flow", None)

from custom_components.ha_mcp_tools import config_flow as cf  # noqa: E402
from custom_components.ha_mcp_tools import const  # noqa: E402
from custom_components.ha_mcp_tools.addon import AddonBootstrapError  # noqa: E402


def _make_flow(*, is_hassio: bool = False) -> cf.HaMcpToolsConfigFlow:
    """Build a flow with the HA framework methods stubbed to return markers."""
    flow = cf.HaMcpToolsConfigFlow()
    flow.async_set_unique_id = AsyncMock(return_value=None)
    flow._abort_if_unique_id_configured = MagicMock(return_value=None)
    flow.async_show_menu = MagicMock(side_effect=lambda **kw: {"type": "menu", **kw})
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


def _make_options_flow(*, options=None, data=None) -> cf.HaMcpServerOptionsFlow:
    flow = cf.HaMcpServerOptionsFlow()
    flow.config_entry = SimpleNamespace(options=options or {}, data=data or {})
    flow.async_show_form = MagicMock(side_effect=lambda **kw: {"type": "form", **kw})
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kw: {"type": "entry", **kw}
    )
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


class TestMenuStep:
    def test_user_step_shows_entry_type_menu(self):
        flow = _make_flow()
        menu = asyncio.run(flow.async_step_user(None))
        assert menu["type"] == "menu"
        assert menu["step_id"] == "user"
        assert menu["menu_options"] == [
            const.ENTRY_TYPE_TOOLS,
            const.ENTRY_TYPE_SERVER,
        ]


class TestToolsBranch:
    def test_non_supervisor_shows_form_then_creates_entry(self):
        flow = _make_flow(is_hassio=False)
        form = asyncio.run(flow.async_step_tools(None))
        assert form["type"] == "form"
        assert form["step_id"] == "tools"

        entry = asyncio.run(flow.async_step_tools({}))
        assert entry["type"] == "entry"
        assert entry["title"] == cf._TOOLS_ENTRY_TITLE
        assert entry["data"] == {const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS}

    def test_supervisor_routes_to_addon_step(self):
        flow = _make_flow(is_hassio=True)
        form = asyncio.run(flow.async_step_tools(None))
        assert form["type"] == "form"
        assert form["step_id"] == "addon"

    def test_tools_uses_domain_unique_id(self):
        flow = _make_flow(is_hassio=False)
        asyncio.run(flow.async_step_tools(None))
        flow.async_set_unique_id.assert_awaited_once_with(const.DOMAIN)
        flow._abort_if_unique_id_configured.assert_called_once()


class TestAddonStep:
    def test_decline_creates_tools_entry_without_installing(self):
        flow = _make_flow(is_hassio=True)
        cf.async_install_and_start_addon = AsyncMock()
        result = asyncio.run(flow.async_step_addon({cf._CONF_INSTALL_ADDON: False}))
        assert result["type"] == "entry"
        assert result["data"] == {const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS}
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


class TestToolsTerminalSteps:
    def test_addon_success_creates_tools_entry(self):
        flow = _make_flow(is_hassio=True)
        result = asyncio.run(flow.async_step_addon_success())
        assert result["type"] == "entry"
        assert result["title"] == cf._TOOLS_ENTRY_TITLE
        assert result["data"] == {const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS}

    def test_install_failed_surfaces_error_then_creates_entry(self):
        flow = _make_flow(is_hassio=True)
        flow._install_error = "boom"
        form = asyncio.run(flow.async_step_install_failed(None))
        assert form["type"] == "form"
        assert form["step_id"] == "install_failed"
        assert form["description_placeholders"]["error"] == "boom"

        entry = asyncio.run(flow.async_step_install_failed({}))
        assert entry["type"] == "entry"
        assert entry["data"] == {const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS}

    def test_install_failed_defaults_error_placeholder(self):
        flow = _make_flow(is_hassio=True)
        form = asyncio.run(flow.async_step_install_failed(None))
        assert form["description_placeholders"]["error"] == "unknown error"


class TestServerBranch:
    def test_server_step_shows_confirm_form(self):
        flow = _make_flow()
        form = asyncio.run(flow.async_step_server(None))
        assert form["type"] == "form"
        assert form["step_id"] == "server"

    def test_server_step_creates_entry_with_entry_type(self):
        flow = _make_flow()
        entry = asyncio.run(flow.async_step_server({}))
        assert entry["type"] == "entry"
        assert entry["title"] == cf._SERVER_ENTRY_TITLE
        assert entry["data"] == {const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_SERVER}
        assert entry["options"] == {}

    def test_server_uses_distinct_unique_id(self):
        flow = _make_flow()
        asyncio.run(flow.async_step_server(None))
        flow.async_set_unique_id.assert_awaited_once_with(cf._SERVER_UNIQUE_ID)
        flow._abort_if_unique_id_configured.assert_called_once()
        # Distinct from the tools entry's unique id so both can coexist.
        assert cf._SERVER_UNIQUE_ID != const.DOMAIN


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


class TestOptionsFlowDispatch:
    def test_server_entry_gets_server_options_flow(self):
        entry = SimpleNamespace(data={const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_SERVER})
        result = cf.HaMcpToolsConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, cf.HaMcpServerOptionsFlow)

    def test_tools_entry_gets_no_options_flow(self):
        entry = SimpleNamespace(data={const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS})
        result = cf.HaMcpToolsConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, cf._NoOptionsFlow)

    def test_missing_entry_type_defaults_to_no_options(self):
        # A pre-existing (pre-fold) tools entry carries no entry_type key; it must
        # be treated as "tools", so it gets the no-options flow, never the server
        # options flow.
        entry = SimpleNamespace(data={})
        result = cf.HaMcpToolsConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, cf._NoOptionsFlow)
        assert not isinstance(result, cf.HaMcpServerOptionsFlow)

    def test_no_options_flow_aborts(self):
        flow = cf._NoOptionsFlow()
        flow.async_abort = MagicMock(side_effect=lambda **kw: {"type": "abort", **kw})
        result = asyncio.run(flow.async_step_init(None))
        assert result["type"] == "abort"
        assert result["reason"] == "no_options"


class TestServerOptionsFlow:
    def test_init_shows_form_with_connect_url_placeholder(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        assert form["type"] == "form"
        assert form["step_id"] == "init"
        assert "mcp_abc" in form["description_placeholders"]["connect_url"]

    def test_channel_is_first_option_field(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        markers = list(form["data_schema"].schema)
        assert markers[0].schema == const.OPT_CHANNEL

    def test_channel_defaults_to_stable(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        channel = next(
            m for m in form["data_schema"].schema if m.schema == const.OPT_CHANNEL
        )
        assert channel.default() == const.CHANNEL_STABLE

    def test_init_submit_round_trips_input_into_entry(self):
        flow = _make_options_flow()
        user_input = {
            const.OPT_CHANNEL: const.CHANNEL_DEV,
            const.OPT_SERVER_PORT: 9999,
            const.OPT_BIND_HOST: const.BIND_HOST_ALL,
            const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_HA,
            const.OPT_PIP_SPEC: "ha-mcp @ https://example/x.tgz",  # real override
            const.OPT_SERVER_URL: const.DEFAULT_LOOPBACK_URL,
        }
        result = asyncio.run(flow.async_step_init(user_input))
        assert result["type"] == "entry"
        assert result["title"] == ""
        # A genuine override is stored verbatim; the channel rides along.
        assert result["data"] == user_input

    def test_default_pip_spec_normalized_to_empty(self):
        # Saving with the pinned default in the pip-spec field must not persist it
        # as an override — the default moves with each release, so it is collapsed
        # to empty ("use the selected channel").
        flow = _make_options_flow()
        result = asyncio.run(
            flow.async_step_init(
                {
                    const.OPT_CHANNEL: const.CHANNEL_STABLE,
                    const.OPT_PIP_SPEC: const.DEFAULT_PIP_SPEC,
                }
            )
        )
        assert result["data"][const.OPT_PIP_SPEC] == ""

    def test_no_enable_toggle_option_exists(self):
        # Regression guard for the single-instance pivot: the enable/disable
        # toggle was dropped (entry-exists = server runs).
        assert not hasattr(const, "OPT_EMBEDDED_ENABLED")

    def test_connect_url_hint_uses_configured_port(self):
        flow = _make_options_flow(
            options={const.OPT_SERVER_PORT: 9999},
            data={
                const.DATA_WEBHOOK_ID: "mcp_abc",
                const.DATA_SECRET_PATH: "/private_x",
            },
        )
        hint = flow._connect_url_hint()
        assert "/api/webhook/mcp_abc" in hint
        # The LAN hint uses the CONFIGURED port, not the 9584 default.
        assert ":9999/private_x" in hint

    def test_connect_url_hint_without_webhook_prompts_notification(self):
        flow = _make_options_flow(data={})
        assert "notification" in flow._connect_url_hint().lower()
