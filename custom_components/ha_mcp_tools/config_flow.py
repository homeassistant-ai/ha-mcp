"""Config flow for HA MCP Tools integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.hassio import is_hassio

from .addon import AddonBootstrapError, async_install_and_start_addon
from .const import (
    BIND_HOST_ALL,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_BIND_HOST,
    DEFAULT_LOOPBACK_URL,
    DEFAULT_PIP_SPEC,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    OPT_BIND_HOST,
    OPT_EMBEDDED_ENABLED,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_NONE,
)

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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HaMcpToolsOptionsFlow:
        """Return the options flow (in-process MCP server configuration)."""
        return HaMcpToolsOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # On Supervisor installs (HA OS / Supervised), offer to install the
        # Home Assistant MCP Server add-on too. On Container / Core installs
        # there is no add-on, so offer to run the MCP server inside Home
        # Assistant instead (the server otherwise runs via Docker or pip there).
        if is_hassio(self.hass):
            return await self.async_step_addon()

        if user_input is not None:
            return self._create_entry(
                options={
                    OPT_EMBEDDED_ENABLED: bool(
                        user_input.get(OPT_EMBEDDED_ENABLED, False)
                    )
                }
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(OPT_EMBEDDED_ENABLED, default=False): bool}
            ),
        )

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

    def _create_entry(self, options: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create the integration config entry."""
        return self.async_create_entry(
            title=_ENTRY_TITLE, data={}, options=options or {}
        )

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


class HaMcpToolsOptionsFlow(OptionsFlow):
    """Options flow: configure the in-process MCP server (issue #1527)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show / apply the embedded-server options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_EMBEDDED_ENABLED,
                    default=opts.get(OPT_EMBEDDED_ENABLED, False),
                ): bool,
                vol.Required(
                    OPT_SERVER_PORT,
                    default=opts.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Required(
                    OPT_BIND_HOST,
                    default=opts.get(OPT_BIND_HOST, DEFAULT_BIND_HOST),
                ): vol.In([DEFAULT_BIND_HOST, BIND_HOST_ALL]),
                vol.Required(
                    OPT_WEBHOOK_AUTH,
                    default=opts.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE),
                ): vol.In([WEBHOOK_AUTH_NONE, WEBHOOK_AUTH_HA]),
                vol.Optional(
                    OPT_PIP_SPEC,
                    default=opts.get(OPT_PIP_SPEC, DEFAULT_PIP_SPEC),
                ): str,
                vol.Optional(
                    OPT_SERVER_URL,
                    default=opts.get(OPT_SERVER_URL, DEFAULT_LOOPBACK_URL),
                ): str,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={"connect_url": self._connect_url_hint()},
        )

    def _connect_url_hint(self) -> str:
        """Return a human-readable connect-URL hint for the options form."""
        webhook_id = self.config_entry.data.get(DATA_WEBHOOK_ID)
        secret_path = self.config_entry.data.get(DATA_SECRET_PATH)
        if not webhook_id:
            return (
                "Enable the server and save; the remote connect URL will appear "
                "as a notification once it starts."
            )
        hint = f"Remote connect URL: <your-home-assistant-url>/api/webhook/{webhook_id}"
        if secret_path:
            hint += (
                f"\nLocal/LAN (when bind host is 0.0.0.0): "
                f"http://<home-assistant-ip>:{DEFAULT_SERVER_PORT}{secret_path}"
            )
        return hint
