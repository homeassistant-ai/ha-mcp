"""The repair issues the server entry's bring-up files on failure (#2427).

Split from test_embedded_setup.py, whose fakes and fixtures it reuses.
"""

from __future__ import annotations

from custom_components.ha_mcp_tools.const import (
    DATA_MANAGER,
    DOMAIN,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
)

from .test_embedded_setup import _make_entry, _make_hass, esetup

# pytest registers a fixture under its own name, so the aliases keep ruff from
# reading the test parameters as redefinitions.
from .test_embedded_setup import _spy as _spy_fixture  # noqa: F401
from .test_embedded_setup import fake_manager as _fake_manager_fixture  # noqa: F401


class TestBringUpRepairs:
    """Each failure kind files the repair issue whose fix flow addresses it."""

    async def test_package_failure_files_package_issue_and_skips_webhook(
        self, fake_manager
    ):
        hass = _make_hass()
        entry = _make_entry()
        fake_manager.async_start.side_effect = esetup.EmbeddedServerError(
            "pip failed", kind="package"
        )

        await esetup.async_bring_up_server(hass, entry)

        fake_manager.async_stop.assert_awaited_once()  # teardown ran
        assert DATA_MANAGER not in hass.data.get(DOMAIN, {})
        esetup.async_register_webhook.assert_not_awaited()
        esetup.async_register_llm_api.assert_not_awaited()
        # The failure kind selects the package-install repair issue, whose
        # fix flow reinstalls the server for this entry.
        call = esetup.ir.async_create_issue.call_args
        assert call.args[2] == ISSUE_PACKAGE_FAILED
        assert call.kwargs["is_fixable"] is True
        assert call.kwargs["data"] == {
            "entry_id": entry.entry_id,
            "detail": "pip failed",
        }

    async def test_start_failure_files_start_issue(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()
        fake_manager.async_start.side_effect = esetup.EmbeddedServerError(
            "bind failed", kind="start"
        )

        await esetup.async_bring_up_server(hass, entry)
        assert esetup.ir.async_create_issue.call_args.args[2] == ISSUE_START_FAILED

    async def test_a_missing_credential_asks_for_a_token_in_repairs(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()
        fake_manager.async_start.side_effect = esetup.EmbeddedServerError(
            "invalid_token", kind="token"
        )

        await esetup.async_bring_up_server(hass, entry)

        call = esetup.ir.async_create_issue.call_args
        assert call.args[2] == esetup.ISSUE_TOKEN_NEEDED
        # The repair's form replaces the token on this entry, and says why the
        # stored one stopped working.
        assert call.kwargs["is_fixable"] is True
        assert call.kwargs["data"] == {
            "entry_id": entry.entry_id,
            "reason": "invalid_token",
        }

    async def test_unexpected_error_files_start_issue(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()
        # Server started, but webhook registration raised a non-EmbeddedServerError.
        esetup.async_register_webhook.side_effect = RuntimeError("register boom")

        await esetup.async_bring_up_server(hass, entry)

        fake_manager.async_stop.assert_awaited_once()
        assert esetup.ir.async_create_issue.call_args.args[2] == ISSUE_START_FAILED
