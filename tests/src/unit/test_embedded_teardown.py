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
    async def test_unregisters_and_stops_without_revoking(
        self, fake_manager: type
    ) -> None:
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

    async def test_closes_the_webhook_and_llm_api_before_stopping_the_server(
        self, fake_manager: type
    ) -> None:
        # Both forward calls to the server over loopback. Stopped first, they
        # would stay routed to a server that is going away, and a call in that
        # window fails (a 502 on the webhook, an error to the conversation
        # agent) instead of finding no endpoint.
        hass = _make_hass()
        await esetup.async_bring_up_server(hass, _make_entry())
        calls: list[str] = []
        esetup.async_unregister_llm_api.side_effect = lambda *_: calls.append("llm")
        esetup.async_unregister_webhook.side_effect = lambda *_: calls.append("webhook")
        fake_manager.async_stop.side_effect = lambda *_: calls.append("stop")

        await esetup.async_teardown_server(hass)

        assert set(calls[:2]) == {"llm", "webhook"}
        assert calls[2:] == ["stop"]

    async def test_teardown_is_noop_when_not_running(self, fake_manager: type) -> None:
        hass = _make_hass()
        await esetup.async_teardown_server(hass)  # must not raise
        esetup.async_unregister_webhook.assert_awaited_once()

    async def test_port_closes_even_when_the_webhook_fails_to_unregister(
        self, fake_manager: type, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    async def test_webhook_closes_even_when_the_llm_api_fails_to_unregister(
        self, fake_manager: type
    ) -> None:
        # Otherwise the webhook and its /readonly alias stay routed to a server
        # that is gone.
        hass = _make_hass()
        await esetup.async_bring_up_server(hass, _make_entry())
        fake_manager.async_stop.reset_mock()
        esetup.async_unregister_llm_api.side_effect = RuntimeError("unregister failed")

        with pytest.raises(RuntimeError, match="unregister failed"):
            await esetup.async_teardown_server(hass)

        esetup.async_unregister_webhook.assert_awaited_once()
        fake_manager.async_stop.assert_awaited_once()

    async def test_stopped_server_leaves_no_running_notice_or_stale_repair(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
