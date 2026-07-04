"""Config + options flow for the Home Assistant MCP Server integration (#1527)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    BIND_HOST_ALL,
    CHANNEL_DEV,
    CHANNEL_STABLE,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_BIND_HOST,
    DEFAULT_CHANNEL,
    DEFAULT_LOOPBACK_URL,
    DEFAULT_PIP_SPEC,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    OPT_BIND_HOST,
    OPT_CHANNEL,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_NONE,
)

_ENTRY_TITLE = "Home Assistant MCP Server"


class HaMcpServerConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle the config flow — a single confirm step creates the entry."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HaMcpServerOptionsFlow:
        """Return the options flow (server port / auth / pip spec / URL)."""
        return HaMcpServerOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and create the single config entry.

        Creating the entry starts the in-process server with the defaults (port
        9584, loopback-only, secret-URL auth); everything is tunable afterward in
        the integration options.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title=_ENTRY_TITLE, data={}, options={})
        return self.async_show_form(step_id="user")


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
