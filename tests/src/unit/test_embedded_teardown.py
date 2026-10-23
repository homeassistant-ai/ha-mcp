"""Server teardown: unload, reload and the access switch all stop the server
through it (#2801).

Split from test_embedded_setup.py, whose fakes and fixtures it reuses.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.const import DATA_MANAGER, DOMAIN  # noqa: E402

from .test_embedded_setup import (  # noqa: E402
    _make_entry,
    _make_hass,
    esetup,
    install_spies,
    make_fake_manager,
)


@pytest.fixture
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> type:
    return make_fake_manager(monkeypatch)


@pytest.fixture(autouse=True)
def _spy(monkeypatch: pytest.MonkeyPatch) -> None:
    install_spies(monkeypatch)


class TestTeardown:
    async def test_unregisters_and_stops_without_revoking(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()
        await esetup.async_bring_up_server(hass, entry)
        fake_manager.async_stop.reset_mock()

        await esetup.async_teardown_server(hass)

        esetup.async_unregister_webhook.assert_awaited()
        esetup.async_unregister_llm_api.assert_called()
        fake_manager.async_stop.assert_awaited_once()
        assert DATA_MANAGER not in hass.data.get(DOMAIN, {})
        # A reload must keep the provisioned token.
        fake_manager.async_revoke_credentials.assert_not_awaited()

    async def test_teardown_is_noop_when_not_running(self, fake_manager):
        hass = _make_hass()
        await esetup.async_teardown_server(hass)  # must not raise
        esetup.async_unregister_webhook.assert_awaited_once()

    async def test_port_closes_even_when_the_webhook_fails_to_unregister(
        self, fake_manager, monkeypatch
    ):
        # The direct port is a way in of its own; turning access off must close
        # it whatever happens to the webhook.
        hass = _make_hass()
        await esetup.async_bring_up_server(hass, _make_entry())
        fake_manager.async_stop.reset_mock()
        esetup.async_unregister_webhook.side_effect = RuntimeError("close failed")
        dismiss = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_dismiss", dismiss)

        with pytest.raises(RuntimeError):
            await esetup.async_teardown_server(hass)

        fake_manager.async_stop.assert_awaited_once()
        dismiss.assert_called_once_with(hass, esetup._NOTIFICATION_ID)

    async def test_stopped_server_leaves_no_running_notice_or_stale_repair(
        self, monkeypatch
    ):
        dismiss = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_dismiss", dismiss)
        hass = _make_hass()

        await esetup.async_teardown_server(hass)

        dismiss.assert_called_once_with(hass, esetup._NOTIFICATION_ID)
        cleared = {c.args[2] for c in esetup.ir.async_delete_issue.call_args_list}
        assert {
            esetup.ISSUE_START_FAILED,
            esetup.ISSUE_TOKEN_NEEDED,
            esetup.ISSUE_PACKAGE_FAILED,
        } <= cleared
