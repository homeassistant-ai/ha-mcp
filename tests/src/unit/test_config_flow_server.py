"""Server-entry setup and its options (#2427).

Split from test_config_flow.py, whose flow fakes and Home Assistant stubs it
reuses.
"""

from __future__ import annotations

import asyncio

import pytest

from .test_config_flow import _make_flow, cf, const


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
