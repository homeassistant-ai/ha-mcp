"""The repair issues the server entry's bring-up files on failure (#2427).

Runs on a real ``hass`` with a real server config entry and the real issue
registry; only the in-process server's start and stop and the webhook / LLM API
registration (this component's own collaborators) are replaced.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.ha_mcp_tools import embedded_setup as esetup
from custom_components.ha_mcp_tools.const import (
    DATA_MANAGER,
    DOMAIN,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
)

from ._server_entry import add_server_entry


class _Collaborators:
    """The replaced collaborators a bring-up test asserts on."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.register_webhook = AsyncMock(return_value=False)
        self.register_llm_api = AsyncMock()
        monkeypatch.setattr(esetup.EmbeddedServerManager, "async_start", self.start)
        monkeypatch.setattr(esetup.EmbeddedServerManager, "async_stop", self.stop)
        monkeypatch.setattr(esetup, "async_register_webhook", self.register_webhook)
        monkeypatch.setattr(esetup, "async_register_llm_api", self.register_llm_api)


@pytest.fixture
def collaborators(monkeypatch: pytest.MonkeyPatch) -> _Collaborators:
    return _Collaborators(monkeypatch)


class TestBringUpRepairs:
    """Each failure kind files the repair issue whose fix flow addresses it."""

    async def test_package_failure_files_package_issue_and_skips_webhook(
        self, hass: HomeAssistant, collaborators: _Collaborators
    ) -> None:
        entry = add_server_entry(hass)
        collaborators.start.side_effect = esetup.EmbeddedServerError(
            "pip failed", kind="package"
        )

        await esetup.async_bring_up_server(hass, entry)

        collaborators.stop.assert_awaited_once()  # teardown ran
        assert DATA_MANAGER not in hass.data.get(DOMAIN, {})
        collaborators.register_webhook.assert_not_awaited()
        collaborators.register_llm_api.assert_not_awaited()
        # The failure kind selects the package-install repair issue, whose
        # fix flow reinstalls the server for this entry.
        issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PACKAGE_FAILED)
        assert issue is not None
        assert issue.is_fixable is True
        assert issue.data == {"entry_id": entry.entry_id, "detail": "pip failed"}

    async def test_start_failure_files_start_issue(
        self, hass: HomeAssistant, collaborators: _Collaborators
    ) -> None:
        entry = add_server_entry(hass)
        collaborators.start.side_effect = esetup.EmbeddedServerError(
            "bind failed", kind="start"
        )

        await esetup.async_bring_up_server(hass, entry)

        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_START_FAILED)

    async def test_a_missing_credential_asks_for_a_token_in_repairs(
        self, hass: HomeAssistant, collaborators: _Collaborators
    ) -> None:
        entry = add_server_entry(hass)
        collaborators.start.side_effect = esetup.EmbeddedServerError(
            "invalid_token", kind="token"
        )

        await esetup.async_bring_up_server(hass, entry)

        issue = ir.async_get(hass).async_get_issue(DOMAIN, esetup.ISSUE_TOKEN_NEEDED)
        assert issue is not None
        # The repair's form replaces the token on this entry, and says why the
        # stored one stopped working.
        assert issue.is_fixable is True
        assert issue.data == {"entry_id": entry.entry_id, "reason": "invalid_token"}

    async def test_unexpected_error_files_start_issue(
        self, hass: HomeAssistant, collaborators: _Collaborators
    ) -> None:
        entry = add_server_entry(hass)
        # Server started, but webhook registration raised a non-EmbeddedServerError.
        collaborators.register_webhook.side_effect = RuntimeError("register boom")

        await esetup.async_bring_up_server(hass, entry)

        collaborators.stop.assert_awaited_once()
        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_START_FAILED)
