"""Config flow for HA MCP Tools integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.hassio import is_hassio

from .addon import AddonBootstrapError, async_install_and_start_addon
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_ENTRY_TITLE = "Home Assistant MCP Server Custom Component"
_CONF_INSTALL_ADDON = "install_addon"


class HaMcpToolsConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for HA MCP Tools."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._install_task: asyncio.Task[None] | None = None
        self._install_error: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # On Supervisor installs (HA OS / Supervised), offer to install the
        # Home Assistant MCP Server add-on too. On Container / Core installs
        # there is no add-on, so fall back to the plain confirm-and-create
        # behaviour (the server runs via Docker or pip there).
        if is_hassio(self.hass):
            return await self.async_step_addon()

        if user_input is not None:
            return self._create_entry()
        return self.async_show_form(step_id="user")

    async def async_step_addon(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to install the Home Assistant MCP Server add-on."""
        if user_input is None:
            return self.async_show_form(
                step_id="addon",
                data_schema=vol.Schema(
                    {vol.Required(_CONF_INSTALL_ADDON, default=True): bool}
                ),
            )
        if not user_input[_CONF_INSTALL_ADDON]:
            return self._create_entry()
        return await self.async_step_install_addon()

    async def async_step_install_addon(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Install and start the add-on, showing a progress spinner."""
        if self._install_task is None:
            self._install_task = self.hass.async_create_task(
                async_install_and_start_addon(self.hass)
            )
        install_task = self._install_task

        if not install_task.done():
            return self.async_show_progress(
                step_id="install_addon",
                progress_action="install_addon",
                progress_task=install_task,
            )

        try:
            await install_task
        except AddonBootstrapError as err:
            _LOGGER.error("ha-mcp add-on bootstrap failed: %s", err)
            self._install_error = str(err)
            return self.async_show_progress_done(next_step_id="install_failed")
        finally:
            self._install_task = None

        return self.async_show_progress_done(next_step_id="addon_success")

    async def async_step_addon_success(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish setup after the add-on was installed and started."""
        return self._create_entry()

    async def async_step_install_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add-on bootstrap failed; still set up the integration's services."""
        if user_input is not None:
            return self._create_entry()
        return self.async_show_form(
            step_id="install_failed",
            data_schema=vol.Schema({}),
            description_placeholders={"error": self._install_error or "unknown error"},
        )

    def _create_entry(self) -> ConfigFlowResult:
        """Create the integration config entry."""
        return self.async_create_entry(title=_ENTRY_TITLE, data={})

    @callback
    def async_remove(self) -> None:
        """Cancel an in-flight add-on install if the flow is abandoned."""
        if self._install_task is not None and not self._install_task.done():
            _LOGGER.info(
                "Config flow abandoned during add-on install; cancelling. The "
                "add-on repository may already be added and the add-on may be "
                "partially installed — check the Add-on Store."
            )
            self._install_task.cancel()
