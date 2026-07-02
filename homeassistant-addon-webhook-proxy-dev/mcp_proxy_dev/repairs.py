"""Repairs flow for MCP Webhook Proxy.

Surfaces a HACS-style "click submit to restart" card in HA's Repairs UI
when OAuth is enabled but the root OAuth views aren't live in the running
HA session yet — either because the add-on's fail-closed gate detected that
the integration code currently loaded doesn't enforce OAuth, or because the
integration itself enabled OAuth mid-session (HA binds the root /authorize +
/token views cleanly only at startup). The card is the more discoverable
counterpart to the persistent_notification the addon also posts; submitting
the fix flow restarts HA so the OAuth-aware code / freshly bound root views
take over.

Lifecycle:
- The marker file (RESTART_MARKER_FILE) has two writers: the add-on's
  fail-closed gate (a separate process that writes the path directly), and
  the integration's `async_setup_entry` (in __init__.py) via `_write_marker`
  when it enables OAuth mid-session — that same path also calls `create_issue`
  to raise the Repair immediately.
- Integration's `async_setup` (in __init__.py) also checks the marker on HA
  boot; if present, it calls `async_create_issue` with this domain's
  `oauth_restart_required` ID and `is_fixable=True` so the user sees
  a Repair card.
- User clicks Submit on the repair card → this module's fix flow calls the
  `homeassistant.restart` service (blocking) WITHOUT clearing the marker, so
  the Repair survives an aborted restart.
- After HA restart, the addon's keep-alive re-creates the config entry,
  the new code's setup probes OAuth, deletes the marker (if still
  present), and the issue self-clears.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

_LOGGER = logging.getLogger(__name__)

ISSUE_ID = "oauth_restart_required"
# Filed when the add-on refreshed the integration files on disk but HA is
# still running the previous version's code. Deliberately NOT marker-backed
# and NOT persistent: a successful HA restart (the fix) loads the new code
# and drops the issue automatically; an aborted restart leaves it in place
# for the session that still needs it.
UPDATE_ISSUE_ID = "update_restart_required"
RESTART_MARKER_FILE = Path("/config/.mcp_proxy_dev_oauth_restart_required")


class OAuthRestartRepairFlow(RepairsFlow):
    """Single-step confirmation flow that restarts Home Assistant.

    Shared by both restart issues (`oauth_restart_required` and
    `update_restart_required`) — the fix is identical, only the issue
    strings differ.
    """

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is not None:
            # Restart HA so the root OAuth views bind cleanly. blocking=True so a
            # failed config check surfaces as a flow error instead of being
            # swallowed — fire-and-forget would report success even when the
            # restart never happened. Do NOT clear the marker here: if the
            # restart aborts it must survive so the Repair persists; a successful
            # restart's boot-time setup clears it once OAuth is actually live.
            await self.hass.services.async_call(
                "homeassistant", "restart", {}, blocking=True
            )
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Factory hook called by the repairs platform to build a flow for our
    issues. Both issue IDs share the click-to-restart flow."""
    return OAuthRestartRepairFlow()


def _clear_marker() -> None:
    """Delete the marker file if present.

    `missing_ok=True` covers the common idempotent path (already cleared
    by the addon side, or never written). Any other OSError (permission
    denied, read-only filesystem, etc.) is logged at WARNING level so an
    operator can see why the Repair card keeps re-firing on boot — silent
    swallow would hide a real disk-state issue. The function is still
    "best effort" in the sense that the caller continues regardless.
    """
    try:
        RESTART_MARKER_FILE.unlink(missing_ok=True)
    except OSError as e:
        _LOGGER.warning(
            "MCP Proxy: could not delete OAuth restart marker at %s "
            "(%s: %s) — Repair card may re-appear on next HA boot until "
            "the file is removed manually.",
            RESTART_MARKER_FILE,
            type(e).__name__,
            e,
        )


def _write_marker() -> None:
    """Write the OAuth restart marker (idempotent). Blocking I/O — call via
    hass.async_add_executor_job. Any OSError is logged at WARNING (the Repair
    just won't persist to the next boot; the in-memory issue is still created)."""
    try:
        RESTART_MARKER_FILE.write_text('{"reason": "oauth_enabled_mid_session"}')
    except OSError as e:
        _LOGGER.warning(
            "MCP Proxy: could not write OAuth restart marker at %s (%s: %s).",
            RESTART_MARKER_FILE,
            type(e).__name__,
            e,
        )


def create_issue(hass: HomeAssistant, domain: str, issue_id: str = ISSUE_ID) -> None:
    """Create a restart Repair issue directly (does not check the marker).
    Same shape as maybe_create_issue's registration."""
    ir.async_create_issue(
        hass,
        domain,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=issue_id,
    )


def _delete_issue_only(
    hass: HomeAssistant, domain: str, issue_id: str = ISSUE_ID
) -> None:
    """Dismiss the Repair issue without touching the marker file.

    Used by `async_setup_entry` after it has already cleared the marker
    via the executor — calling `clear_issue` here would do the executor
    work twice. `clear_issue` is the combined convenience wrapper (marker
    + issue) kept for tests and any future in-process caller; neither
    start.py (a separate process that manipulates the marker file directly
    without importing repairs) nor the fix flow calls it.
    """
    ir.async_delete_issue(hass, domain, issue_id)


def marker_present() -> bool:
    """Sync helper for use under `hass.async_add_executor_job`."""
    return RESTART_MARKER_FILE.exists()


def maybe_create_issue(hass: HomeAssistant, domain: str) -> None:
    """Register the repair issue iff the marker file is present.

    Called from `async_setup` on every HA boot. Delegates the file check
    to the executor since it's blocking I/O.
    """
    if not marker_present():
        return
    ir.async_create_issue(
        hass,
        domain,
        ISSUE_ID,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ID,
    )


def clear_issue(hass: HomeAssistant, domain: str) -> None:
    """Dismiss the repair issue and delete the marker file.

    Synchronous filesystem I/O — callers on the event loop should prefer
    `_delete_issue_only` plus `hass.async_add_executor_job(_clear_marker)`.
    """
    _clear_marker()
    ir.async_delete_issue(hass, domain, ISSUE_ID)
