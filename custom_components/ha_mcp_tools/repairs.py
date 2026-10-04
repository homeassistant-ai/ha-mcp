"""Repair flows for the HA-MCP Custom Component."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import server_credentials
from .const import ISSUE_TOKEN_NEEDED

_ADMIN_TOKEN = "admin_token"


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
            # Wait for HA to validate the config and schedule the restart. If the
            # service rejects the request, the exception prevents flow completion
            # and the repair remains registered. The next startup independently
            # re-evaluates whether the OAuth change is still pending.
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


class ServerTokenRepairFlow(RepairsFlow):
    """Take a replacement administrator token for the in-process server (#2427)."""

    def __init__(self, entry_id: str) -> None:
        """Remember which server entry the token is for."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Open the token step."""
        return await self.async_step_token()

    async def async_step_token(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Store a working token and restart the server with it."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_removed")
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input.get(_ADMIN_TOKEN, "")).strip()
            problem = server_credentials.token_problem(self.hass, token)
            if problem is None:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data=server_credentials.adopt_admin_token(
                        self.hass, entry.data, token
                    ),
                )
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_create_entry(data={})
            errors[_ADMIN_TOKEN] = problem
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema(
                {
                    vol.Required(_ADMIN_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the fix flow for one of this integration's fixable repairs."""
    if issue_id == ISSUE_TOKEN_NEEDED:
        return ServerTokenRepairFlow(str((data or {}).get("entry_id", "")))
    return LegacyOAuthRestartRepairFlow()
