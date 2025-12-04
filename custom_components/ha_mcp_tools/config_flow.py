"""Config flow for HA MCP Tools integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class HaMcpToolsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HA MCP Tools."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        # Check if already configured
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="HA MCP Tools", data={})

        return self.async_show_form(step_id="user")
