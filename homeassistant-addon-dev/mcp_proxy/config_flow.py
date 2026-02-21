"""Config flow for MCP Webhook Proxy."""

from homeassistant.config_entries import ConfigFlow

DOMAIN = "mcp_proxy"


class McpProxyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MCP Webhook Proxy."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle setup via the UI."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="MCP Webhook Proxy", data={})

        return self.async_show_form(step_id="user")

    async def async_step_import(self, import_data=None):
        """Handle auto-import from YAML migration or addon API call."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="MCP Webhook Proxy", data={})
