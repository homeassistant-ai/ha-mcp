"""Repairs flow for MCP Webhook Proxy.

Surfaces a HACS-style "click submit to restart" card in HA's Repairs UI
when the addon has detected that OAuth is enabled but the integration
code currently loaded in HA doesn't enforce it. The card is the more
discoverable counterpart to the persistent_notification the addon also
posts; submitting the fix flow restarts HA so the new OAuth-aware code
takes over.

Lifecycle:
- Addon writes RESTART_MARKER_FILE when its fail-closed gate triggers.
- Integration's `async_setup` (in __init__.py) checks the marker on HA
  boot; if present, it calls `async_create_issue` with this domain's
  `oauth_restart_required` ID and `is_fixable=True` so the user sees
  a Repair card.
- User clicks Submit on the repair card → this module's fix flow
  deletes the marker, then calls the `homeassistant.restart` service.
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
RESTART_MARKER_FILE = Path("/config/.mcp_proxy_oauth_restart_required")


class OAuthRestartRepairFlow(RepairsFlow):
    """Single-step confirmation flow that restarts Home Assistant."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is not None:
            await self.hass.async_add_executor_job(_clear_marker)
            await self.hass.services.async_call(
                "homeassistant", "restart", {}, blocking=False
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
    issue. Single issue ID for now — `oauth_restart_required`."""
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


def _delete_issue_only(hass: HomeAssistant, domain: str) -> None:
    """Dismiss the Repair issue without touching the marker file.

    Used by `async_setup_entry` after it has already cleared the marker
    via the executor — calling `clear_issue` here would do the executor
    work twice. Kept separate from `clear_issue` so external callers
    (start.py, fix flow) get the convenience of a single function that
    does both.
    """
    ir.async_delete_issue(hass, domain, ISSUE_ID)


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
