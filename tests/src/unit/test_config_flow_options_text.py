"""The options form's runtime-assembled prose follows the configured language.

The sentences depend on runtime state, so the form builds them from the
integration's ``common`` translations. Core answers the lookup from the
integration's real catalogs; a test replaces Core's lookup only for an answer
Core cannot be made to give (a failure, a malformed catalog).
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from collections.abc import Callable
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import translation
from homeassistant.loader import IntegrationNotFound

from custom_components.ha_mcp_tools import config_flow, embedded_setup
from custom_components.ha_mcp_tools.const import (
    BIND_HOST_LOOPBACK,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DOMAIN,
    OPT_BIND_HOST,
    OPT_ENABLE_WEBHOOK,
    OPT_PIP_SPEC,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_LEGACY,
)

from ._config_flow_core import (
    GERMAN_COMMON,
    MANIFEST_VERSION,
    STRINGS_COMMON,
    WEBHOOK_ID,
    add_tools_entry,
    open_options_form,
    placeholders,
)

PREFIX = f"component.{DOMAIN}.common."
ENGLISH_PANEL_HINT = STRINGS_COMMON["panel_hint"]
ENGLISH_VERSIONS = f"Component {MANIFEST_VERSION} - Server ha-mcp "
WITH_SECRET = {DATA_WEBHOOK_ID: WEBHOOK_ID, DATA_SECRET_PATH: "/private_x"}


def _catalog(**keys: str) -> dict[str, str]:
    """A ``common`` catalog the way Core's translation lookup keys it."""
    return {f"{PREFIX}{key}": value for key, value in keys.items()}


