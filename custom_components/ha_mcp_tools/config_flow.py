"""Config + options flow for the HA-MCP custom component.

One config flow serves two entry types under the shared domain, chosen from a
menu on the first step:

* ``tools`` — the privileged file / YAML services (the original component).
  A single confirm step creates the entry. Single-instance, keyed on
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

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    BIND_HOST_ALL,
    BIND_HOST_LOOPBACK,
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
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_PIP_SPEC,
    OPT_REGENERATE_SECRETS,
    OPT_SECRET_PATH_OVERRIDE,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    OPT_WEBHOOK_AUTH,
    OPT_WEBHOOK_ID_OVERRIDE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_NONE,
)

# Titles shown for each entry in the integration tile's entry list.
_TOOLS_ENTRY_TITLE = "HA MCP Tools"
_SERVER_ENTRY_TITLE = "HA-MCP Server"

# The single-instance server entry's unique id — distinct from the tools entry's
# unique id (``DOMAIN``) so both entry types coexist under the one domain.
_SERVER_UNIQUE_ID = f"{DOMAIN}-server"


class HaMcpToolsConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle the config flow for the HA-MCP custom component (both entry types)."""

    VERSION = 1

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
            menu_options=[ENTRY_TYPE_SERVER, ENTRY_TYPE_TOOLS],
        )

    # -- tools entry: privileged file / YAML services -----------------------

    async def async_step_tools(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up the services (tools) entry — single-instance, keyed on DOMAIN.

        Plain confirm-and-create on every install type. (The add-on bootstrap
        this step used to offer on Supervisor installs was removed: the
        in-process server entry is the one-click way to get a server, and a
        second install path only caused confusion. The add-on remains fully
        supported - installed from the add-on store as always.)
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self._create_tools_entry()
        return self.async_show_form(step_id="tools")

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
        9584, LAN-reachable like the add-on, secret-URL auth); everything is
        tunable afterward in the integration options.
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
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[CHANNEL_STABLE, CHANNEL_DEV],
                        translation_key="server_channel",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    OPT_SERVER_PORT,
                    default=opts.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Required(
                    OPT_BIND_HOST,
                    default=opts.get(OPT_BIND_HOST, DEFAULT_BIND_HOST),
                ): SelectSelector(
                    # Inline labels: hassfest forbids dots in translation
                    # keys, so the IP-valued options cannot use strings.json
                    # selector translations.
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=BIND_HOST_ALL,
                                label="Local network (default)",
                            ),
                            SelectOptionDict(
                                value=BIND_HOST_LOOPBACK,
                                label="This machine only (loopback)",
                            ),
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    OPT_WEBHOOK_AUTH,
                    default=opts.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[WEBHOOK_AUTH_NONE, WEBHOOK_AUTH_HA],
                        translation_key="server_webhook_auth",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
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
                vol.Required(
                    OPT_ENABLE_WEBHOOK,
                    default=bool(opts.get(OPT_ENABLE_WEBHOOK, True)),
                ): bool,
                vol.Optional(
                    OPT_EXTERNAL_URL,
                    default=opts.get(OPT_EXTERNAL_URL, ""),
                ): str,
                vol.Optional(
                    OPT_WEBHOOK_ID_OVERRIDE,
                    default=opts.get(OPT_WEBHOOK_ID_OVERRIDE, ""),
                ): str,
                vol.Optional(
                    OPT_SECRET_PATH_OVERRIDE,
                    default=opts.get(OPT_SECRET_PATH_OVERRIDE, ""),
                ): str,
                vol.Optional(
                    OPT_REGENERATE_SECRETS,
                    default=False,
                ): bool,
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
        for key in (
            OPT_EXTERNAL_URL,
            OPT_WEBHOOK_ID_OVERRIDE,
            OPT_SECRET_PATH_OVERRIDE,
        ):
            cleaned[key] = str(cleaned.get(key, "") or "").strip()
        cleaned[OPT_EXTERNAL_URL] = cleaned[OPT_EXTERNAL_URL].rstrip("/")
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
        external = str(self.config_entry.options.get(OPT_EXTERNAL_URL) or "").rstrip(
            "/"
        )
        base = external or "<your-home-assistant-url>"
        hint = f"Remote connect URL: {base}/api/webhook/{webhook_id}"
        if secret_path:
            hint += (
                f"\nLocal/LAN (when bind host is 0.0.0.0): "
                f"http://<home-assistant-ip>:{port}{secret_path}"
            )
        return hint
