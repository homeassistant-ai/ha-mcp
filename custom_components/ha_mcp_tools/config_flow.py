"""Config + options flow for the HA-MCP custom component.

One config flow serves two entry types under the shared domain, chosen from a
menu on the first step:

* ``tools`` — the privileged file / YAML services (the original component).
  A single confirm step creates the entry. Single-instance, keyed on
  ``DOMAIN``.
* ``server`` — the in-process ha-mcp FastMCP server (issue #1527). A single
  confirm step creates the entry (entry-exists = the server runs);
  single-instance, keyed on ``DOMAIN-server``. Its options flow tunes the
  port / bind host / webhook auth / pip spec / server URL.

The two entry types are discriminated by ``entry.data[CONF_ENTRY_TYPE]``; the
options-flow dispatcher branches on it — the server entry gets the configurable
options flow, and the tools entry gets a light informational options flow
(nothing to configure yet).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
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
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.loader import async_get_integration
from packaging.version import InvalidVersion, Version

from . import server_credentials
from .const import (
    BIND_HOST_ALL,
    BIND_HOST_LOOPBACK,
    CONF_ENTRY_TYPE,
    DATA_ADMIN_TOKEN,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_BIND_HOST,
    DEFAULT_ENABLE_LLM_API,
    DEFAULT_LLM_API_EXPOSURE,
    DEFAULT_LOOPBACK_URL,
    DEFAULT_OAUTH_REDIRECT_ALLOWLIST,
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
    OPT_ADMIN_TOKEN_REPLACEMENT,
    OPT_BIND_HOST,
    OPT_ENABLE_LLM_API,
    OPT_ENABLE_SIDEBAR_PANEL,
    OPT_ENABLE_STARTUP_NOTIFICATION,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_LLM_API_EXPOSURE,
    OPT_OAUTH_CLIENT_ID,
    OPT_OAUTH_CLIENT_SECRET,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
    OPT_OAUTH_REGENERATE,
    OPT_PIP_SPEC,
    OPT_REGENERATE_SECRETS,
    OPT_SECRET_PATH_OVERRIDE,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    OPT_WEBHOOK_AUTH,
    OPT_WEBHOOK_ID_OVERRIDE,
    TOOLS_ENTRY_TITLE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)
from .oauth_redirect_allowlist import effective_allowlist, normalize_allowlist

# Title shown for the server entry in the integration tile's entry list; the
# tools entry's title lives in const.py (setup migration in __init__ needs it).
_SERVER_ENTRY_TITLE = "HA-MCP Server"

# The single-instance server entry's unique id — distinct from the tools entry's
# unique id (``DOMAIN``) so both entry types coexist under the one domain.
_SERVER_UNIQUE_ID = f"{DOMAIN}-server"

# Setup-form field for the server entry: whether Home Assistant forwards
# remote MCP traffic through a webhook, and how clients authenticate there.
# One choice instead of the options form's checkbox + mode, so the step that
# decides exposure states it in one place.
SETUP_REMOTE_ACCESS = "remote_access"
REMOTE_ACCESS_DISABLED = "disabled"
SETUP_ADMIN_TOKEN = "admin_token"


_LOGGER = logging.getLogger(__name__)


# The options form's prose is assembled here rather than in strings.json,
# because which sentences appear depends on runtime state. Keeping the text
# itself in the ``common`` catalog means the assembled paragraphs follow the
# system-configured language instead of being English inside an otherwise
# translated form. These are the English source strings and the fallback: if
# the language is unreadable or the catalog cannot be loaded, the form still
# renders, in English, exactly as it did before.
#
# Kept identical to ``strings.json``'s ``common`` block, keys and values —
# asserted by ``test_common_fallbacks_mirror_strings_json`` in
# tests/src/unit/test_config_flow.py, because a fallback that has drifted
# from the source shows different English than every catalog.
_COMMON_FALLBACKS: dict[str, str] = {
    "panel_hint": (
        "Open the [HA-MCP settings panel](/ha-mcp) for tool management and "
        "server settings."
    ),
    "version_line": (
        "Component {component_version} - Server ha-mcp {server_version} ({source})"
    ),
    "server_source_paired": "installed with this component release",
    "server_source_override": "pip requirement override",
    "version_unknown": "unknown",
    "version_not_installed": "not installed yet",
    "tools_module_installed": (
        "Beta/advanced file & YAML tools module (optional): Installed"
    ),
    "tools_module_not_loaded": (
        "Beta/advanced file & YAML tools module (optional): Installed "
        'but not loaded — enable or reload the "HA-MCP File & YAML '
        "Tools\" entry on this integration's page"
    ),
    "tools_module_not_installed": (
        "Beta/advanced file & YAML tools module (optional): Not installed — "
        'press "Add entry" on this integration\'s page and choose '
        '"HA-MCP File & YAML Tools" to add it'
    ),
    "connect_urls_pending": (
        "The connect URLs appear here once the server has started."
    ),
    "connect_urls_label": "Connect URL(s):",
    "connect_webhook_disabled": (
        "Remote access via webhook is disabled (local-only mode)."
    ),
    "connect_direct_access": "Direct access from the Home Assistant machine: {url}",
    "connect_remote_url": "Remote connect URL: {url}",
    "connect_local_lan": 'Local/LAN (when Network access is "Local network"): {url}',
    "oauth_select_legacy_mode": (
        "Set Authentication mode to legacy OAuth above and save to "
        "generate a Client ID and Client Secret."
    ),
    "oauth_creds_pending": (
        "The Client ID and Client Secret appear here once the server has started."
    ),
    "oauth_not_serving": (
        "Legacy OAuth is not serving these yet — restart Home Assistant "
        "when it asks you to, to activate them."
    ),
}


def _fill(common: dict[str, str], key: str, /, **values: str) -> str:
    """Return the ``common`` string ``key`` with ``values`` substituted.

    Placeholder parity is asserted in tests/src/unit/test_locale_parity.py, but
    a catalog is data: one malformed brace, or a placeholder the parity check
    cannot see (``{component_version.major}`` reads as no placeholder at all),
    would otherwise take the whole options form down. The English source is a
    module constant, so formatting it after a failure needs no second guard.
    """
    try:
        return common[key].format(**values)
    except Exception as err:  # noqa: BLE001
        # Names both causes: a catalog string this caller cannot fill, or a
        # caller passing values the template never declared. The second is our
        # bug and crashes on the English constant below, so the log line has to
        # point at the caller rather than blame the translator.
        _LOGGER.warning(
            "Unusable %s template (%r) — bad catalog string or wrong caller "
            "arguments; using the English source: %s",
            key,
            common.get(key),
            err,
        )
    return _COMMON_FALLBACKS[key].format(**values)


# Scripts that set their own inter-sentence spacing: the full-width punctuation
# they end on already carries it, so an ASCII space after it renders as a gap.
#
# Keyed off the language, not off the last character. Sniffing glyphs got it
# wrong in both directions: U+201D (”) is Simplified Chinese's closing quote and
# was removed as "Latin", while 「」『』 are the traditional forms zh-Hans does
# not use and were kept.
#
# ``ko`` is deliberately absent. Korean separates words with ASCII spaces and
# ends sentences on an ASCII full stop, so a future ``ko`` catalog wants the
# separator exactly like a Latin one — the full-width rationale above simply
# does not apply to it.
_NO_ASCII_SENTENCE_SPACE = frozenset({"zh", "ja"})


def _sentence_prefix(sentence: str, language: str, english: str) -> str:
    """Return ``sentence`` spaced to run into the prose that follows it.

    Two inputs decide this, and each alone has already been wrong once. The
    language names the script, which the last character cannot. But the
    language does not promise the text follows it: core loads
    ``[en, <language>]`` and merges English first as the documented fallback
    (``helpers/translation.py``), so an instance set to a language this
    integration does not ship reads these sentences in English — and English
    needs the ASCII separator whatever ``hass.config.language`` says. The same
    holds for a shipped language whenever the catalog load degrades.

    ``english`` is the English source for this sentence; when the catalog
    hands back exactly that, the rendered text is English and gets the space.
    """
    if not sentence:
        return sentence
    if (
        language.split("-", maxsplit=1)[0].lower() in _NO_ASCII_SENTENCE_SPACE
        and sentence != english
    ):
        return sentence
    return f"{sentence} "


async def _fetch_common_translations(
    hass: HomeAssistant, language: str
) -> dict[str, str]:
    """core ``async_get_translations(hass, language, "common")``; test seam.

    Mirrors the seam in ``websocket_api.services`` so the lookup can be replaced in
    tests without reaching into Home Assistant's translation machinery.
    """
    from homeassistant.helpers.translation import async_get_translations

    result = await async_get_translations(hass, language, "common", {DOMAIN})
    # Any Mapping, not just dict: core returns a plain dict today, but the
    # mirrored seam in ``websocket_api.services`` accepts a Mapping, and narrowing it
    # here would silently discard a whole catalog on a core-internal change.
    if isinstance(result, Mapping):
        return dict(result)
    # Discarding a whole catalog is the same pure-English outcome as a failed
    # load, so it gets the same visibility; the type is the only useful clue.
    _LOGGER.warning(
        "Ignoring the %s common translations: expected a Mapping, got %s",
        language,
        type(result).__name__,
    )
    return {}


async def _common_strings(hass: HomeAssistant | None) -> tuple[dict[str, str], str]:
    """Return the ``common`` catalog and the language it was fetched for.

    ``hass.config.language`` is the instance-wide language, not the profile
    language of the administrator who opened the form — an options flow is
    handed no requester language (Home Assistant's flow context carries
    ``source`` and ``entry_id`` only), so where the two differ this prose
    follows the system setting while the surrounding form follows the user.

    The language is returned rather than left for the caller to read again:
    the sentence separator needs it, and two independent reads of the same
    attribute can disagree about which catalog is actually in hand. Here they
    cannot — this is the only place the attribute is read, and ``en`` is what
    both the fallback strings and the returned language say when it is
    unreadable.

    Failure-proof like the hints it feeds: an unreadable language or a
    failing lookup degrades to the English source strings rather than
    breaking the options form.
    """
    strings = dict(_COMMON_FALLBACKS)
    configured = getattr(getattr(hass, "config", None), "language", None)
    if hass is None or not isinstance(configured, str):
        return strings, "en"
    language = configured
    try:
        loaded = await _fetch_common_translations(hass, language)
    except Exception as err:
        # Warning, not debug: this is a degradation an administrator can see
        # in the form (English paragraphs inside a translated page) and the
        # broad ``except`` also covers an ImportError from the function-local
        # core import — a permanent defect nobody would ever notice at debug.
        # ``exc_info`` because the traceback is the only way to tell the two
        # apart. Same level the ``websocket_api.services`` seam this mirrors uses.
        _LOGGER.warning(
            "Could not load the %s options-form translations, falling back to "
            "English: %s",
            language,
            err,
            exc_info=True,
        )
        return strings, language

    prefix = f"component.{DOMAIN}.common."
    translated = {
        key.removeprefix(prefix): value
        for key, value in loaded.items()
        if key.startswith(prefix) and isinstance(value, str) and value
    }
    if not translated:
        # Deliberately not ``if loaded and not translated``: core returns a
        # single-component lookup straight from that component's cache entry
        # (``_TranslationCache.get_cached``), so a category that was never
        # built arrives as ``{}`` — which is exactly the developer error worth
        # seeing, and an empty-``loaded`` condition would skip it. The merge is
        # a silent no-op either way: the form renders pure English and no other
        # check notices.
        # Wording covers both ways to get here: a catalog that carries nothing
        # under our prefix, and one the seam already discarded and warned about
        # (where "carries no keys" would be untrue — there was no catalog).
        _LOGGER.warning(
            "No usable %s translations under %s, so the options form renders "
            "its assembled prose in English",
            language,
            prefix,
        )
    strings.update(translated)
    return strings, language


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

    Checks both server distributions (only one is ever installed at a time).
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


def _bind_host_selector() -> SelectSelector:
    """Network-access dropdown shared by the setup step and the options form.

    Inline labels: hassfest forbids dots in translation keys, so the
    IP-valued options cannot use strings.json selector translations.
    """
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(
                    value=BIND_HOST_LOOPBACK, label="This machine only (loopback)"
                ),
                SelectOptionDict(value=BIND_HOST_ALL, label="Local network"),
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


class HaMcpToolsConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle the config flow for the HA-MCP custom component (both entry types)."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for this entry type.

        The in-process server entry gets the configurable options flow (
        port / bind / auth / pip spec / URL). The tools services entry has
        nothing to configure yet, so it gets a light informational options flow
        instead of aborting.
        """
        if config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SERVER:
            return HaMcpServerOptionsFlow()
        return HaMcpToolsInfoOptionsFlow()

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
            title=TOOLS_ENTRY_TITLE,
            data={CONF_ENTRY_TYPE: ENTRY_TYPE_TOOLS},
        )

    # -- server entry: in-process MCP server (issue #1527) ------------------

    async def async_step_server(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose remote and network access, then create the server entry.

        A new install starts with the webhook off and the server port bound to
        loopback (#2427): nothing beyond the Home Assistant machine can reach
        the server until the administrator chooses a remote-access mode or LAN
        access here or later in the options. The choice is saved explicitly,
        so entries created before these defaults keep the behaviour they
        inherit. The server runs with the administrator token entered here.
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

        errors: dict[str, str] = {}
        values = user_input or {}
        if user_input is not None:
            token = str(user_input.get(SETUP_ADMIN_TOKEN, "")).strip()
            problem = server_credentials.token_problem(self.hass, token)
            if problem is not None:
                errors[SETUP_ADMIN_TOKEN] = problem
            else:
                remote = user_input.get(SETUP_REMOTE_ACCESS, REMOTE_ACCESS_DISABLED)
                enabled = remote != REMOTE_ACCESS_DISABLED
                return self.async_create_entry(
                    title=_SERVER_ENTRY_TITLE,
                    data={CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER, DATA_ADMIN_TOKEN: token},
                    options={
                        OPT_ENABLE_WEBHOOK: enabled,
                        # Disabled keeps Home Assistant sign-in queued, so
                        # enabling the webhook later never lands on the
                        # secret-URL mode.
                        OPT_WEBHOOK_AUTH: remote if enabled else WEBHOOK_AUTH_HA,
                        OPT_BIND_HOST: user_input.get(
                            OPT_BIND_HOST, BIND_HOST_LOOPBACK
                        ),
                    },
                )
        return self.async_show_form(
            step_id="server",
            errors=errors,
            data_schema=vol.Schema(
                {
                    # The server acts with this account's rights (#2427); the
                    # component no longer creates an administrator for itself.
                    vol.Required(SETUP_ADMIN_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Required(
                        SETUP_REMOTE_ACCESS,
                        default=values.get(SETUP_REMOTE_ACCESS, REMOTE_ACCESS_DISABLED),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                REMOTE_ACCESS_DISABLED,
                                WEBHOOK_AUTH_HA,
                                WEBHOOK_AUTH_LEGACY,
                                WEBHOOK_AUTH_NONE,
                            ],
                            translation_key="server_remote_access",
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        OPT_BIND_HOST,
                        default=values.get(OPT_BIND_HOST, BIND_HOST_LOOPBACK),
                    ): _bind_host_selector(),
                }
            ),
        )


class HaMcpToolsInfoOptionsFlow(OptionsFlow):
    """Options flow for the tools entry: edit the privileged services' config.

    Surfaces the two operator-tunable sets the file/YAML tools honour - the
    extra read/write directories and the extra top-level YAML write keys - so
    they are reachable from the integration UI, not only the ha-mcp server's own
    settings. Both live in the component's own .storage (get/set_allowed_paths
    and get/set_extra_yaml_keys), so this screen and the server settings UI edit
    the same source of truth, applied live with no restart. The deny floor is
    non-overridable: traversal / out-of-config directories and denylisted keys
    are dropped on save.

    The form uses the ``tools_info`` step id, NOT ``init``: the server options
    flow already owns ``options.step.init`` in strings.json, so a shared step id
    would collide. ``async_step_init`` renders the form; HA routes the form's
    submit to ``async_step_tools_info``.
    """

    def _tools_form_schema(self) -> vol.Schema:
        """Build the form schema with the current stored values as defaults."""
        from . import _current_extra_dirs, _current_extra_yaml_keys

        current_dirs = _current_extra_dirs(self.hass)
        current_keys = _current_extra_yaml_keys(self.hass)
        return vol.Schema(
            {
                vol.Optional("allowed_dirs", default=current_dirs): SelectSelector(
                    SelectSelectorConfig(
                        options=current_dirs,
                        multiple=True,
                        custom_value=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional("extra_yaml_keys", default=current_keys): SelectSelector(
                    SelectSelectorConfig(
                        options=current_keys,
                        multiple=True,
                        custom_value=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Render the editable form under the ``tools_info`` step id."""
        return self.async_show_form(
            step_id="tools_info", data_schema=self._tools_form_schema()
        )

    async def async_step_tools_info(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Persist the edited directories and keys once the form is submitted."""
        if user_input is None:
            return self.async_show_form(
                step_id="tools_info", data_schema=self._tools_form_schema()
            )
        from . import _apply_allowed_paths, _apply_extra_yaml_keys

        await _apply_allowed_paths(self.hass, user_input.get("allowed_dirs", []))
        await _apply_extra_yaml_keys(self.hass, user_input.get("extra_yaml_keys", []))
        return self.async_create_entry(title="", data={})


class HaMcpServerOptionsFlow(OptionsFlow):
    """Options flow: configure the in-process MCP server (issue #1527)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show / apply the server options."""
        opts = self.config_entry.options
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._connect_path_override_errors(user_input)
            entries, invalid = normalize_allowlist(
                user_input.get(OPT_OAUTH_REDIRECT_ALLOWLIST) or []
            )
            if invalid:
                errors[OPT_OAUTH_REDIRECT_ALLOWLIST] = "invalid_oauth_callback"
            token = str(user_input.get(OPT_ADMIN_TOKEN_REPLACEMENT) or "").strip()
            if token and (
                problem := server_credentials.token_problem(self.hass, token)
            ):
                errors[OPT_ADMIN_TOKEN_REPLACEMENT] = problem
            if not errors:
                data = self._normalize(user_input)
                data.pop(OPT_OAUTH_REDIRECT_ALLOWLIST, None)
                # An entry still following the shipped default keeps following
                # it, so a later release's additions reach it.
                if OPT_OAUTH_REDIRECT_ALLOWLIST in opts or entries != list(
                    DEFAULT_OAUTH_REDIRECT_ALLOWLIST
                ):
                    data[OPT_OAUTH_REDIRECT_ALLOWLIST] = entries
                return self.async_create_entry(title="", data=data)

        # A validation failure must re-render the values the user just entered.
        # Required fields fall back to their stored/default values only when a
        # direct unit-test call omits them; optional text fields treat omission
        # as an intentional clear, matching Home Assistant's frontend payload.
        form_values = opts if user_input is None else {**opts, **user_input}
        suggested_values = opts if user_input is None else user_input
        schema = vol.Schema(
            {
                # Authentication mode first, directly under the connect URLs
                # shown in the step description (#1875): it is the setting users
                # most need to find, and legacy mode is what unblocks OAuth-only
                # clients such as Google Gemini Spark and Copilot CLI.
                vol.Required(
                    OPT_WEBHOOK_AUTH,
                    default=form_values.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE),
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
                    OPT_SERVER_PORT,
                    default=form_values.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Required(
                    OPT_BIND_HOST,
                    default=form_values.get(OPT_BIND_HOST, DEFAULT_BIND_HOST),
                ): _bind_host_selector(),
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
                    description={
                        "suggested_value": suggested_values.get(OPT_PIP_SPEC, "")
                    },
                ): str,
                vol.Optional(
                    OPT_SERVER_URL,
                    # Only a genuinely saved override is suggested (same
                    # pattern as OPT_PIP_SPEC above). Pre-filling
                    # DEFAULT_LOOPBACK_URL made every options save store the
                    # constant as an explicit override, which would pin the
                    # scheme/port even after issue #1890's SSL/port-aware
                    # loopback derivation.
                    description={
                        "suggested_value": suggested_values.get(OPT_SERVER_URL, "")
                    },
                ): str,
                vol.Required(
                    OPT_ENABLE_WEBHOOK,
                    default=bool(form_values.get(OPT_ENABLE_WEBHOOK, True)),
                ): bool,
                vol.Required(
                    OPT_ENABLE_STARTUP_NOTIFICATION,
                    default=bool(
                        form_values.get(OPT_ENABLE_STARTUP_NOTIFICATION, True)
                    ),
                ): bool,
                vol.Required(
                    OPT_ENABLE_SIDEBAR_PANEL,
                    default=bool(form_values.get(OPT_ENABLE_SIDEBAR_PANEL, True)),
                ): bool,
                vol.Required(
                    OPT_ENABLE_LLM_API,
                    default=bool(
                        form_values.get(OPT_ENABLE_LLM_API, DEFAULT_ENABLE_LLM_API)
                    ),
                ): bool,
                vol.Required(
                    OPT_LLM_API_EXPOSURE,
                    default=str(
                        form_values.get(OPT_LLM_API_EXPOSURE, DEFAULT_LLM_API_EXPOSURE)
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
                    description={
                        "suggested_value": suggested_values.get(OPT_EXTERNAL_URL, "")
                    },
                ): str,
                vol.Optional(
                    OPT_WEBHOOK_ID_OVERRIDE,
                    description={
                        "suggested_value": suggested_values.get(
                            OPT_WEBHOOK_ID_OVERRIDE, ""
                        )
                    },
                ): str,
                vol.Optional(
                    OPT_SECRET_PATH_OVERRIDE,
                    description={
                        "suggested_value": suggested_values.get(
                            OPT_SECRET_PATH_OVERRIDE, ""
                        )
                    },
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(
                    OPT_REGENERATE_SECRETS,
                    default=False,
                ): bool,
                # Legacy OAuth mode (Google Gemini Spark) credential overrides —
                # same shape as the webhook id/secret-path overrides above.
                # Empty = auto-generate/keep the current value.
                vol.Optional(
                    OPT_OAUTH_CLIENT_ID,
                    description={
                        "suggested_value": suggested_values.get(OPT_OAUTH_CLIENT_ID, "")
                    },
                ): str,
                vol.Optional(
                    OPT_OAUTH_CLIENT_SECRET,
                    description={
                        "suggested_value": suggested_values.get(
                            OPT_OAUTH_CLIENT_SECRET, ""
                        )
                    },
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(
                    OPT_OAUTH_REGENERATE,
                    default=False,
                ): bool,
                # Never pre-filled: the stored token stays out of the form.
                vol.Optional(OPT_ADMIN_TOKEN_REPLACEMENT): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                # suggested_value, not default: an emptied list must save as
                # empty rather than fall back (see the OPT_PIP_SPEC note).
                vol.Optional(
                    OPT_OAUTH_REDIRECT_ALLOWLIST,
                    description={
                        "suggested_value": suggested_values.get(
                            OPT_OAUTH_REDIRECT_ALLOWLIST,
                            effective_allowlist(opts),
                        )
                    },
                ): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL, multiple=True)
                ),
            }
        )
        # The sidebar-panel sentence in the description is only truthful while
        # the panel is registered; drop it (from the CURRENT stored options, not
        # the unsaved form state) when the panel is off so the link cannot point
        # at a route that 404s. The separator keeps the surrounding prose spaced
        # correctly whether the sentence is present or empty.
        common, language = await _common_strings(getattr(self, "hass", None))
        panel_hint = (
            _sentence_prefix(
                common["panel_hint"], language, _COMMON_FALLBACKS["panel_hint"]
            )
            if bool(opts.get(OPT_ENABLE_SIDEBAR_PANEL, True))
            else ""
        )
        # The tools-module status renders as its own paragraph directly under
        # the version line, sharing the {versions} placeholder so every
        # translation shows it without a strings change.
        versions = await self._versions_hint(common)
        tools_hint = self._tools_module_hint(common)
        if tools_hint:
            versions = f"{versions}\n\n{tools_hint}"
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "versions": versions,
                "connect_url": await self._connect_url_hint(common),
                "oauth_creds": self._oauth_creds_hint(common),
                "llm_api_docs_url": LLM_API_DOCS_URL,
                "panel_hint": panel_hint,
            },
        )

    @staticmethod
    def _connect_path_override_errors(
        user_input: Mapping[str, Any],
    ) -> dict[str, str]:
        """Return field errors for values that cannot be embedded in a URL path."""
        errors: dict[str, str] = {}
        for key in (OPT_SECRET_PATH_OVERRIDE, OPT_WEBHOOK_ID_OVERRIDE):
            # Match _normalize's long-standing paste-friendly behavior: outer
            # whitespace is discarded before saving, while whitespace inside
            # the value remains invalid because it breaks the resulting URL.
            value = str(user_input.get(key, "") or "").strip()
            if any(character in "#?%" or character.isspace() for character in value):
                errors[key] = "invalid_connect_path"
            elif key == OPT_WEBHOOK_ID_OVERRIDE and "/" in value:
                errors[key] = "invalid_webhook_id"
        return errors

    @staticmethod
    def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
        """Normalize the submitted options before they are persisted.

        Collapses the pip-spec field to empty when it is empty or equals
        ``DEFAULT_PIP_SPEC`` (the bare ``ha-mcp`` distribution): a user may type
        the default dist name, and persisting it verbatim would read as an
        intentional override of the server this component release pins.
        Empty means "no override" (run the paired server); any other string is
        a genuine override, stored as-is. Also strips the URL / secret
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
            OPT_ADMIN_TOKEN_REPLACEMENT,
        ):
            cleaned[key] = str(cleaned.get(key, "") or "").strip()
        cleaned[OPT_EXTERNAL_URL] = cleaned[OPT_EXTERNAL_URL].rstrip("/")
        # server_url gets no _normalize-forced empty like the fields above; strip
        # it and drop it entirely when blank so a whitespace-only value can't be
        # stored verbatim (it would bypass the consumer's empty -> loopback
        # fallback and break the HA connection). A value equal to
        # DEFAULT_LOOPBACK_URL is likewise dropped: older forms pre-filled it,
        # so it reaches here from users who never chose an override, and
        # storing it would pin the scheme/port the #1890 loopback derivation
        # exists to resolve.
        server_url = str(cleaned.get(OPT_SERVER_URL, "") or "").strip().rstrip("/")
        if server_url and server_url != DEFAULT_LOOPBACK_URL:
            cleaned[OPT_SERVER_URL] = server_url
        else:
            cleaned.pop(OPT_SERVER_URL, None)
        return cleaned

    async def _versions_hint(self, common: dict[str, str]) -> str:
        """Return a one-line component + server version summary for the form.

        Reads the component version from the integration manifest and the
        installed server version from the distribution metadata; the source
        says whether that server is the one this release pins or a pip-spec
        override.
        Failure-proof like the connect-URL hint: any read error degrades to a
        best-effort string ("unknown" / "not installed yet") rather than
        breaking the options form.
        """
        opts = self.config_entry.options
        override = str(opts.get(OPT_PIP_SPEC) or "").strip()
        source = (
            common["server_source_override"]
            if override and override != DEFAULT_PIP_SPEC
            else common["server_source_paired"]
        )

        component_version = common["version_unknown"]
        hass = getattr(self, "hass", None)
        if hass is not None:
            try:
                integration = await async_get_integration(hass, DOMAIN)
                # A manifest without a version yields None, not an exception —
                # ``str()`` would render the literal "None" into the form and
                # the "unknown" wording would never appear for the likeliest
                # defect it exists for.
                if integration.version is not None:
                    component_version = str(integration.version)
                else:
                    _LOGGER.warning(
                        "The %s manifest carries no version; the options form "
                        "shows the unknown-version wording",
                        DOMAIN,
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Could not read the component version for the options hint, "
                    "showing the unknown-version wording: %s",
                    err,
                )

        try:
            # importlib.metadata scans dist-info via os.listdir (blocking I/O),
            # so run it on the executor rather than the event loop.
            raw_version = (
                await hass.async_add_executor_job(_installed_server_version)
                if hass is not None
                else _installed_server_version()
            )
            server_version = raw_version or common["version_not_installed"]
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not read the server version for the options hint, showing "
                "the not-installed wording: %s",
                err,
            )
            server_version = common["version_not_installed"]

        return _fill(
            common,
            "version_line",
            component_version=component_version,
            server_version=server_version,
            source=source,
        )

    def _tools_module_hint(self, common: dict[str, str]) -> str | None:
        """Return the File & YAML tools entry status line, or None if unreadable.

        Shown directly under the version line (#1996): users routinely add the
        server entry only and never learn the file / YAML tools need the second
        "HA-MCP File & YAML Tools" entry until a tool call fails. An entry
        that exists but is not loaded (disabled, or setup failed) serves no
        services either, so it reports "not loaded" rather than Installed.
        Failure-proof like the other hints: any read error drops the line
        rather than breaking the options form.
        """
        hass = getattr(self, "hass", None)
        if hass is None:
            return None
        try:
            # A missing entry_type means tools (pre-#1527 entries never carried
            # the discriminator) — same default async_setup_entry dispatches on.
            tools_entries = [
                entry
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_TOOLS) == ENTRY_TYPE_TOOLS
            ]
            loaded = any(
                entry.state is ConfigEntryState.LOADED for entry in tools_entries
            )
        except Exception as err:  # noqa: BLE001
            # Warning, like the two version reads above: this drops the whole
            # tools-module paragraph from the form, which is a larger visible
            # loss than either of those fallbacks.
            _LOGGER.warning(
                "Could not read the tools-entry state, dropping the "
                "tools-module line from the options form: %s",
                err,
            )
            return None
        if loaded:
            return common["tools_module_installed"]
        if tools_entries:
            return common["tools_module_not_loaded"]
        return common["tools_module_not_installed"]

    async def _connect_url_hint(self, common: dict[str, str]) -> str:
        """Return the connect URLs for the options form.

        The Configure screen is admin-only, so it shows the real resolved
        URLs (the start-up notification deliberately does not - it is visible
        to every signed-in user). Falls back to a placeholder form when
        resolution is unavailable.

        The URLs themselves and the two ``<...>`` stand-ins stay verbatim: they
        are addresses to copy, not prose. Everything around them comes from the
        ``common`` catalog.
        """
        webhook_id = self.config_entry.data.get(DATA_WEBHOOK_ID)
        secret_path = self.config_entry.data.get(DATA_SECRET_PATH)
        if not webhook_id:
            return common["connect_urls_pending"]
        webhook_enabled = bool(self.config_entry.options.get(OPT_ENABLE_WEBHOOK, True))
        port = self.config_entry.options.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT)
        hass = getattr(self, "hass", None)
        if hass is not None:
            try:
                from .embedded_setup import async_get_lan_hosts, build_connect_urls

                urls = build_connect_urls(
                    hass,
                    self.config_entry,
                    webhook_enabled=webhook_enabled,
                    extra_hosts=await async_get_lan_hosts(hass),
                )
                if urls:
                    listed = "\n".join(f"- {u}" for u in urls)
                    return f"{common['connect_urls_label']}\n{listed}"
            except Exception as err:  # noqa: BLE001
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
            hint = common["connect_webhook_disabled"]
            if secret_path:
                direct = _fill(
                    common,
                    "connect_direct_access",
                    url=f"http://127.0.0.1:{port}{secret_path}",
                )
                hint += f"\n{direct}"
            return hint
        external = str(self.config_entry.options.get(OPT_EXTERNAL_URL) or "").rstrip(
            "/"
        )
        base = external or "<your-home-assistant-url>"
        hint = _fill(
            common, "connect_remote_url", url=f"{base}/api/webhook/{webhook_id}"
        )
        if secret_path:
            lan = _fill(
                common,
                "connect_local_lan",
                url=f"http://<home-assistant-ip>:{port}{secret_path}",
            )
            hint += f"\n{lan}"
        return hint

    def _oauth_creds_hint(self, common: dict[str, str]) -> str:
        """Return the resolved legacy OAuth Client ID + Secret for the options
        form, or a note pointing at the mode selector when legacy mode isn't
        the CONFIGURED mode. Admin-only screen (like ``_connect_url_hint``),
        so showing the secret in cleartext here is acceptable — and this is
        the surface the startup log points at while a rotation is pending
        (``_surface_connect_urls`` withholds pending credentials from the
        log, where a still-valid old-identity token could read them; an HA
        admin here is trusted). A pending rotation gets a caveat so the admin
        doesn't paste values that only start working after the restart.

        The ``Client ID`` / ``Client Secret`` labels stay verbatim: they name
        the two fields the client an admin pastes them into asks for, and those
        clients label them in English whatever this instance's language is.
        """
        configured_mode = str(self.config_entry.options.get(OPT_WEBHOOK_AUTH) or "")
        if configured_mode != WEBHOOK_AUTH_LEGACY:
            return common["oauth_select_legacy_mode"]
        client_id = self.config_entry.data.get(DATA_OAUTH_CLIENT_ID)
        client_secret = self.config_entry.data.get(DATA_OAUTH_CLIENT_SECRET)
        if not client_id or not client_secret:
            # Not minted yet — the entry hasn't finished a bring-up cycle
            # since legacy mode was selected (e.g. this save just turned it
            # on). They appear after the next reload.
            return common["oauth_creds_pending"]
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
        return f"{creds}\n{common['oauth_not_serving']}"
