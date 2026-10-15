"""Server-entry setup and its options (#2427).

Split from test_config_flow.py, whose flow fakes and Home Assistant stubs it
reuses.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from .test_config_flow import _make_flow, _make_options_flow, cf, const


class TestServerBranch:
    def test_server_step_shows_confirm_form(self):
        flow = _make_flow()
        form = asyncio.run(flow.async_step_server(None))
        assert form["type"] == "form"
        assert form["step_id"] == "server"

    @staticmethod
    def _defaults(form) -> dict:
        return {
            marker.schema: marker.default() for marker in form["data_schema"].schema
        }

    def test_new_install_defaults_to_no_webhook_and_loopback_only(self):
        # #2427 (HACS review): a fresh install exposes nothing beyond the Home
        # Assistant machine until the admin chooses otherwise during setup.
        flow = _make_flow()
        form = asyncio.run(flow.async_step_server(None))
        defaults = self._defaults(form)

        entry = asyncio.run(flow.async_step_server(defaults))

        assert entry["type"] == "entry"
        assert entry["title"] == cf.SERVER_ENTRY_TITLE
        assert entry["data"] == {const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_SERVER}
        assert entry["options"][const.OPT_ENABLE_WEBHOOK] is False
        assert entry["options"][const.OPT_BIND_HOST] == const.BIND_HOST_LOOPBACK

    def test_new_install_options_are_saved_not_inherited(self):
        # Seeding them keeps later default changes from moving this entry, and
        # leaves entries created before this change on the defaults they had.
        flow = _make_flow()
        form = asyncio.run(flow.async_step_server(None))
        entry = asyncio.run(flow.async_step_server(self._defaults(form)))
        assert {
            const.OPT_ENABLE_WEBHOOK,
            const.OPT_WEBHOOK_AUTH,
            const.OPT_BIND_HOST,
        } <= set(entry["options"])

    @pytest.mark.parametrize(
        "mode",
        [const.WEBHOOK_AUTH_NONE, const.WEBHOOK_AUTH_HA, const.WEBHOOK_AUTH_LEGACY],
    )
    def test_choosing_remote_access_enables_the_webhook_with_that_auth(self, mode):
        flow = _make_flow()
        entry = asyncio.run(
            flow.async_step_server(
                {cf.SETUP_REMOTE_ACCESS: mode, const.OPT_BIND_HOST: const.BIND_HOST_ALL}
            )
        )
        assert entry["options"][const.OPT_ENABLE_WEBHOOK] is True
        assert entry["options"][const.OPT_WEBHOOK_AUTH] == mode
        assert entry["options"][const.OPT_BIND_HOST] == const.BIND_HOST_ALL

    def test_disabled_remote_access_keeps_ha_sign_in_for_a_later_enable(self):
        # Turning the webhook on later in Configure must not land on the
        # secret-URL mode by default.
        flow = _make_flow()
        entry = asyncio.run(
            flow.async_step_server(
                {
                    cf.SETUP_REMOTE_ACCESS: cf.REMOTE_ACCESS_DISABLED,
                    const.OPT_BIND_HOST: const.BIND_HOST_LOOPBACK,
                }
            )
        )
        assert entry["options"][const.OPT_WEBHOOK_AUTH] == const.WEBHOOK_AUTH_HA

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
            "required": "2026.8.0",
        }
        flow.async_set_unique_id.assert_not_awaited()


class TestOAuthCallbackAllowlistOption:
    """The Configure screen edits the none-mode callback allowlist (#2427)."""

    CALLBACK = "https://chatgpt.example/cb"

    def _suggested(self, form):
        marker = next(
            m
            for m in form["data_schema"].schema
            if m.schema == const.OPT_OAUTH_REDIRECT_ALLOWLIST
        )
        return marker.description["suggested_value"]

    def test_an_unedited_entry_shows_the_default_list(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        assert self._suggested(form) == list(const.DEFAULT_OAUTH_REDIRECT_ALLOWLIST)

    def test_saves_the_entered_callbacks(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        result = asyncio.run(
            flow.async_step_init(
                {const.OPT_OAUTH_REDIRECT_ALLOWLIST: [f" {self.CALLBACK} "]}
            )
        )
        assert result["data"][const.OPT_OAUTH_REDIRECT_ALLOWLIST] == [self.CALLBACK]

    def test_an_unusable_callback_blocks_the_save(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        result = asyncio.run(
            flow.async_step_init(
                {const.OPT_OAUTH_REDIRECT_ALLOWLIST: ["http://not-loopback/cb"]}
            )
        )
        assert result["errors"] == {
            const.OPT_OAUTH_REDIRECT_ALLOWLIST: "invalid_oauth_callback"
        }

    def test_saving_the_untouched_default_keeps_following_it(self):
        # Pinning the default would hide callbacks a later release adds to it.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        result = asyncio.run(
            flow.async_step_init(
                {
                    const.OPT_OAUTH_REDIRECT_ALLOWLIST: list(
                        const.DEFAULT_OAUTH_REDIRECT_ALLOWLIST
                    )
                }
            )
        )
        assert const.OPT_OAUTH_REDIRECT_ALLOWLIST not in result["data"]

    def test_removing_every_callback_sticks(self):
        # Home Assistant's frontend omits an emptied optional field.
        flow = _make_options_flow(
            options={const.OPT_OAUTH_REDIRECT_ALLOWLIST: [self.CALLBACK]},
            data={const.DATA_WEBHOOK_ID: "mcp_abc"},
        )
        result = asyncio.run(flow.async_step_init({}))
        assert result["data"][const.OPT_OAUTH_REDIRECT_ALLOWLIST] == []


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
