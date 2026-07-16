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

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.loader import async_get_integration
from packaging.version import InvalidVersion, Version

from .const import (
    BIND_HOST_ALL,
    BIND_HOST_LOOPBACK,
    CHANNEL_DEV,
    CHANNEL_STABLE,
    CONF_ENTRY_TYPE,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_AUTO_UPDATE,
    DEFAULT_BIND_HOST,
    DEFAULT_CHANNEL,
    DEFAULT_ENABLE_LLM_API,
    DEFAULT_LLM_API_EXPOSURE,
    DEFAULT_LOOPBACK_URL,
    DEFAULT_PIP_SPEC,
    DEFAULT_SERVER_PORT,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
    EXPOSURE_BOTH,
    EXPOSURE_FULL,
    EXPOSURE_TOOL_SEARCH,
    LLM_API_DOCS_URL,
    MIN_EMBEDDED_HOME_ASSISTANT_VERSION,
    OPT_AUTO_UPDATE,
    OPT_BIND_HOST,
    OPT_CHANNEL,
    OPT_ENABLE_LLM_API,
    OPT_ENABLE_SIDEBAR_PANEL,
    OPT_ENABLE_STARTUP_NOTIFICATION,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_LLM_API_EXPOSURE,
    OPT_OAUTH_CLIENT_ID,
    OPT_OAUTH_CLIENT_SECRET,
    OPT_OAUTH_REGENERATE,
    OPT_PIP_SPEC,
    OPT_REGENERATE_SECRETS,
    OPT_SECRET_PATH_OVERRIDE,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    OPT_WEBHOOK_AUTH,
    OPT_WEBHOOK_ID_OVERRIDE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)

# Titles shown for each entry in the integration tile's entry list.
_TOOLS_ENTRY_TITLE = "HA MCP Tools"
_SERVER_ENTRY_TITLE = "HA-MCP Server"

# The single-instance server entry's unique id — distinct from the tools entry's
# unique id (``DOMAIN``) so both entry types coexist under the one domain.
_SERVER_UNIQUE_ID = f"{DOMAIN}-server"


_LOGGER = logging.getLogger(__name__)


def _legacy_credentials_active(
    hass: HomeAssistant, client_id: str, client_secret: str, signing_key: str
) -> bool:
    """Deferred-import seam for :func:`oauth_legacy.legacy_credentials_active`.

    oauth_legacy pulls in the aiohttp/HTTP view layer at import time; the
    config-flow module must stay importable without it (Home Assistant imports
    config flows early, and the flow unit tests stub only the config-entries
    surface) — same pattern as ``oauth_legacy._live_auth_mode``.
    """
    from .oauth_legacy import legacy_credentials_active

    return legacy_credentials_active(hass, client_id, client_secret, signing_key)


def _legacy_restart_pending(hass: HomeAssistant) -> bool:
    """Deferred-import seam for :func:`oauth_legacy.legacy_restart_pending`
    (see :func:`_legacy_credentials_active` for why the import is deferred)."""
    from .oauth_legacy import legacy_restart_pending

    return legacy_restart_pending(hass)


