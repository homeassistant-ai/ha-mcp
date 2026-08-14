"""Repair flows for the HA-MCP Custom Component."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant


class LegacyOAuthRestartRepairFlow(RepairsFlow):
    """Confirm and apply a legacy OAuth change by restarting Home Assistant."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Open the restart confirmation step."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Restart Home Assistant after the user confirms the repair."""
        if user_input is not None:
            # Wait for HA to accept the restart request so a failed config check
            # remains visible instead of reporting a successful repair. The issue
            # is intentionally cleared only after bring-up proves no restart is
            # pending, so an aborted restart leaves the repair actionable.
            await self.hass.services.async_call(
                "homeassistant",
                "restart",
                {},
                blocking=True,
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
    """Create the click-to-restart flow for the legacy OAuth repair."""
    return LegacyOAuthRestartRepairFlow()