def _answer_lookup(monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    """Replace Core's translation lookup: ``answer`` is the reply, or the error to raise."""
    lookup = (
        AsyncMock(side_effect=answer)
        if isinstance(answer, Exception)
        else AsyncMock(return_value=answer)
    )
    monkeypatch.setattr(translation, "async_get_translations", lookup)


async def _core_catalog(hass: HomeAssistant) -> dict[str, str]:
    """Core's own answer for the configured language."""
    return dict(
        await translation.async_get_translations(
            hass, hass.config.language, "common", {DOMAIN}
        )
    )


async def _form_text(hass: HomeAssistant, language: str) -> dict[str, str]:
    hass.config.language = language
    return placeholders(await open_options_form(hass))


async def test_form_prose_uses_the_catalog_for_the_configured_language(
    hass: HomeAssistant,
) -> None:
    """A German instance sees the German sentences, not English inside a German form."""
    shown = await _form_text(hass, "de")

    # The trailing space that spaces the sentence from the prose after it is
    # added by the form, not carried in the catalog.
    assert shown["panel_hint"] == f"{GERMAN_COMMON['panel_hint']} "
    assert shown["versions"].startswith(
        f"Komponente {MANIFEST_VERSION} - Server ha-mcp "
    )
    assert GERMAN_COMMON["tools_module_not_installed"] in shown["versions"]


async def test_a_language_the_integration_does_not_ship_shows_english(
    hass: HomeAssistant,
) -> None:
    """Core serves English for an unshipped language, and English keeps its space.

    A ``ja`` instance renders these sentences in English while the configured
    language still reads ``ja``; keying the separator off the language alone
    jammed the two English sentences together.
    """
    shown = await _form_text(hass, "ja")

    assert shown["panel_hint"] == f"{ENGLISH_PANEL_HINT} "
    assert shown["versions"].startswith(ENGLISH_VERSIONS)


async def test_keys_a_catalog_lacks_keep_their_english_source(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial catalog must not blank the sentences it does not carry."""
    _answer_lookup(monkeypatch, _catalog(panel_hint="Öffne das Panel."))

    shown = await _form_text(hass, "de")

    assert shown["panel_hint"] == "Öffne das Panel. "
    assert shown["versions"].startswith(ENGLISH_VERSIONS)


async def test_a_failing_lookup_degrades_to_english_and_warns(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Translations are additive; a broken lookup must not break the form.

    The level is asserted too: at debug the degradation would be invisible
    while every text assertion stayed green.
    """
    _answer_lookup(monkeypatch, HomeAssistantError("boom"))

    with caplog.at_level(logging.WARNING):
        shown = await _form_text(hass, "de")

    assert shown["panel_hint"] == f"{ENGLISH_PANEL_HINT} "
    assert shown["versions"].startswith(ENGLISH_VERSIONS)
    assert "falling back to" in caplog.text


@pytest.mark.parametrize(
    "template",
    ["Komponente {nope} {", "Komponente {component_version.major}"],
    ids=["malformed-brace", "dotted-placeholder"],
)
async def test_an_unusable_version_template_falls_back_to_english(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    template: str,
) -> None:
    """A catalog is data: one unusable brace must not take the form down.

    The dotted placeholder is the case the placeholder-parity check cannot
    see: it reads as no placeholder, and formatting it raises ``AttributeError``.
    """
    _answer_lookup(monkeypatch, _catalog(version_line=template))

    with caplog.at_level(logging.WARNING):
        shown = await _form_text(hass, "de")

    assert shown["versions"].startswith(ENGLISH_VERSIONS)
    assert "Unusable version_line template" in caplog.text


async def test_an_empty_translated_value_keeps_the_english_source(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty value in a catalog would otherwise blank the sentence."""
    _answer_lookup(monkeypatch, _catalog(panel_hint=""))

    shown = await _form_text(hass, "de")

    assert shown["panel_hint"] == f"{ENGLISH_PANEL_HINT} "


@pytest.mark.parametrize(
    "answer",
    [{}, {"component.other.common.panel_hint": "Öffne das Panel."}],
    ids=["empty", "other-integration"],
)
async def test_a_catalog_without_our_keys_says_so(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    answer: dict[str, str],
) -> None:
    """A wrong category or prefix would otherwise render pure English with no sign why."""
    _answer_lookup(monkeypatch, answer)

    with caplog.at_level(logging.WARNING):
        shown = await _form_text(hass, "de")

    assert shown["panel_hint"] == f"{ENGLISH_PANEL_HINT} "
    assert PREFIX in caplog.text
    assert "No usable" in caplog.text


async def test_a_lookup_that_is_not_a_mapping_says_so(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Discarding a whole catalog is the same pure-English outcome, so it is logged."""
    _answer_lookup(monkeypatch, ["not", "a", "mapping"])

    with caplog.at_level(logging.WARNING):
        shown = await _form_text(hass, "de")

    assert shown["panel_hint"] == f"{ENGLISH_PANEL_HINT} "
    assert "expected a Mapping, got list" in caplog.text


async def test_chinese_sentence_ending_on_a_closing_quote_gets_no_ascii_space(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The separator belongs to the script, and the language names it.

    U+201D is Simplified Chinese's closing quote. The sentence ends on it
    deliberately: a rule that sniffs glyphs spaces this string and fails here,
    where a sentence ending on U+3002 would pass either way.
    """
    _answer_lookup(monkeypatch, _catalog(panel_hint="打开“设置面板”"))

    shown = await _form_text(hass, "zh-Hans")

    assert shown["panel_hint"] == "打开“设置面板”"


async def test_english_text_keeps_its_space_when_a_chinese_catalog_degrades(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The language alone does not decide it: the text has to agree.

    A shipped language whose catalog load degrades to English still needs the
    ASCII separator.
    """
    _answer_lookup(monkeypatch, _catalog(panel_hint=ENGLISH_PANEL_HINT))

    shown = await _form_text(hass, "zh-Hans")

    assert shown["panel_hint"] == f"{ENGLISH_PANEL_HINT} "


async def test_korean_keeps_the_separator(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Korean separates words with ASCII spaces, so it is spaced like a Latin script.

    The text is not the English source, so this fails if ``ko`` is ever added
    to the no-space set rather than passing through the English exemption.
    """
    _answer_lookup(monkeypatch, _catalog(panel_hint="설정 패널을 여세요."))

    shown = await _form_text(hass, "ko")

    assert shown["panel_hint"] == "설정 패널을 여세요. "


async def test_latin_sentence_ending_on_a_quote_keeps_its_space(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: a Latin catalog may end on U+201D and still needs the space."""
    _answer_lookup(monkeypatch, _catalog(panel_hint="Öffne das “Panel”"))

    shown = await _form_text(hass, "de")

    assert shown["panel_hint"] == "Öffne das “Panel” "


async def test_connect_and_oauth_prose_follow_the_catalog(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connect-URL and OAuth hints are translated too, not English islands."""
    monkeypatch.setattr(embedded_setup, "build_connect_urls", lambda *a, **k: [])
    hass.config.language = "de"

    shown = placeholders(await open_options_form(hass, data=WITH_SECRET))

    prefix = GERMAN_COMMON["connect_remote_url"].split("{url}")[0]
    assert shown["connect_url"].startswith(prefix)
    assert f"/api/webhook/{WEBHOOK_ID}" in shown["connect_url"]
    assert shown["oauth_creds"] == GERMAN_COMMON["oauth_select_legacy_mode"]


class Scenario(NamedTuple):
    """An entry state that makes the form show the ``common`` sentences ``keys``."""

    name: str
    keys: tuple[str, ...]
    data: dict[str, str]
    options: dict[str, Any]
    tools: str | None = None
    replaced: tuple[str, ...] = ()


LEGACY = {OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_LEGACY}
SCENARIOS = [
    Scenario(
        "paired-server-without-tools",
        (
            "panel_hint",
            "version_line",
            "server_source_paired",
            "tools_module_not_installed",
            "connect_urls_label",
            "oauth_select_legacy_mode",
        ),
        WITH_SECRET,
        {},
    ),
    Scenario(
        "unreadable-versions",
        ("version_unknown", "version_not_installed"),
        WITH_SECRET,
        {},
        replaced=("integration", "metadata"),
    ),
    Scenario(
        "pip-override",
        ("server_source_override",),
        WITH_SECRET,
        {OPT_PIP_SPEC: "ha-mcp==0.0.1"},
    ),
    Scenario(
        "tools-loaded", ("tools_module_installed",), WITH_SECRET, {}, tools="loaded"
    ),
    Scenario(
        "tools-not-loaded", ("tools_module_not_loaded",), WITH_SECRET, {}, tools="idle"
    ),
    Scenario("no-webhook-id", ("connect_urls_pending",), {}, {}),
    Scenario(
        "local-only",
        ("connect_webhook_disabled", "connect_direct_access"),
        WITH_SECRET,
        {OPT_ENABLE_WEBHOOK: False, OPT_BIND_HOST: BIND_HOST_LOOPBACK},
    ),
    Scenario(
        "no-resolvable-url",
        ("connect_remote_url", "connect_local_lan"),
        WITH_SECRET,
        {},
        replaced=("urls",),
    ),
    Scenario(
        "legacy-without-credentials", ("oauth_creds_pending",), WITH_SECRET, LEGACY
    ),
    Scenario(
        "legacy-not-serving",
        ("oauth_not_serving",),
        {
            **WITH_SECRET,
            DATA_OAUTH_CLIENT_ID: "hamcp-id",
            DATA_OAUTH_CLIENT_SECRET: "secret",
            DATA_OAUTH_SIGNING_KEY: "ab" * 32,
        },
        LEGACY,
    ),
]
FAILURES: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "integration": lambda patch: patch.setattr(
        config_flow,
        "async_get_integration",
        AsyncMock(side_effect=IntegrationNotFound(DOMAIN)),
    ),
    "metadata": lambda patch: patch.setattr(
        importlib.metadata,
        "version",
        Mock(side_effect=importlib.metadata.PackageNotFoundError("ha-mcp")),
    ),
    "urls": lambda patch: patch.setattr(
        embedded_setup, "build_connect_urls", lambda *a, **k: []
    ),
}


def test_every_common_sentence_has_a_scenario_that_shows_it() -> None:
    """A sentence added to strings.json needs an entry state that renders it."""
    covered = {key for scenario in SCENARIOS for key in scenario.keys}

    assert covered == set(STRINGS_COMMON)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_failed_lookup_shows_the_strings_json_english(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, scenario: Scenario
) -> None:
    """With no catalog the form shows the English of strings.json, sentence for sentence.

    Nothing else compares the built-in fallback with strings.json: reword a
    string there and the fallback would keep serving the old English to every
    failed lookup.
    """
    if scenario.tools:
        await add_tools_entry(hass, loaded=scenario.tools == "loaded")
    _answer_lookup(monkeypatch, HomeAssistantError("no catalog"))
    for failure in scenario.replaced:
        FAILURES[failure](monkeypatch)

    form = await open_options_form(hass, data=scenario.data, options=scenario.options)

    shown = "\n".join(placeholders(form).values())
    for key in scenario.keys:
        pattern = re.sub(r"\\\{\w+\\\}", ".+", re.escape(STRINGS_COMMON[key]))
        assert re.search(pattern, shown, re.DOTALL), f"{key} is not shown:\n{shown}"
