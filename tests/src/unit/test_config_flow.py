"""Unit tests for the merged HA-MCP config + options flow (issues #1527, tile fold).

One config flow serves two entry types under the shared domain:

* the ``user`` step is a menu (``tools`` / ``server``);
* ``tools`` is a plain confirm-and-create step that creates the services entry;
* ``server`` is a single confirm step that creates the in-process server entry
  (``entry_type="server"``);
* ``async_get_options_flow`` dispatches on ``entry_type`` — the server entry gets
  the port/auth/pip options flow, the tools entry gets a flow that aborts with
  ``no_options``.

The HA framework methods (async_show_menu / async_show_form / async_create_entry
/ ...) are stubbed on the flow instance so each routing decision is asserted
directly.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
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


def _make_flow() -> cf.HaMcpToolsConfigFlow:
    """Build a flow with the HA framework methods stubbed to return markers."""
    flow = cf.HaMcpToolsConfigFlow()
    flow.async_set_unique_id = AsyncMock(return_value=None)
    flow._abort_if_unique_id_configured = MagicMock(return_value=None)
    flow.async_show_menu = MagicMock(side_effect=lambda **kw: {"type": "menu", **kw})
    flow.async_show_form = MagicMock(side_effect=lambda **kw: {"type": "form", **kw})
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kw: {"type": "entry", **kw}
    )
    return flow


def _make_options_flow(*, options=None, data=None) -> cf.HaMcpServerOptionsFlow:
    flow = cf.HaMcpServerOptionsFlow()
    flow.config_entry = SimpleNamespace(options=options or {}, data=data or {})
    flow.async_show_form = MagicMock(side_effect=lambda **kw: {"type": "form", **kw})
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kw: {"type": "entry", **kw}
    )
    return flow


class TestMenuStep:
    def test_user_step_shows_entry_type_menu(self):
        flow = _make_flow()
        menu = asyncio.run(flow.async_step_user(None))
        assert menu["type"] == "menu"
        assert menu["step_id"] == "user"
        # Server first: it is the recommended entry; tools is the opt-in
        # file/YAML services entry (#1715).
        assert menu["menu_options"] == [
            const.ENTRY_TYPE_SERVER,
            const.ENTRY_TYPE_TOOLS,
        ]


class TestToolsBranch:
    def test_non_supervisor_shows_form_then_creates_entry(self):
        flow = _make_flow()
        form = asyncio.run(flow.async_step_tools(None))
        assert form["type"] == "form"
        assert form["step_id"] == "tools"

        entry = asyncio.run(flow.async_step_tools({}))
        assert entry["type"] == "entry"
        assert entry["title"] == cf._TOOLS_ENTRY_TITLE
        assert entry["data"] == {const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS}

    def test_tools_uses_domain_unique_id(self):
        flow = _make_flow()
        asyncio.run(flow.async_step_tools(None))
        flow.async_set_unique_id.assert_awaited_once_with(const.DOMAIN)
        flow._abort_if_unique_id_configured.assert_called_once()


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

    def test_form_prefills_every_field_from_saved_options(self):
        # Review gap: the form must show the user's SAVED values, not the
        # defaults, for every field (a regression here silently reverts a
        # user's config on the next save).
        saved = {
            const.OPT_CHANNEL: const.CHANNEL_DEV,
            const.OPT_SERVER_PORT: 12345,
            const.OPT_BIND_HOST: const.BIND_HOST_LOOPBACK,
            const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_HA,
            const.OPT_PIP_SPEC: "ha-mcp==0.0.1",
            const.OPT_SERVER_URL: "https://ha.example:8123",
            const.OPT_EXTERNAL_URL: "https://ha.example.com",
            const.OPT_WEBHOOK_ID_OVERRIDE: "my_custom_hook",
            const.OPT_SECRET_PATH_OVERRIDE: "/custom_path",
        }
        flow = _make_options_flow(
            data={const.DATA_WEBHOOK_ID: "mcp_abc"}, options=saved
        )
        form = asyncio.run(flow.async_step_init(None))
        defaults = {m.schema: m.default() for m in form["data_schema"].schema}
        # regenerate_secrets is a one-shot action, never pre-filled True;
        # enable_webhook defaults on when unsaved.
        regenerate_default = defaults.pop(const.OPT_REGENERATE_SECRETS)
        assert regenerate_default is False
        webhook_default = defaults.pop(const.OPT_ENABLE_WEBHOOK)
        assert webhook_default is True
        assert defaults == saved

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
        # _normalize adds the (empty) URL/secret management fields when the
        # submission omits them.
        assert result["data"] == {
            **user_input,
            const.OPT_EXTERNAL_URL: "",
            const.OPT_WEBHOOK_ID_OVERRIDE: "",
            const.OPT_SECRET_PATH_OVERRIDE: "",
        }

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

    def test_connect_url_hint_before_start_points_at_log(self):
        flow = _make_options_flow(data={})
        hint = flow._connect_url_hint().lower()
        assert "once the server has started" in hint
        assert "notification" not in hint

    def test_connect_url_hint_resolves_actual_urls_via_builder(self):
        """With hass available, the hint lists the REAL resolved URLs.

        The builder import is module-local (``from .embedded_setup import
        build_connect_urls``), so injecting a stub module into sys.modules
        substitutes it without importing the real embedded_setup (which
        needs a full Home Assistant install).
        """
        calls: list[dict] = []

        def fake_builder(hass, entry, *, webhook_enabled=True):
            calls.append({"hass": hass, "webhook_enabled": webhook_enabled})
            return [
                "https://example.duckdns.org/api/webhook/mcp_abc",
                "http://192.168.1.150:9584/private_x (direct access)",
            ]

        stub = ModuleType("custom_components.ha_mcp_tools.embedded_setup")
        stub.build_connect_urls = fake_builder
        flow = _make_options_flow(
            options={const.OPT_ENABLE_WEBHOOK: False},
            data={"webhook_id": "mcp_abc", "secret_path": "/private_x"},
        )
        flow.hass = MagicMock()
        orig = sys.modules.get("custom_components.ha_mcp_tools.embedded_setup")
        sys.modules["custom_components.ha_mcp_tools.embedded_setup"] = stub
        try:
            hint = flow._connect_url_hint()
        finally:
            if orig is None:
                del sys.modules["custom_components.ha_mcp_tools.embedded_setup"]
            else:
                sys.modules["custom_components.ha_mcp_tools.embedded_setup"] = orig
        assert "https://example.duckdns.org/api/webhook/mcp_abc" in hint
        assert "http://192.168.1.150:9584/private_x" in hint
        assert "<your-home-assistant-url>" not in hint
        # The enable_webhook option is forwarded to the builder.
        assert calls and calls[0]["webhook_enabled"] is False
