"""Text of the HA-MCP server options form, in the user's language.

Which sentences the form shows depends on runtime state, so the prose is
assembled in code from the ``common`` translation block rather than kept in
``strings.json``'s step descriptions.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

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
