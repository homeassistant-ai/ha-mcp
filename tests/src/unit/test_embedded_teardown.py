"""Server teardown: unload, reload and the access switch all stop the server
through it (#2801).

Runs on a real ``hass`` with a real server config entry, the real issue
registry and Core's persistent notifications. Only the in-process server's
start, stop and credential release and the webhook / LLM API registration
(this component's own collaborators) are replaced.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from custom_components.ha_mcp_tools import embedded_setup as esetup
from custom_components.ha_mcp_tools.const import DATA_MANAGER, DOMAIN

from ._server_entry import add_server_entry


class _Server:
    """The replaced server and endpoint registration a teardown test reads."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = esetup.EmbeddedServerManager
        self.stop = AsyncMock()
        self.revoke = AsyncMock()
        self.unregister_webhook = AsyncMock()
        self.unregister_llm_api = MagicMock()
        monkeypatch.setattr(manager, "async_start", AsyncMock())
        monkeypatch.setattr(manager, "async_stop", self.stop)
        monkeypatch.setattr(manager, "async_revoke_credentials", self.revoke)
        monkeypatch.setattr(
            esetup, "async_register_webhook", AsyncMock(return_value=False)
        )
        monkeypatch.setattr(esetup, "async_register_llm_api", AsyncMock())
        monkeypatch.setattr(esetup, "async_unregister_webhook", self.unregister_webhook)
        monkeypatch.setattr(esetup, "async_unregister_llm_api", self.unregister_llm_api)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _Server:
    return _Server(monkeypatch)


@pytest.fixture
def notices(hass: HomeAssistant) -> dict[str, set[str]]:
    """The ids of the notifications Core added and removed during the test."""
    seen: dict[str, set[str]] = {"added": set(), "removed": set()}

    @callback
    def _record(
        update_type: persistent_notification.UpdateType,
        notifications: dict[str, persistent_notification.Notification],
    ) -> None:
        if update_type is persistent_notification.UpdateType.ADDED:
            seen["added"].update(notifications)
        elif update_type is persistent_notification.UpdateType.REMOVED:
            seen["removed"].update(notifications)

    persistent_notification.async_register_callback(hass, _record)
    return seen


async def _bring_up(hass: HomeAssistant) -> None:
    """A running server, as the entry's setup leaves it; it posts its notice."""
    await esetup.async_bring_up_server(hass, add_server_entry(hass))
    await hass.async_block_till_done()
    assert DATA_MANAGER in hass.data[DOMAIN]


class TestTeardown:
    async def test_unregisters_and_stops_without_revoking(
        self, hass: HomeAssistant, server: _Server
    ) -> None:
        await _bring_up(hass)
        server.stop.reset_mock()

        await esetup.async_teardown_server(hass)

        server.unregister_webhook.assert_awaited()
        server.unregister_llm_api.assert_called()
        server.stop.assert_awaited_once()
        assert DATA_MANAGER not in hass.data.get(DOMAIN, {})
        # A reload must keep the provisioned token.
        server.revoke.assert_not_awaited()

    async def test_closes_the_webhook_and_llm_api_before_stopping_the_server(
        self, hass: HomeAssistant, server: _Server
    ) -> None:
        # Both forward calls to the server over loopback. Stopped first, they
        # would stay routed to a server that is going away, and a call in that
        # window fails (a 502 on the webhook, an error to the conversation
        # agent) instead of finding no endpoint.
        await _bring_up(hass)
        calls: list[str] = []
        server.unregister_llm_api.side_effect = lambda *_: calls.append("llm")
        server.unregister_webhook.side_effect = lambda *_: calls.append("webhook")
        server.stop.side_effect = lambda *_: calls.append("stop")

        await esetup.async_teardown_server(hass)

        assert set(calls[:2]) == {"llm", "webhook"}
        assert calls[2:] == ["stop"]

    async def test_teardown_is_noop_when_not_running(
        self, hass: HomeAssistant, server: _Server
    ) -> None:
        await esetup.async_teardown_server(hass)  # must not raise
        server.unregister_webhook.assert_awaited_once()

    async def test_port_closes_even_when_the_webhook_fails_to_unregister(
        self, hass: HomeAssistant, server: _Server, notices: dict[str, set[str]]
    ) -> None:
        # The direct port is a way in of its own; turning access off must close
        # it whatever happens to the webhook.
        await _bring_up(hass)
        (running_notice,) = notices["added"]
        server.stop.reset_mock()
        server.unregister_webhook.side_effect = RuntimeError("close failed")

        with pytest.raises(RuntimeError):
            await esetup.async_teardown_server(hass)
        await hass.async_block_till_done()

        server.stop.assert_awaited_once()
        assert notices["removed"] == {running_notice}

    async def test_webhook_closes_even_when_the_llm_api_fails_to_unregister(
        self, hass: HomeAssistant, server: _Server
    ) -> None:
        # Otherwise the webhook and its /readonly alias stay routed to a server
        # that is gone.
        await _bring_up(hass)
        server.stop.reset_mock()
        server.unregister_llm_api.side_effect = RuntimeError("unregister failed")

        with pytest.raises(RuntimeError, match="unregister failed"):
            await esetup.async_teardown_server(hass)

        server.unregister_webhook.assert_awaited_once()
        server.stop.assert_awaited_once()

    async def test_stopped_server_leaves_no_running_notice_or_stale_repair(
        self,
        hass: HomeAssistant,
        server: _Server,
        notices: dict[str, set[str]],
        issue_registry: ir.IssueRegistry,
    ) -> None:
        await _bring_up(hass)
        (running_notice,) = notices["added"]
        stale = (
            esetup.ISSUE_START_FAILED,
            esetup.ISSUE_TOKEN_NEEDED,
            esetup.ISSUE_PACKAGE_FAILED,
        )
        for issue_id in stale:
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=issue_id,
            )

        await esetup.async_teardown_server(hass)
        await hass.async_block_till_done()

        assert notices["removed"] == {running_notice}
        assert [i for i in stale if issue_registry.async_get_issue(DOMAIN, i)] == []
