"""Config + options flow for the HA-MCP custom component.

One config flow serves two entry types under the shared domain, chosen from a
menu on the first step:

* ``tools`` — the privileged file / YAML services (the original component),
  including the Supervisor add-on bootstrap offer. Single-instance, keyed on
  ``DOMAIN``.
* ``server`` — the in-process ha-mcp FastMCP server (issue #1527). A single
  confirm step creates the entry (entry-exists = the server runs);
  single-instance, keyed on ``DOMAIN-server``. Its options flow tunes the
  channel / port / bind host / webhook auth / pip spec / server URL.

The two entry types are discriminated by ``entry.data[CONF_ENTRY_TYPE]``; the
options-flow dispatcher branches on it so only the server entry gets a
configurable options flow (the tools entry aborts with ``no_options``).
"""

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
    CHANNEL_DEV,
    CHANNEL_STABLE,
    CONF_ENTRY_TYPE,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_BIND_HOST,
    DEFAULT_CHANNEL,
    DEFAULT_LOOPBACK_URL,
    DEFAULT_PIP_SPEC,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
    OPT_BIND_HOST,
    OPT_CHANNEL,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_NONE,
)

_LOGGER = logging.getLogger(__name__)

# Titles shown for each entry in the integration tile's entry list.
_TOOLS_ENTRY_TITLE = "HA MCP Tools"
_SERVER_ENTRY_TITLE = "In-process MCP server"
_CONF_INSTALL_ADDON = "install_addon"

# The single-instance server entry's unique id — distinct from the tools entry's
# unique id (``DOMAIN``) so both entry types coexist under the one domain.
_SERVER_UNIQUE_ID = f"{DOMAIN}-server"


class HaMcpToolsConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle the config flow for the HA-MCP custom component (both entry types)."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow (add-on bootstrap task state)."""
        self._install_task: asyncio.Task[None] | None = None
        self._install_error: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for this entry type.

        Only the in-process server entry has options (channel / port / bind /
        auth / pip spec / URL). The tools services entry has none, so it returns
        a flow that aborts with an explanatory message.
        """
        if config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SERVER:
            return HaMcpServerOptionsFlow()
        return _NoOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which entry type to add: the services tools or the server."""
        return self.async_show_menu(
            step_id="user",
            menu_options=[ENTRY_TYPE_TOOLS, ENTRY_TYPE_SERVER],
        )

    # -- tools entry: privileged file / YAML services -----------------------

    async def async_step_tools(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up the services (tools) entry — single-instance, keyed on DOMAIN.

        On Supervisor installs (HA OS / Supervised), offer to install the Home
        Assistant MCP Server add-on too. On Container / Core installs there is no
        add-on, so fall back to the plain confirm-and-create behaviour (the
        server runs via Docker or pip there).
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if is_hassio(self.hass):
            return await self.async_step_addon()

        if user_input is not None:
            return self._create_tools_entry()
        return self.async_show_form(step_id="tools")

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
            return self._create_tools_entry()
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
        return self._create_tools_entry()

    async def async_step_install_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add-on bootstrap failed; still set up the integration's services."""
        if user_input is not None:
            return self._create_tools_entry()
        return self.async_show_form(
            step_id="install_failed",
            data_schema=vol.Schema({}),
            description_placeholders={"error": self._install_error or "unknown error"},
        )

    def _create_tools_entry(self) -> ConfigFlowResult:
        """Create the services (tools) config entry."""
        return self.async_create_entry(
            title=_TOOLS_ENTRY_TITLE,
            data={CONF_ENTRY_TYPE: ENTRY_TYPE_TOOLS},
        )

    # -- server entry: in-process MCP server (issue #1527) ------------------

    async def async_step_server(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and create the single in-process server entry.

        Creating the entry starts the in-process server with the defaults (port
        9584, loopback-only, secret-URL auth); everything is tunable afterward in
        the integration options.
        """
        await self.async_set_unique_id(_SERVER_UNIQUE_ID)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title=_SERVER_ENTRY_TITLE,
                data={CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER},
                options={},
            )
        return self.async_show_form(step_id="server")

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


class _NoOptionsFlow(OptionsFlow):
    """Options flow for the tools entry: it has no configurable options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Abort immediately — the services entry exposes no options."""
        return self.async_abort(reason="no_options")


class HaMcpServerOptionsFlow(OptionsFlow):
    """Options flow: configure the in-process MCP server (issue #1527)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show / apply the server options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=self._normalize(user_input))

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_CHANNEL,
                    default=opts.get(OPT_CHANNEL, DEFAULT_CHANNEL),
                ): vol.In([CHANNEL_STABLE, CHANNEL_DEV]),
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
                    # ``or DEFAULT_PIP_SPEC`` so a stored-empty spec (the normalized
                    # "no override" state) re-displays the pinned default as a hint.
                    default=opts.get(OPT_PIP_SPEC) or DEFAULT_PIP_SPEC,
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

    @staticmethod
    def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
        """Store the pinned default pip spec as empty so it is not an override.

        The pip-spec field is pre-filled with ``DEFAULT_PIP_SPEC``, whose version
        moves with each release. Persisting that value verbatim would later read
        as an intentional pin once the default changes, freezing a stable-channel
        entry on the old version. Collapsing "equals the default" to empty keeps
        the entry tracking the selected channel across upgrades; a genuine
        override (any other string) is stored as-is.
        """
        cleaned = dict(user_input)
        if cleaned.get(OPT_PIP_SPEC, "").strip() in ("", DEFAULT_PIP_SPEC):
            cleaned[OPT_PIP_SPEC] = ""
        return cleaned

    def _connect_url_hint(self) -> str:
        """Return a human-readable connect-URL hint for the options form."""
        webhook_id = self.config_entry.data.get(DATA_WEBHOOK_ID)
        secret_path = self.config_entry.data.get(DATA_SECRET_PATH)
        if not webhook_id:
            return (
                "The remote connect URL will appear as a notification once the "
                "server starts."
            )
        port = self.config_entry.options.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT)
        hint = f"Remote connect URL: <your-home-assistant-url>/api/webhook/{webhook_id}"
        if secret_path:
            hint += (
                f"\nLocal/LAN (when bind host is 0.0.0.0): "
                f"http://<home-assistant-ip>:{port}{secret_path}"
            )
        return hint
