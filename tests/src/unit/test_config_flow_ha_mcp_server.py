"""Unit tests for the ha_mcp_server config + options flow (issue #1527).

Single-instance config flow (a plain confirm step creates the entry — the entry
existing = the server runs) plus an options flow for port / bind host / webhook
auth / pip spec / server URL, with NO enable toggle. Drives the flow state machine
with the HA framework methods stubbed, asserting each routing decision directly.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ._embedded_stubs import install

install()


# config_flow subclasses ConfigFlow / OptionsFlow, so unlike the generic
# MagicMock config_entries the other embedded tests use we need real, subclassable
# bases here. ``callback`` must be identity so ``async_get_options_flow`` builds.
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
_core = sys.modules.get("homeassistant.core") or MagicMock()
_core.callback = lambda func: func
sys.modules["homeassistant.core"] = _core

# Determinism under pytest-xdist: peer unit files register a MagicMock voluptuous
# via sys.modules.setdefault, and whether that or the real package is bound is
# import-order dependent across workers. The channel tests below inspect the REAL
# vol.Schema markers (marker.schema / marker.default()), so evict any stub — plus
# any cached ha_mcp_server.config_flow that may have bound it — so config_flow's
# own ``import voluptuous`` re-binds the real package (a hard dependency, always
# installed) regardless of worker file composition/order.
for _name in [
    n for n in sys.modules if n == "voluptuous" or n.startswith("voluptuous.")
]:
    del sys.modules[_name]
sys.modules.pop("ha_mcp_server.config_flow", None)

import ha_mcp_server.config_flow as cf  # noqa: E402
import ha_mcp_server.const as const  # noqa: E402


def _make_flow() -> cf.HaMcpServerConfigFlow:
    flow = cf.HaMcpServerConfigFlow()
    flow.async_set_unique_id = AsyncMock(return_value=None)
    flow._abort_if_unique_id_configured = MagicMock(return_value=None)
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


class TestConfigFlow:
    def test_user_step_shows_confirm_form(self):
        flow = _make_flow()
        form = asyncio.run(flow.async_step_user(None))
        assert form["type"] == "form"
        assert form["step_id"] == "user"

    def test_user_step_creates_single_entry(self):
        flow = _make_flow()
        entry = asyncio.run(flow.async_step_user({}))
        assert entry["type"] == "entry"
        assert entry["title"] == cf._ENTRY_TITLE
        assert entry["data"] == {}
        assert entry["options"] == {}

    def test_single_instance_guard(self):
        flow = _make_flow()
        asyncio.run(flow.async_step_user(None))
        flow.async_set_unique_id.assert_awaited_once_with(const.DOMAIN)
        flow._abort_if_unique_id_configured.assert_called_once()


class TestOptionsFlow:
    def test_get_options_flow_returns_options_flow(self):
        result = cf.HaMcpServerConfigFlow.async_get_options_flow(MagicMock())
        assert isinstance(result, cf.HaMcpServerOptionsFlow)

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