def _installed_server_version() -> str | None:
    """Return the installed ha-mcp server version, or None if not installed.

    Checks both channel distributions (only one is ever installed at a time).
    Kept dependency-free (``importlib.metadata``) and swallow-nothing-surprising
    so a read can never break the options form.
    """
    import importlib.metadata

    for dist in (DIST_NAME_STABLE, DIST_NAME_DEV):
        try:
            return importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


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
        try:
            supported = Version(HA_VERSION) >= Version(
                MIN_EMBEDDED_HOME_ASSISTANT_VERSION
            )
        except InvalidVersion:
            supported = False
        if not supported:
            return self.async_abort(
                reason="unsupported_home_assistant",
                description_placeholders={
                    "installed": HA_VERSION,
                    "required": MIN_EMBEDDED_HOME_ASSISTANT_VERSION,
                },
            )

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
                # Authentication mode first, directly under the connect URLs
                # shown in the step description (#1875): it is the setting users
                # most need to find, and legacy mode is what unblocks OAuth-only
                # clients such as Google Gemini Spark and Copilot CLI.
                vol.Required(
                    OPT_WEBHOOK_AUTH,
                    default=opts.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            WEBHOOK_AUTH_NONE,
                            WEBHOOK_AUTH_HA,
                            WEBHOOK_AUTH_LEGACY,
                        ],
                        translation_key="server_webhook_auth",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
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
                    OPT_AUTO_UPDATE,
                    default=bool(opts.get(OPT_AUTO_UPDATE, DEFAULT_AUTO_UPDATE)),
                ): bool,
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
                vol.Optional(
                    OPT_PIP_SPEC,
                    # Pre-fill via suggested_value, NOT a schema default: a
                    # default equal to the saved value makes the field
                    # impossible to clear. HA's frontend drops an emptied
                    # optional field from the submitted payload, so voluptuous
                    # re-applies the default (the old override) and clearing
                    # never sticks. suggested_value pre-fills the same value but
                    # is not re-injected on an empty submit. (Applies to every
                    # optional text field below.) Only a genuinely saved
                    # override is suggested; the normalized "no override" state
                    # renders an EMPTY field — the help text says "Leave empty",
                    # and pre-filling DEFAULT_PIP_SPEC would show the STABLE dist
                    # name even on the dev channel.
                    description={"suggested_value": opts.get(OPT_PIP_SPEC, "")},
                ): str,
                vol.Optional(
                    OPT_SERVER_URL,
                    description={
                        "suggested_value": opts.get(
                            OPT_SERVER_URL, DEFAULT_LOOPBACK_URL
                        )
                    },
                ): str,
                vol.Required(
                    OPT_ENABLE_WEBHOOK,
                    default=bool(opts.get(OPT_ENABLE_WEBHOOK, True)),
                ): bool,
                vol.Required(
                    OPT_ENABLE_STARTUP_NOTIFICATION,
                    default=bool(opts.get(OPT_ENABLE_STARTUP_NOTIFICATION, True)),
                ): bool,
                vol.Required(
                    OPT_ENABLE_SIDEBAR_PANEL,
                    default=bool(opts.get(OPT_ENABLE_SIDEBAR_PANEL, True)),
                ): bool,
                vol.Required(
                    OPT_ENABLE_LLM_API,
                    default=bool(opts.get(OPT_ENABLE_LLM_API, DEFAULT_ENABLE_LLM_API)),
                ): bool,
                vol.Required(
                    OPT_LLM_API_EXPOSURE,
                    default=str(
                        opts.get(OPT_LLM_API_EXPOSURE, DEFAULT_LLM_API_EXPOSURE)
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[EXPOSURE_TOOL_SEARCH, EXPOSURE_FULL, EXPOSURE_BOTH],
                        translation_key="llm_api_exposure",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                # suggested_value (not default) so these clear properly on an
                # empty submit — see the OPT_PIP_SPEC note above.
                vol.Optional(
                    OPT_EXTERNAL_URL,
                    description={"suggested_value": opts.get(OPT_EXTERNAL_URL, "")},
                ): str,
                vol.Optional(
                    OPT_WEBHOOK_ID_OVERRIDE,
                    description={
                        "suggested_value": opts.get(OPT_WEBHOOK_ID_OVERRIDE, "")
                    },
                ): str,
                vol.Optional(
                    OPT_SECRET_PATH_OVERRIDE,
                    description={
                        "suggested_value": opts.get(OPT_SECRET_PATH_OVERRIDE, "")
                    },
                ): str,
                vol.Optional(
                    OPT_REGENERATE_SECRETS,
                    default=False,
                ): bool,
                # Legacy OAuth mode (Google Gemini Spark) credential overrides —
                # same shape as the webhook id/secret-path overrides above.
                # Empty = auto-generate/keep the current value.
                vol.Optional(
                    OPT_OAUTH_CLIENT_ID,
                    description={"suggested_value": opts.get(OPT_OAUTH_CLIENT_ID, "")},
                ): str,
                vol.Optional(
                    OPT_OAUTH_CLIENT_SECRET,
                    description={
                        "suggested_value": opts.get(OPT_OAUTH_CLIENT_SECRET, "")
                    },
                ): str,
                vol.Optional(
                    OPT_OAUTH_REGENERATE,
                    default=False,
                ): bool,
            }
        )
        # The sidebar-panel sentence in the description is only truthful while
        # the panel is registered; drop it (from the CURRENT stored options, not
        # the unsaved form state) when the panel is off so the link cannot point
        # at a route that 404s. The trailing space keeps the surrounding prose
        # spaced correctly whether the sentence is present or empty.
        panel_hint = (
            "Open the [HA-MCP settings panel](/ha-mcp) for tool management and "
            "server settings. "
            if bool(opts.get(OPT_ENABLE_SIDEBAR_PANEL, True))
            else ""
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "versions": await self._versions_hint(),
                "connect_url": self._connect_url_hint(),
                "oauth_creds": self._oauth_creds_hint(),
                "llm_api_docs_url": LLM_API_DOCS_URL,
                "panel_hint": panel_hint,
            },
        )

    @staticmethod
    def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
        """Normalize the submitted options before they are persisted.

        Collapses the pip-spec field to empty when it is empty or equals
        ``DEFAULT_PIP_SPEC`` (the unpinned ``ha-mcp`` distribution): the field is
        pre-filled with the saved override or blank, but a user may also type the
        default dist name, and persisting it verbatim would read as an
        intentional override and disable the stable channel's automatic updates.
        Empty means "no override" (track the selected channel); any other string
        is a genuine override, stored as-is. Also strips the URL / secret
        override fields, and drops a blank ``server_url`` so its default applies.
        """
        cleaned = dict(user_input)
        if cleaned.get(OPT_PIP_SPEC, "").strip() in ("", DEFAULT_PIP_SPEC):
            cleaned[OPT_PIP_SPEC] = ""
        for key in (
            OPT_EXTERNAL_URL,
            OPT_WEBHOOK_ID_OVERRIDE,
            OPT_SECRET_PATH_OVERRIDE,
            OPT_OAUTH_CLIENT_ID,
            OPT_OAUTH_CLIENT_SECRET,
        ):
            cleaned[key] = str(cleaned.get(key, "") or "").strip()
        cleaned[OPT_EXTERNAL_URL] = cleaned[OPT_EXTERNAL_URL].rstrip("/")
        # server_url gets no _normalize-forced empty like the fields above; strip
        # it and drop it entirely when blank so a whitespace-only value can't be
        # stored verbatim (it would bypass the consumer's empty -> loopback
        # fallback and break the HA connection).
        server_url = str(cleaned.get(OPT_SERVER_URL, "") or "").strip().rstrip("/")
        if server_url:
            cleaned[OPT_SERVER_URL] = server_url
        else:
            cleaned.pop(OPT_SERVER_URL, None)
        return cleaned

    async def _versions_hint(self) -> str:
        """Return a one-line component + server version summary for the form.

        Reads the component version from the integration manifest and the
        installed server version from the channel's distribution metadata.
        Failure-proof like the connect-URL hint: any read error degrades to a
        best-effort string ("unknown" / "not installed yet") rather than
        breaking the options form.
        """
        opts = self.config_entry.options
        channel = str(opts.get(OPT_CHANNEL) or DEFAULT_CHANNEL)

        component_version = "unknown"
        hass = getattr(self, "hass", None)
        if hass is not None:
            try:
                integration = await async_get_integration(hass, DOMAIN)
                component_version = str(integration.version)
            except Exception as err:
                _LOGGER.debug(
                    "Could not read component version for the options hint: %s", err
                )

        try:
            # importlib.metadata scans dist-info via os.listdir (blocking I/O),
            # so run it on the executor rather than the event loop.
            raw_version = (
                await hass.async_add_executor_job(_installed_server_version)
                if hass is not None
                else _installed_server_version()
            )
            server_version = raw_version or "not installed yet"
        except Exception as err:
            _LOGGER.debug("Could not read server version for the options hint: %s", err)
            server_version = "not installed yet"

        return (
            f"Component {component_version} - "
            f"Server ha-mcp {server_version} ({channel} channel)"
        )

    def _connect_url_hint(self) -> str:
        """Return the connect URLs for the options form.

        The Configure screen is admin-only, so it shows the real resolved
        URLs (the start-up notification deliberately does not - it is visible
        to every signed-in user). Falls back to a placeholder form when
        resolution is unavailable.
        """
        webhook_id = self.config_entry.data.get(DATA_WEBHOOK_ID)
        secret_path = self.config_entry.data.get(DATA_SECRET_PATH)
        if not webhook_id:
            return (
                "The connect URLs appear here (and in the Home Assistant log) "
                "once the server has started."
            )
        webhook_enabled = bool(self.config_entry.options.get(OPT_ENABLE_WEBHOOK, True))
        port = self.config_entry.options.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT)
        hass = getattr(self, "hass", None)
        if hass is not None:
            try:
                from .embedded_setup import build_connect_urls

                urls = build_connect_urls(
                    hass, self.config_entry, webhook_enabled=webhook_enabled
                )
                if urls:
                    return "Connect URL(s):\n" + "\n".join(f"- {u}" for u in urls)
            except Exception as err:
                # The hint is auxiliary display data: a resolution bug must not
                # take down the whole options form, but the degradation should
                # be visible by default - hence warning, not debug.
                _LOGGER.warning(
                    "Falling back to the placeholder connect-URL hint: %s", err
                )
        if not webhook_enabled:
            # Local-only mode: the webhook endpoint is never registered, so
            # a webhook URL here would 404. With loopback binding the builder
            # resolves no URLs at all - state that instead of inventing one.
            hint = "Remote access via webhook is disabled (local-only mode)."
            if secret_path:
                hint += (
                    f"\nDirect access from the Home Assistant machine: "
                    f"http://127.0.0.1:{port}{secret_path}"
                )
            return hint
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

    def _oauth_creds_hint(self) -> str:
        """Return the resolved legacy OAuth Client ID + Secret for the options
        form, or a note pointing at the mode selector when legacy mode isn't
        the CONFIGURED mode. Admin-only screen (like ``_connect_url_hint``),
        so showing the secret in cleartext here is acceptable — and this is
        the surface the startup log points at while a rotation is pending
        (``_surface_connect_urls`` withholds pending credentials from the
        log, where a still-valid old-identity token could read them; an HA
        admin here is trusted). A pending rotation gets a caveat so the admin
        doesn't paste values that only start working after the restart.
        """
        configured_mode = str(self.config_entry.options.get(OPT_WEBHOOK_AUTH) or "")
        if configured_mode != WEBHOOK_AUTH_LEGACY:
            return (
                "Set Authentication mode to legacy OAuth above and save to "
                "generate a Client ID and Client Secret."
            )
        client_id = self.config_entry.data.get(DATA_OAUTH_CLIENT_ID)
        client_secret = self.config_entry.data.get(DATA_OAUTH_CLIENT_SECRET)
        if not client_id or not client_secret:
            # Not minted yet — the entry hasn't finished a bring-up cycle
            # since legacy mode was selected (e.g. this save just turned it
            # on). They appear after the next reload.
            return (
                "The Client ID and Client Secret appear here once the server "
                "has started."
            )
        creds = f"Client ID: {client_id}\nClient Secret: {client_secret}"
        signing_key = str(self.config_entry.data.get(DATA_OAUTH_SIGNING_KEY) or "")
        active = _legacy_credentials_active(
            self.hass, str(client_id), str(client_secret), signing_key
        )
        if active and not _legacy_restart_pending(self.hass):
            return creds  # bound and live
        # Not serving these credentials yet: a mid-session first enable
        # (bound but not live until the restart), a pending rotation (the old
        # identity still bound), the webhook disabled, or another integration
        # owning the routes. State-agnostic wording — the previous "the
        # previous Client ID and Client Secret remain active" was false at a
        # first enable and when nothing of ours is bound. Matches the startup
        # log's first-enable caveat and the oauth_regenerate help text.
        return (
            f"{creds}\n"
            "Legacy OAuth is not serving these yet — restart Home Assistant "
            "when it asks you to, to activate them."
        )
