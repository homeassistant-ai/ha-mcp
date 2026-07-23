"""Unit tests for the merged HA-MCP config + options flow (issues #1527, tile fold).

One config flow serves two entry types under the shared domain:

* the ``user`` step is a menu (``tools`` / ``server``);
* ``tools`` is a plain confirm-and-create step that creates the services entry;
* ``server`` is a single confirm step that creates the in-process server entry
  (``entry_type="server"``);
* ``async_get_options_flow`` dispatches on ``entry_type`` — the server entry gets
  the port/auth/pip options flow, the tools entry gets a light informational
  options flow (an empty-schema ``tools_info`` step, nothing to configure yet).

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

_ha_const = MagicMock()
_ha_const.__version__ = "2026.6.0"
sys.modules["homeassistant.const"] = _ha_const


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
    flow.async_abort = MagicMock(side_effect=lambda **kw: {"type": "abort", **kw})
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kw: {"type": "entry", **kw}
    )
    return flow


def _make_options_flow(*, options=None, data=None) -> cf.HaMcpServerOptionsFlow:
    flow = cf.HaMcpServerOptionsFlow()
    flow.config_entry = SimpleNamespace(options=options or {}, data=data or {})
    # Bare hass with empty data: legacy_credentials_active() resolves False
    # (nothing bound) unless a test monkeypatches it.
    flow.hass = MagicMock(name="hass")
    flow.hass.data = {}
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
        assert entry["title"] == const.TOOLS_ENTRY_TITLE
        assert entry["data"] == {const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS}

    def test_tools_uses_domain_unique_id(self):
        flow = _make_flow()
        asyncio.run(flow.async_step_tools(None))
        flow.async_set_unique_id.assert_awaited_once_with(const.DOMAIN)
        flow._abort_if_unique_id_configured.assert_called_once()

    def test_tools_remains_available_on_older_home_assistant(self, monkeypatch):
        monkeypatch.setattr(cf, "HA_VERSION", "2024.11.0")
        flow = _make_flow()

        entry = asyncio.run(flow.async_step_tools({}))

        assert entry["type"] == "entry"

    def test_tools_entry_title_reflects_rename(self):
        # #1853: the title names what the entry actually is — the privileged
        # file & YAML editing services — not "HA MCP Tools", which read as if the
        # component were required for MCP tools in general.
        assert const.TOOLS_ENTRY_TITLE == "HA-MCP File & YAML Tools"
        # The pre-rename default the setup migration retitles existing installs
        # away from (see _async_setup_tools_entry).
        assert const.TOOLS_ENTRY_LEGACY_TITLE == "HA MCP Tools"


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

    def test_server_aborts_on_unsupported_home_assistant(self, monkeypatch):
        monkeypatch.setattr(cf, "HA_VERSION", "2025.9.4")
        flow = _make_flow()

        result = asyncio.run(flow.async_step_server(None))

        assert result["type"] == "abort"
        assert result["reason"] == "unsupported_home_assistant"
        assert result["description_placeholders"] == {
            "installed": "2025.9.4",
            "required": "2026.6.0",
        }
        flow.async_set_unique_id.assert_not_awaited()


class TestOAuthCredsHint:
    """All three branches of the admin-only credentials hint on the options
    form (review gap: zero coverage on any of them)."""

    def test_non_legacy_mode_points_at_the_mode_selector(self):
        flow = _make_options_flow(
            options={const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_NONE}
        )
        hint = flow._oauth_creds_hint()
        assert "Set Authentication mode" in hint
        assert "Client ID" in hint

    def test_legacy_mode_before_minting_says_appear_after_start(self):
        flow = _make_options_flow(
            options={const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY}
        )
        hint = flow._oauth_creds_hint()
        assert "once the server" in hint

    def test_legacy_mode_live_creds_shows_values_without_caveat(self, monkeypatch):
        # Bound AND live (post-restart steady state): no restart caveat.
        monkeypatch.setattr(cf, "_legacy_credentials_active", lambda *a: True)
        monkeypatch.setattr(cf, "_legacy_restart_pending", lambda *a: False)
        flow = _make_options_flow(
            options={const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY},
            data={
                const.DATA_OAUTH_CLIENT_ID: "hamcp-abc",
                const.DATA_OAUTH_CLIENT_SECRET: "s3cr3t",
            },
        )
        hint = flow._oauth_creds_hint()
        assert "Client ID: hamcp-abc" in hint
        assert "Client Secret: s3cr3t" in hint
        assert "not serving these yet" not in hint

    def test_legacy_mode_first_enable_carries_not_live_caveat(self, monkeypatch):
        # Review finding: mid-session first enable binds the current identity
        # (active True) but /authorize is not live until the restart -- the
        # hint must caveat, matching the startup log's first-enable branch.
        monkeypatch.setattr(cf, "_legacy_credentials_active", lambda *a: True)
        monkeypatch.setattr(cf, "_legacy_restart_pending", lambda *a: True)
        flow = _make_options_flow(
            options={const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY},
            data={
                const.DATA_OAUTH_CLIENT_ID: "hamcp-abc",
                const.DATA_OAUTH_CLIENT_SECRET: "s3cr3t",
            },
        )
        hint = flow._oauth_creds_hint()
        assert "Client ID: hamcp-abc" in hint
        assert "not serving these yet" in hint

    def test_legacy_mode_pending_rotation_carries_restart_caveat(self, monkeypatch):
        # Review finding on #1880: after a rotation, entry.data updates
        # immediately but the bound views keep the previous identity until
        # restart -- the hint must say the shown values do not work yet.
        monkeypatch.setattr(cf, "_legacy_credentials_active", lambda *a: False)
        flow = _make_options_flow(
            options={const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY},
            data={
                const.DATA_OAUTH_CLIENT_ID: "hamcp-new",
                const.DATA_OAUTH_CLIENT_SECRET: "new-secret",
            },
        )
        hint = flow._oauth_creds_hint()
        assert "Client ID: hamcp-new" in hint
        assert "Client Secret: new-secret" in hint
        assert "not serving these yet" in hint
        assert "restart" in hint


class TestOptionsFlowDispatch:
    def test_server_entry_gets_server_options_flow(self):
        entry = SimpleNamespace(data={const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_SERVER})
        result = cf.HaMcpToolsConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, cf.HaMcpServerOptionsFlow)

    def test_tools_entry_gets_info_options_flow(self):
        entry = SimpleNamespace(data={const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS})
        result = cf.HaMcpToolsConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, cf.HaMcpToolsInfoOptionsFlow)

    def test_missing_entry_type_defaults_to_info_options(self):
        # A pre-existing (pre-fold) tools entry carries no entry_type key; it must
        # be treated as "tools", so it gets the informational tools flow, never
        # the server options flow.
        entry = SimpleNamespace(data={})
        result = cf.HaMcpToolsConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, cf.HaMcpToolsInfoOptionsFlow)
        assert not isinstance(result, cf.HaMcpServerOptionsFlow)


class TestToolsInfoOptionsFlow:
    def test_init_shows_info_form_under_tools_info_step(self):
        # The tools entry's options flow shows an informational form on a step id
        # distinct from the server flow's ``init`` (a shared id would collide in
        # strings.json). The schema is empty — there is nothing to configure yet.
        flow = cf.HaMcpToolsInfoOptionsFlow()
        flow.async_show_form = MagicMock(
            side_effect=lambda **kw: {"type": "form", **kw}
        )
        form = asyncio.run(flow.async_step_init(None))
        assert form["type"] == "form"
        assert form["step_id"] == "tools_info"
        # Empty schema: nothing to configure yet.
        assert list(form["data_schema"].schema) == []

    def test_submitting_info_form_creates_empty_options_entry(self):
        # HA routes the info form's submit to async_step_tools_info, which
        # persists an empty options payload (title "").
        flow = cf.HaMcpToolsInfoOptionsFlow()
        flow.async_create_entry = MagicMock(
            side_effect=lambda **kw: {"type": "entry", **kw}
        )
        result = asyncio.run(flow.async_step_tools_info({}))
        assert result["type"] == "entry"
        assert result["title"] == ""
        assert result["data"] == {}


class TestServerOptionsFlow:
    def test_init_shows_form_with_connect_url_placeholder(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        assert form["type"] == "form"
        assert form["step_id"] == "init"
        assert "mcp_abc" in form["description_placeholders"]["connect_url"]

    def test_versions_placeholder_present_and_populated(self, monkeypatch):
        # The Configure form carries a "versions" placeholder that names the
        # component version (from the manifest) and the installed server version.
        flow = _make_options_flow(
            options={const.OPT_CHANNEL: const.CHANNEL_DEV},
            data={const.DATA_WEBHOOK_ID: "mcp_abc"},
        )
        flow.hass = MagicMock()
        # The server-version read is offloaded to the executor (blocking I/O);
        # make the mock actually run the callable.
        flow.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
        monkeypatch.setattr(
            cf,
            "async_get_integration",
            AsyncMock(return_value=SimpleNamespace(version="0.14.0")),
        )
        monkeypatch.setattr(cf, "_installed_server_version", lambda: "7.9.0")

        form = asyncio.run(flow.async_step_init(None))
        versions = form["description_placeholders"]["versions"]
        # startswith, not equality: the tools-module status line (#1996) rides
        # the same placeholder below the version line.
        assert versions.startswith(
            "Component 0.14.0 - Server ha-mcp 7.9.0 (dev channel)"
        )

    def test_versions_placeholder_is_failure_proof(self, monkeypatch):
        # A broken version read must not break the form: the component read
        # failing and the server read raising both degrade to safe text.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        flow.hass = MagicMock()
        flow.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
        monkeypatch.setattr(
            cf, "async_get_integration", AsyncMock(side_effect=RuntimeError("boom"))
        )

        def _raise():
            raise RuntimeError("metadata boom")

        monkeypatch.setattr(cf, "_installed_server_version", _raise)

        form = asyncio.run(flow.async_step_init(None))  # must not raise
        versions = form["description_placeholders"]["versions"]
        assert versions.startswith(
            "Component unknown - Server ha-mcp not installed yet (stable channel)"
        )

    def test_versions_placeholder_server_not_installed_yet(self, monkeypatch):
        # Before the server package is installed, the server half reads
        # "not installed yet" rather than a bogus version.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        monkeypatch.setattr(cf, "_installed_server_version", lambda: None)
        form = asyncio.run(flow.async_step_init(None))
        versions = form["description_placeholders"]["versions"]
        # No hass on the flow ⇒ component "unknown"; server not installed yet.
        assert "not installed yet" in versions
        assert versions.startswith("Component unknown")

    @staticmethod
    def _hass_with_entries(entries):
        """hass whose config-entry registry returns ``entries`` for the domain."""
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
        hass.config_entries.async_entries = MagicMock(return_value=entries)
        return hass

    def test_versions_placeholder_flags_missing_tools_entry(self, monkeypatch):
        # #1996: a server-entry-only install shows a status line directly under
        # the version line pointing at the missing File & YAML Tools entry —
        # users otherwise never learn about the second entry until a tool fails.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        flow.hass = self._hass_with_entries(
            [SimpleNamespace(data={const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_SERVER})]
        )
        monkeypatch.setattr(
            cf,
            "async_get_integration",
            AsyncMock(return_value=SimpleNamespace(version="1.2.4")),
        )
        monkeypatch.setattr(cf, "_installed_server_version", lambda: "7.14.1")

        form = asyncio.run(flow.async_step_init(None))
        versions = form["description_placeholders"]["versions"]
        assert versions.startswith(
            "Component 1.2.4 - Server ha-mcp 7.14.1 (stable channel)"
        )
        assert "Not installed" in versions
        assert "Add entry" in versions

    def test_versions_placeholder_shows_tools_entry_installed(self, monkeypatch):
        # Both entries present and the tools entry loaded → the status line
        # reads Installed. A legacy tools entry without the entry_type
        # discriminator counts too (missing entry_type means tools, mirroring
        # async_setup_entry).
        monkeypatch.setattr(
            cf,
            "async_get_integration",
            AsyncMock(return_value=SimpleNamespace(version="1.2.4")),
        )
        monkeypatch.setattr(cf, "_installed_server_version", lambda: "7.14.1")
        for tools_data in (
            {const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS},
            {},  # legacy pre-#1527 entry
        ):
            flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
            flow.hass = self._hass_with_entries(
                [
                    SimpleNamespace(
                        data={const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_SERVER}
                    ),
                    SimpleNamespace(data=tools_data, state=cf.ConfigEntryState.LOADED),
                ]
            )
            form = asyncio.run(flow.async_step_init(None))
            versions = form["description_placeholders"]["versions"]
            assert "tools module (optional): Installed" in versions
            assert "Not installed" not in versions
            assert "not loaded" not in versions

    def test_versions_placeholder_flags_unloaded_tools_entry(self, monkeypatch):
        # A tools entry that exists but is not loaded (disabled / setup failed)
        # serves no services, so it must not read as plain Installed — the line
        # points at enabling/reloading the existing entry instead.
        monkeypatch.setattr(
            cf,
            "async_get_integration",
            AsyncMock(return_value=SimpleNamespace(version="1.2.4")),
        )
        monkeypatch.setattr(cf, "_installed_server_version", lambda: "7.14.1")
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        flow.hass = self._hass_with_entries(
            [
                SimpleNamespace(data={const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_SERVER}),
                SimpleNamespace(
                    data={const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS},
                    state=cf.ConfigEntryState.NOT_LOADED,
                ),
            ]
        )
        form = asyncio.run(flow.async_step_init(None))
        versions = form["description_placeholders"]["versions"]
        assert "not loaded" in versions
        assert "enable or reload" in versions

    def test_webhook_auth_is_first_option_field(self):
        # #1875: Authentication mode sits at the top of the options form,
        # directly under the connect URLs, so users find it without scrolling.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        markers = list(form["data_schema"].schema)
        assert markers[0].schema == const.OPT_WEBHOOK_AUTH

    def test_channel_defaults_to_stable(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        channel = next(
            m for m in form["data_schema"].schema if m.schema == const.OPT_CHANNEL
        )
        assert channel.default() == const.CHANNEL_STABLE

    def test_auto_update_defaults_on(self):
        # The auto-update checkbox is present and defaults on (checked) when the
        # option has never been saved.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        marker = next(
            m for m in form["data_schema"].schema if m.schema == const.OPT_AUTO_UPDATE
        )
        assert marker.default() is True

    def test_form_prefills_every_field_from_saved_options(self):
        # Review gap: the form must show the user's SAVED values, not the
        # defaults, for every field (a regression here silently reverts a
        # user's config on the next save). Dropdowns/toggles pre-fill via the
        # schema default; the optional text fields pre-fill via suggested_value
        # (a default there would make them impossible to clear — see
        # test_clearing_an_override_field_sticks).
        saved = {
            const.OPT_CHANNEL: const.CHANNEL_DEV,
            const.OPT_AUTO_UPDATE: False,
            const.OPT_SERVER_PORT: 12345,
            const.OPT_BIND_HOST: const.BIND_HOST_LOOPBACK,
            const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_HA,
            const.OPT_PIP_SPEC: "ha-mcp==0.0.1",
            const.OPT_SERVER_URL: "https://ha.example:8123",
            # Saved False (non-default) proves the LLM-API toggle prefills.
            const.OPT_ENABLE_LLM_API: False,
            # Saved full (non-default) proves the exposure selector prefills.
            const.OPT_LLM_API_EXPOSURE: const.EXPOSURE_FULL,
            const.OPT_EXTERNAL_URL: "https://ha.example.com",
            const.OPT_WEBHOOK_ID_OVERRIDE: "my_custom_hook",
            const.OPT_SECRET_PATH_OVERRIDE: "/custom_path",
            const.OPT_OAUTH_CLIENT_ID: "hamcp-deadbeef",
            const.OPT_OAUTH_CLIENT_SECRET: "super-secret-value",
        }
        flow = _make_options_flow(
            data={const.DATA_WEBHOOK_ID: "mcp_abc"}, options=saved
        )
        form = asyncio.run(flow.async_step_init(None))
        markers = {m.schema: m for m in form["data_schema"].schema}

        # Optional text fields pre-fill via suggested_value so they stay
        # clearable; every other field pre-fills via the schema default.
        text_fields = (
            const.OPT_PIP_SPEC,
            const.OPT_SERVER_URL,
            const.OPT_EXTERNAL_URL,
            const.OPT_WEBHOOK_ID_OVERRIDE,
            const.OPT_SECRET_PATH_OVERRIDE,
            const.OPT_OAUTH_CLIENT_ID,
            const.OPT_OAUTH_CLIENT_SECRET,
        )
        for key in text_fields:
            assert markers[key].description["suggested_value"] == saved[key]

        defaults = {
            key: m.default() for key, m in markers.items() if key not in text_fields
        }
        # regenerate_secrets is a one-shot action, never pre-filled True;
        # enable_webhook / enable_startup_notification / enable_sidebar_panel
        # default on when unsaved. Pop off the schema (not inside assert, which
        # `python -O` would strip) before comparing the remainder.
        regenerate_default = defaults.pop(const.OPT_REGENERATE_SECRETS)
        assert regenerate_default is False
        oauth_regenerate_default = defaults.pop(const.OPT_OAUTH_REGENERATE)
        assert oauth_regenerate_default is False
        webhook_default = defaults.pop(const.OPT_ENABLE_WEBHOOK)
        assert webhook_default is True
        notification_default = defaults.pop(const.OPT_ENABLE_STARTUP_NOTIFICATION)
        assert notification_default is True
        panel_default = defaults.pop(const.OPT_ENABLE_SIDEBAR_PANEL)
        assert panel_default is True
        assert defaults == {k: v for k, v in saved.items() if k not in text_fields}

    def test_init_submit_round_trips_input_into_entry(self):
        flow = _make_options_flow()
        user_input = {
            const.OPT_CHANNEL: const.CHANNEL_DEV,
            const.OPT_SERVER_PORT: 9999,
            const.OPT_BIND_HOST: const.BIND_HOST_ALL,
            const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_HA,
            const.OPT_PIP_SPEC: "ha-mcp @ https://example/x.tgz",  # real override
            # A REAL URL override — DEFAULT_LOOPBACK_URL would be normalized
            # away (see test_server_url_default_loopback_normalized_away).
            const.OPT_SERVER_URL: "http://ha.internal:8123",
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
            const.OPT_OAUTH_CLIENT_ID: "",
            const.OPT_OAUTH_CLIENT_SECRET: "",
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

    def test_server_url_whitespace_is_dropped_to_default(self):
        # A whitespace-only Home Assistant URL must not be stored verbatim: it is
        # truthy, so it would bypass the consumer's empty -> loopback fallback and
        # break the connection. _normalize drops it so the default applies.
        flow = _make_options_flow()
        result = asyncio.run(
            flow.async_step_init(
                {const.OPT_CHANNEL: const.CHANNEL_STABLE, const.OPT_SERVER_URL: "   "}
            )
        )
        assert const.OPT_SERVER_URL not in result["data"]

    def test_server_url_trailing_slash_stripped(self):
        # A real URL is kept, with any trailing slash trimmed.
        flow = _make_options_flow()
        result = asyncio.run(
            flow.async_step_init(
                {
                    const.OPT_CHANNEL: const.CHANNEL_STABLE,
                    const.OPT_SERVER_URL: "http://ha.local:8123/",
                }
            )
        )
        assert result["data"][const.OPT_SERVER_URL] == "http://ha.local:8123"

    def test_server_url_default_loopback_normalized_away(self):
        # Older forms pre-filled DEFAULT_LOOPBACK_URL as suggested_value, so a
        # plain options save stored it as an explicit override. Persisting it
        # would pin the plaintext scheme and port, defeating the SSL/port-aware
        # loopback derivation (issue #1890) — collapse it to "no override".
        flow = _make_options_flow()
        result = asyncio.run(
            flow.async_step_init(
                {
                    const.OPT_CHANNEL: const.CHANNEL_STABLE,
                    const.OPT_SERVER_URL: const.DEFAULT_LOOPBACK_URL + "/",
                }
            )
        )
        assert const.OPT_SERVER_URL not in result["data"]

    def test_pip_spec_field_empty_when_no_override(self):
        # The "leave blank to follow the channel" field must actually BE
        # blank when no override is stored — pre-filling the default dist
        # name as a hint made it always look populated (and showed the
        # STABLE dist name even on the dev channel). A saved override still
        # pre-fills: see test_form_prefills_every_field_from_saved_options.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        marker = next(
            m for m in form["data_schema"].schema if m.schema == const.OPT_PIP_SPEC
        )
        assert marker.description["suggested_value"] == ""

    def test_server_url_field_empty_when_no_override(self):
        # Same pattern as pip_spec above: pre-filling DEFAULT_LOOPBACK_URL made
        # every options save persist the constant as an explicit override,
        # pinning the plaintext scheme/port that the #1890 loopback derivation
        # replaces. No override stored -> empty field.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        marker = next(
            m for m in form["data_schema"].schema if m.schema == const.OPT_SERVER_URL
        )
        assert marker.description["suggested_value"] == ""

    def test_clearing_an_override_field_sticks(self):
        # Regression: emptying an optional text field must persist as cleared.
        # HA's frontend DROPS an emptied optional field from the submitted
        # payload; the flow manager then validates that payload against the
        # shown schema (filling voluptuous defaults) before the step handler
        # runs — the layer a direct-handler unit test skips. A schema
        # ``default=<saved value>`` silently re-injects the old value there, so
        # clearing never took: the pip-spec override kept re-installing the old
        # build and the field re-appeared populated on reopen. Pre-filling with
        # ``suggested_value`` (not ``default``) has no such re-injection, so the
        # cleared state sticks.
        clearable = {
            const.OPT_PIP_SPEC: "ha-mcp @ https://example/x.tgz",
            const.OPT_SERVER_URL: "https://ha.example:8123",
            const.OPT_EXTERNAL_URL: "https://ha.example.com",
            const.OPT_WEBHOOK_ID_OVERRIDE: "my_custom_hook",
            const.OPT_SECRET_PATH_OVERRIDE: "/custom_path",
        }
        for field in clearable:
            flow = _make_options_flow(
                data={const.DATA_WEBHOOK_ID: "mcp_abc"}, options=dict(clearable)
            )
            form = asyncio.run(flow.async_step_init(None))
            # The user cleared exactly one field; the frontend omits it and
            # submits the rest. Validate through the shown schema exactly as the
            # flow manager does, then hand the result to the step.
            submitted = {k: v for k, v in clearable.items() if k != field}
            validated = form["data_schema"](submitted)
            result = asyncio.run(flow.async_step_init(validated))
            assert result["data"].get(field, "") == "", (
                f"clearing {field!r} did not persist: {result['data'].get(field)!r}"
            )

    def test_no_enable_toggle_option_exists(self):
        # Regression guard for the single-instance pivot: the enable/disable
        # toggle was dropped (entry-exists = server runs).
        assert not hasattr(const, "OPT_EMBEDDED_ENABLED")

    def test_new_toggles_default_on_for_fresh_entry(self):
        # Both UX toggles (start-up notification, sidebar panel) are present in
        # the rendered schema and default on when the entry has never stored
        # them.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        markers = {m.schema: m for m in form["data_schema"].schema}
        assert const.OPT_ENABLE_STARTUP_NOTIFICATION in markers
        assert markers[const.OPT_ENABLE_STARTUP_NOTIFICATION].default() is True
        assert const.OPT_ENABLE_SIDEBAR_PANEL in markers
        assert markers[const.OPT_ENABLE_SIDEBAR_PANEL].default() is True

    def test_new_toggles_placed_right_after_enable_webhook(self):
        # Contract: both toggles sit immediately after the enable_webhook field.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        keys = [m.schema for m in form["data_schema"].schema]
        webhook_idx = keys.index(const.OPT_ENABLE_WEBHOOK)
        assert set(keys[webhook_idx + 1 : webhook_idx + 3]) == {
            const.OPT_ENABLE_STARTUP_NOTIFICATION,
            const.OPT_ENABLE_SIDEBAR_PANEL,
        }

    def test_new_toggles_prefill_stored_false(self):
        # Re-opening the form after saving False shows the stored False, not the
        # default True (a regression here silently re-enables an opted-out UI).
        flow = _make_options_flow(
            data={const.DATA_WEBHOOK_ID: "mcp_abc"},
            options={
                const.OPT_ENABLE_STARTUP_NOTIFICATION: False,
                const.OPT_ENABLE_SIDEBAR_PANEL: False,
            },
        )
        form = asyncio.run(flow.async_step_init(None))
        markers = {m.schema: m for m in form["data_schema"].schema}
        assert markers[const.OPT_ENABLE_STARTUP_NOTIFICATION].default() is False
        assert markers[const.OPT_ENABLE_SIDEBAR_PANEL].default() is False

    def test_submitting_false_stores_false_for_new_toggles(self):
        # Submitting the form with both toggles unchecked persists False.
        flow = _make_options_flow()
        user_input = {
            const.OPT_CHANNEL: const.CHANNEL_STABLE,
            const.OPT_ENABLE_STARTUP_NOTIFICATION: False,
            const.OPT_ENABLE_SIDEBAR_PANEL: False,
        }
        result = asyncio.run(flow.async_step_init(user_input))
        assert result["type"] == "entry"
        assert result["data"][const.OPT_ENABLE_STARTUP_NOTIFICATION] is False
        assert result["data"][const.OPT_ENABLE_SIDEBAR_PANEL] is False

    def test_panel_hint_contains_panel_url_when_sidebar_enabled(self):
        # panel_hint is a non-empty sentence naming the panel URL when the
        # sidebar option is enabled (absent counts as enabled).
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        hint = form["description_placeholders"]["panel_hint"]
        assert hint
        assert "(/ha-mcp)" in hint

    def test_panel_hint_empty_when_sidebar_disabled(self):
        # With the sidebar option stored False, the panel hint collapses to "".
        flow = _make_options_flow(
            data={const.DATA_WEBHOOK_ID: "mcp_abc"},
            options={const.OPT_ENABLE_SIDEBAR_PANEL: False},
        )
        form = asyncio.run(flow.async_step_init(None))
        assert form["description_placeholders"]["panel_hint"] == ""

    def test_connect_url_hint_uses_configured_port(self):
        flow = _make_options_flow(
            options={const.OPT_SERVER_PORT: 9999},
            data={
                const.DATA_WEBHOOK_ID: "mcp_abc",
                const.DATA_SECRET_PATH: "/private_x",
            },
        )
        hint = asyncio.run(flow._connect_url_hint())
        assert "/api/webhook/mcp_abc" in hint
        # The LAN hint uses the CONFIGURED port, not the 9584 default.
        assert ":9999/private_x" in hint

    def test_connect_url_hint_before_start_points_at_log(self):
        flow = _make_options_flow(data={})
        hint = asyncio.run(flow._connect_url_hint()).lower()
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

        def fake_builder(hass, entry, *, webhook_enabled=True, extra_hosts=None):
            calls.append(
                {
                    "hass": hass,
                    "webhook_enabled": webhook_enabled,
                    "extra_hosts": extra_hosts,
                }
            )
            return [
                "https://example.duckdns.org/api/webhook/mcp_abc",
                "http://192.168.1.150:9584/private_x (direct access)",
            ]

        async def fake_lan_hosts(hass):
            return ["10.0.1.3"]

        stub = ModuleType("custom_components.ha_mcp_tools.embedded_setup")
        stub.build_connect_urls = fake_builder
        stub.async_get_lan_hosts = fake_lan_hosts
        flow = _make_options_flow(
            options={const.OPT_ENABLE_WEBHOOK: False},
            data={"webhook_id": "mcp_abc", "secret_path": "/private_x"},
        )
        flow.hass = MagicMock()
        orig = sys.modules.get("custom_components.ha_mcp_tools.embedded_setup")
        sys.modules["custom_components.ha_mcp_tools.embedded_setup"] = stub
        try:
            hint = asyncio.run(flow._connect_url_hint())
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
        # The enumerated adapter hosts are forwarded so per-interface URLs
        # surface on the Configure screen too (#1862).
        assert calls[0]["extra_hosts"] == ["10.0.1.3"]

    def _hint_with_stub_builder(self, builder, *, data, options=None):
        """Call ``_connect_url_hint`` with ``build_connect_urls`` stubbed.

        Injects a fake ``embedded_setup`` module so the flow's module-local
        ``from .embedded_setup import build_connect_urls`` resolves to ``builder``
        without importing the real module (which needs a full HA install).
        """

        async def fake_lan_hosts(hass):
            return []

        stub = ModuleType("custom_components.ha_mcp_tools.embedded_setup")
        stub.build_connect_urls = builder
        stub.async_get_lan_hosts = fake_lan_hosts
        flow = _make_options_flow(data=data, options=options)
        flow.hass = MagicMock()
        orig = sys.modules.get("custom_components.ha_mcp_tools.embedded_setup")
        sys.modules["custom_components.ha_mcp_tools.embedded_setup"] = stub
        try:
            return asyncio.run(flow._connect_url_hint())
        finally:
            if orig is None:
                del sys.modules["custom_components.ha_mcp_tools.embedded_setup"]
            else:
                sys.modules["custom_components.ha_mcp_tools.embedded_setup"] = orig

    def test_connect_url_hint_falls_back_when_builder_raises(self):
        # A resolution bug in build_connect_urls must not escape and take down
        # the whole options form: the hint degrades to the placeholder form.
        def boom_builder(hass, entry, *, webhook_enabled=True, extra_hosts=None):
            raise RuntimeError("resolution boom")

        hint = self._hint_with_stub_builder(
            boom_builder, data={"webhook_id": "mcp_abc", "secret_path": "/private_x"}
        )
        assert "<your-home-assistant-url>" in hint
        assert "/api/webhook/mcp_abc" in hint

    def test_connect_url_hint_falls_back_when_builder_returns_empty(self):
        # An empty resolver result (nothing resolvable yet) also degrades to the
        # placeholder form rather than an empty "Connect URL(s):" header.
        def empty_builder(hass, entry, *, webhook_enabled=True, extra_hosts=None):
            return []

        hint = self._hint_with_stub_builder(
            empty_builder, data={"webhook_id": "mcp_abc", "secret_path": "/private_x"}
        )
        assert "<your-home-assistant-url>" in hint
        assert "/api/webhook/mcp_abc" in hint

    def test_connect_url_hint_local_only_never_shows_webhook_url(self):
        """Webhook disabled + nothing resolvable: no dead webhook URL.

        With remote access via webhook off, the webhook endpoint is never
        registered - the fallback must state local-only mode with the real
        loopback direct URL instead of rendering a webhook URL that 404s.
        """

        def empty_builder(hass, entry, *, webhook_enabled=True, extra_hosts=None):
            return []

        hint = self._hint_with_stub_builder(
            empty_builder,
            data={"webhook_id": "mcp_abc", "secret_path": "/private_x"},
            options={const.OPT_ENABLE_WEBHOOK: False, const.OPT_SERVER_PORT: 9999},
        )
        assert hint.startswith("Remote access via webhook is disabled")
        assert "http://127.0.0.1:9999/private_x" in hint
        assert "/api/webhook/" not in hint

    def test_connect_url_hint_local_only_when_builder_raises(self):
        """Webhook disabled + resolver RAISES: same local-only contract.

        The ``except`` path must also fall through to the local-only message
        (loopback direct URL, no webhook URL), not the placeholder webhook form
        — a resolution error must not resurrect a webhook URL that 404s.
        """

        def boom_builder(hass, entry, *, webhook_enabled=True, extra_hosts=None):
            raise RuntimeError("resolution boom")

        hint = self._hint_with_stub_builder(
            boom_builder,
            data={"webhook_id": "mcp_abc", "secret_path": "/private_x"},
            options={const.OPT_ENABLE_WEBHOOK: False},
        )
        assert hint.startswith("Remote access via webhook is disabled")
        assert "http://127.0.0.1:9584/private_x" in hint  # DEFAULT_SERVER_PORT
        assert "/api/webhook/" not in hint
