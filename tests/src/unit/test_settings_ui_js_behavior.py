"""Behavioural tests for the rendered ``<script>`` body in ``settings_ui/__init__.py``.

Use the JSDOM harness at ``tests/js/harness.mjs`` to drive the script
through realistic flows and assert on observable side effects (HTTP
calls issued, BroadcastChannel messages emitted, DOM mutations,
``location.reload`` invocations).

These tests catch the "broken page with no in-page diagnostic" failure
class — script-level errors silently abort handler init and leave the
page stuck on its initial state with no signal in the UI. The
parse-time guard in ``test_rendered_scripts_parse.py`` catches syntax
breaks; this file catches behavioural regressions on top of it.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar, get_args

import pytest

from ._js_harness import HarnessResult, extract_script_body, run_script

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def settings_script() -> str:
    """The rendered ``<script>`` body from ``_SETTINGS_HTML``.

    Module-scoped because the body is pure and large; re-extracting on
    every test would be wasted work.
    """
    from ha_mcp.settings_ui import _SETTINGS_HTML

    return extract_script_body(_SETTINGS_HTML)


# Element ids the script binds top-level handlers on. Drift between
# this set and what the production script reaches via
# ``document.getElementById(...).addEventListener(...)`` is checked at
# import time by ``_assert_min_dom_covers_handlers`` below, so a future
# handler that lands without a matching DOM stub fails the suite
# immediately with a clear message instead of letting tests silently
# fail at init.
_TOP_LEVEL_ELEMENT_IDS = [
    "status",
    "restartBtn",
    "restartNotice",
    "restartNoticeText",
    "sidecarStopRow",
    "stopSidecarBtn",
    "search",
    "groups",
    "summary",
    "featuresBody",
    "backupConfigForm",
    "backupConfigActions",
    "backupConfigSave",
    "backupConfigStatus",
    "backupRefresh",
    "backupBulkDelete",
    "backupDomain",
    "backupEntity",
    "backupState",
    "backupList",
    "modalBackdrop",
    "modalBody",
    "modalTitle",
    "modalClose",
    "panel-tools",
    "panel-server",
    "panel-backups",
    # Tool Security Policies tab (#966): the master-toggle checkbox
    # mirrors the Server-Settings flag and posts to /api/settings/features;
    # the global-settings save button writes wait_seconds / TTL.
    "policy-master-toggle",
    # enable_security_policy_tool (#2148) — registers
    # ha_manage_security_policy; same save-then-verify flow as the master.
    "policy-manage-tool-toggle",
    "policy-save-global-btn",
    # Read Only Mode toggle (#1569) — Tools tab, above the search box.
    # Same save-then-verify flow as the policy master toggle.
    "read-only-mode-toggle",
    # Where applyFlagToggle writes the env-locked note for that switch.
    "read-only-locked",
    # Advanced settings panel — the 5 section containers that
    # loadAdvancedSettings() writes to via innerHTML. Without container
    # divs in MIN_DOM, renderSection silently no-ops (getElementById
    # returns null) and the behavioural tests find an empty body.
    # (The advanced fields auto-save on change, so there is no Save
    # button/status row here anymore.)
    # Connection section was removed from the panel;
    # advSearch is now the first rendered advanced section.
    "advSearch",
    "advOperations",
    "advToolsSurface",
    "advDiagnostics",
    # Developer section (issue #1775) — bottom of panel-server; hosts the
    # dev-mode toggle whose enable path is confirm()-gated.
    "advDeveloper",
    # Beta features dedicated container — beta
    # master + sub-flags render here, NOT into featuresBody, so the
    # dangerous block sits at the bottom of panel-server.
    "betaBody",
    # Version footer span — populated from /api/settings/info on
    # init; without the container element the JS would no-op.
    "versionFooterText",
]


def _min_dom_row(el_id: str) -> str | None:
    """DOM stub markup for ``el_id`` (first half of the dispatch); None emits nothing."""
    if el_id.startswith("backup") and el_id.endswith(("Domain", "Entity", "State")):
        return f'<select id="{el_id}"></select>'
    if el_id in ("backupList", "featuresBody"):
        return f'<table><tbody id="{el_id}"></tbody></table>'
    if el_id == "modalBackdrop":
        # Full modal scaffold (mirrors settings.html) so the focus-trap
        # code in showModal/closeModal — which queries `.modal` and
        # #modalClose — has the structure it depends on.
        return (
            '<div id="modalBackdrop">'
            '<div class="modal" role="dialog" tabindex="-1">'
            '<div class="modal-header">'
            '<span class="modal-title" id="modalTitle"></span>'
            '<button class="modal-close" id="modalClose">×</button>'
            "</div>"
            '<div class="modal-body" id="modalBody"></div>'
            "</div>"
            "</div>"
        )
    if el_id in ("modalBody", "modalTitle", "modalClose"):
        return None  # rendered as children of modalBackdrop above
    if el_id.startswith("panel-"):
        return f'<div id="{el_id}" class="panel"></div>'
    return _min_dom_row_tail(el_id)


def _min_dom_row_tail(el_id: str) -> str | None:
    """DOM stub markup for ``el_id`` (second half of the dispatch); None emits nothing."""
    if el_id in (
        "restartBtn",
        "stopSidecarBtn",
        "backupConfigSave",
        "backupRefresh",
        "backupBulkDelete",
    ):
        return f'<button id="{el_id}"></button>'
    if el_id == "restartNotice":
        return '<div id="restartNotice"><span id="restartNoticeText"></span></div>'
    if el_id == "restartNoticeText":
        return None  # rendered as a child of restartNotice above
    if el_id == "search":
        return '<input id="search" />'
    if el_id in (
        "policy-master-toggle",
        "policy-manage-tool-toggle",
        "read-only-mode-toggle",
    ):
        return f'<input id="{el_id}" type="checkbox" />'
    if el_id == "policy-save-global-btn":
        return '<button id="policy-save-global-btn"></button>'
    return f'<div id="{el_id}"></div>'


def _build_min_dom() -> str:
    """Render an HTML document that supplies every element the script
    binds a top-level handler on, so the init pass doesn't throw on a
    missing-element addEventListener call.
    """
    rows = []
    for el_id in _TOP_LEVEL_ELEMENT_IDS:
        row = _min_dom_row(el_id)
        if row is not None:
            rows.append(row)
    body = "\n  ".join(rows)
    return f"<!DOCTYPE html>\n<html><body>\n  {body}\n</body></html>"


MIN_DOM = _build_min_dom()


def _assert_min_dom_covers_handlers() -> None:
    """Fail at collection time if the production script's top-level
    ``document.getElementById(...)`` calls drift past
    ``_TOP_LEVEL_ELEMENT_IDS``. Without this, a production change that
    adds a new top-level ``getElementById(...).addEventListener(...)``
    would throw during JSDOM init, every restart-flow assertion in the
    file would silently fail with empty-side-effect outputs, and a
    maintainer would have to dig through ``result.errors`` to find the
    actual root cause.
    """
    from ha_mcp.settings_ui import _SETTINGS_HTML

    script = extract_script_body(_SETTINGS_HTML)
    # Top-level (not indented) getElementById(...) calls — handlers
    # bound at script load time. Indented calls are inside functions
    # and only run when those functions execute, which our tests
    # control.
    referenced = set(
        re.findall(r"^document\.getElementById\('([^']+)'\)", script, re.M)
    )
    missing = referenced - set(_TOP_LEVEL_ELEMENT_IDS)
    if missing:
        raise RuntimeError(
            "settings_ui/__init__.py top-level getElementById ids drifted past "
            f"_TOP_LEVEL_ELEMENT_IDS in this test file: {sorted(missing)}. "
            "Add them and rebuild MIN_DOM.",
        )


_assert_min_dom_covers_handlers()


# Default routes for the init-time fetches. Individual tests merge in
# their own entries via ``{**DEFAULT_FETCHES, "/restart": ...}``.
DEFAULT_FETCHES: dict[str, dict] = {
    "/api/settings/features": {
        "status": 200,
        "json": {"features": [], "values": {}},
    },
    "/api/settings/tools": {
        "status": 200,
        "json": {"tools": [], "states": {}, "addon_mode": True},
    },
    "/api/settings/info": {
        "status": 200,
        "json": {"instance_id": "baseline-id"},
    },
    "/api/settings/backup-config": {
        "status": 200,
        "json": {},
    },
}


# The add-on tool as the settings API renders it, plus a Russian translation
# supplied by the tests themselves (see _app_tool_dom).
APP_TOOL_NAME = "ha_get_app"
APP_TOOL_TITLE = "Get Apps (add-ons)"
APP_TOOL_GROUP = "Apps (add-ons)"
APP_TOOL_TITLE_RU = "Получить приложения"
APP_TOOL_GROUP_RU = "Приложения"


class TestLocalizationBehavior:
    """Russian catalog application and language-selector behaviour."""

    @staticmethod
    def _localized_dom(
        locale: str = "ru",
        *,
        tools: dict[str, dict[str, str]] | None = None,
        tool_groups: dict[str, str] | None = None,
    ) -> str:
        from ha_mcp.settings_ui._i18n import build_payload, serialize_payload

        catalog = build_payload(locale)
        if tools:
            catalog["tools"].update(tools)
        if tool_groups:
            catalog["tool_groups"].update(tool_groups)
        payload = serialize_payload(catalog)
        additions = (
            f'<script id="ha-mcp-i18n" type="application/json">{payload}</script>'
            '<select id="languageToggle"></select>'
            '<span id="i18nProbe" data-i18n="tabs.tools">Tools</span>'
        )
        return MIN_DOM.replace("</body>", additions + "</body>")

    @staticmethod
    def _app_tool_fetches() -> dict:
        """The tools endpoint as the server renders it for the add-on tool."""
        return {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [
                        {
                            "name": APP_TOOL_NAME,
                            "title": APP_TOOL_TITLE,
                            "description": "Get Home Assistant apps.",
                            "primary_tag": APP_TOOL_GROUP,
                            "category": "read",
                        }
                    ],
                    "states": {},
                },
            },
        }

    def _app_tool_dom(self) -> str:
        """A catalog carrying a translation for that tool and its group.

        Both entries are supplied here instead of read out of the shipped
        Russian catalog. What the catalogs hold for this tool is not this
        test's subject and does not stand still: the English behind the
        heading and the title moves through the post-merge sync, which re-keys
        the group and retranslates the title under the tool's current name.
        Pinning the shipped Russian made this test green in a PR and red on
        master one sync later, and reading it back would make the localized
        search term collapse onto the English one for as long as the key is
        missing — the test would keep passing while testing nothing.
        """
        return self._localized_dom(
            tools={
                APP_TOOL_NAME: {
                    "title": APP_TOOL_TITLE_RU,
                    "description": "Список приложений",
                }
            },
            tool_groups={APP_TOOL_GROUP: APP_TOOL_GROUP_RU},
        )

    def test_russian_static_group_and_tool_copy(self, settings_script: str) -> None:
        result = run_script(
            settings_script,
            initial_html=self._app_tool_dom(),
            fetch_map=self._app_tool_fetches(),
        )
        assert not result.errors
        assert "Инструменты" in result.dom
        assert APP_TOOL_GROUP_RU in result.dom
        assert APP_TOOL_TITLE_RU in result.dom

    def test_tool_search_matches_localized_title(self, settings_script: str) -> None:
        invoke = """
              await new Promise(r => setTimeout(r, 200));
              const search = document.getElementById('search');
              search.value = '__LOCALIZED__';
              search.dispatchEvent(new Event('input', {bubbles: true}));
              const row = document.querySelector('[data-name="__NAME__"]');
              document.body.dataset.localizedSearchMatch = String(
                row && !row.classList.contains('hidden')
              );
              search.value = '__SOURCE__';
              search.dispatchEvent(new Event('input', {bubbles: true}));
              document.body.dataset.sourceSearchMatch = String(
                row && !row.classList.contains('hidden')
              );
            """
        result = run_script(
            settings_script,
            initial_html=self._app_tool_dom(),
            fetch_map=self._app_tool_fetches(),
            invoke=invoke.replace("__LOCALIZED__", APP_TOOL_TITLE_RU.lower())
            .replace("__SOURCE__", APP_TOOL_TITLE.lower())
            .replace("__NAME__", APP_TOOL_NAME),
        )
        assert not result.errors
        assert 'data-localized-search-match="true"' in result.dom
        assert 'data-source-search-match="true"' in result.dom

    def test_tool_translation_placeholder_mismatch_falls_back_to_source(
        self, settings_script: str
    ) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [
                        {
                            "name": "ha_placeholder_example",
                            "title": "Open {entity}",
                            "description": "Inspect {entity}",
                            "primary_tag": "System",
                            "category": "read",
                        }
                    ],
                    "states": {},
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=self._localized_dom(
                tools={
                    "ha_placeholder_example": {
                        "title": "Открыть",
                        "description": "Проверить {device}",
                    }
                }
            ),
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const row = document.querySelector(
                '[data-name="ha_placeholder_example"]'
              );
              document.body.dataset.placeholderRow = row ? row.textContent : '';
            """,
        )

        assert not result.errors
        assert "Open {entity}" in result.dom
        assert "Inspect {entity}" in result.dom
        row_text = re.search(r'data-placeholder-row="([^"]*)"', result.dom)
        assert row_text is not None
        assert "Открыть" not in row_text.group(1)
        assert "Проверить {device}" not in row_text.group(1)
        warnings = [
            str(entry["args"]) for entry in result.console if entry["level"] == "warn"
        ]
        assert any("title translation" in warning for warning in warnings)
        assert any("description translation" in warning for warning in warnings)

    def test_tool_translation_validates_description_against_displayed_text(
        self, settings_script: str
    ) -> None:
        # The row shows only the first line of a tool description, and the
        # description placeholder check compares against that DISPLAYED text,
        # not the full runtime docstring. Here the docstring carries {entity}
        # only on a deeper line (like the real {slug}/{type} API-path tokens),
        # so a condensed German translation that omits it must still render
        # instead of falling back to English. The title still uses set
        # semantics against the displayed title (reordered placeholder ok).
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [
                        {
                            "name": "ha_placeholder_valid",
                            "title": "Open {entity}",
                            "description": "Inspect the entity\nUse {entity} twice: {entity}",
                            "primary_tag": "System",
                            "category": "read",
                        }
                    ],
                    "states": {},
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=self._localized_dom(
                tools={
                    "ha_placeholder_valid": {
                        "title": "{entity} öffnen",
                        "description": "Die Entität inspizieren",
                    }
                }
            ),
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const row = document.querySelector(
                '[data-name="ha_placeholder_valid"]'
              );
              document.body.dataset.placeholderValidRow = row ? row.textContent : '';
            """,
        )

        assert not result.errors
        row_text = re.search(r'data-placeholder-valid-row="([^"]*)"', result.dom)
        assert row_text is not None
        # Title: reordered {entity} accepted (set semantics vs displayed title).
        assert "{entity} öffnen" in row_text.group(1)
        # Description: matches the displayed first line (no placeholder there),
        # so it renders despite the {entity} tokens deeper in the docstring.
        assert "Die Entität inspizieren" in row_text.group(1)
        assert not any(
            entry["level"] == "warn"
            and "translation" in str(entry["args"])
            and "placeholder mismatch" in str(entry["args"])
            for entry in result.console
        )

    def test_language_selector_sets_cookie_and_reloads(
        self, settings_script: str
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=self._localized_dom("en"),
            fetch_map=DEFAULT_FETCHES,
            invoke="""
              const select = document.getElementById('languageToggle');
              select.value = 'ru';
              select.dispatchEvent(new Event('change', {bubbles: true}));
              document.body.dataset.localeCookie = document.cookie;
            """,
        )
        assert not result.errors
        assert result.reloads == 1
        assert "ha_mcp_locale=ru" in result.dom

    def test_malformed_locale_cookie_falls_back_to_auto(
        self, settings_script: str
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=self._localized_dom("en"),
            fetch_map=DEFAULT_FETCHES,
            prelude="document.cookie = 'ha_mcp_locale=%E0%A4%A; Path=/';",
            invoke="""
              const select = document.getElementById('languageToggle');
              document.body.dataset.languageValue = select.value;
            """,
        )
        assert not result.errors
        assert 'data-language-value="auto"' in result.dom
        assert any(
            entry["level"] == "warn"
            and "ignoring malformed locale cookie" in str(entry["args"])
            for entry in result.console
        )


class TestTranslationHtmlSanitization:
    """Catalog strings rendered into ``innerHTML`` go through ``tHtml``:
    the whole value is HTML-escaped and only the fixed allowlist of
    formatting tags (``<code>``, ``<strong>``, the internal tab links) is
    restored, so a hostile or vandalized community translation cannot
    contribute any other markup (CodeQL js/xss-through-dom).
    """

    @staticmethod
    def _dom_with_catalog(messages: dict[str, str], extra_html: str = "") -> str:
        from ha_mcp.settings_ui._i18n import build_payload, serialize_payload

        catalog = build_payload("en")
        catalog["messages"].update(messages)
        payload = serialize_payload(catalog)
        additions = (
            f'<script id="ha-mcp-i18n" type="application/json">{payload}</script>'
            + extra_html
        )
        return MIN_DOM.replace("</body>", additions + "</body>")

    def test_malicious_static_html_translation_is_neutralized(
        self, settings_script: str
    ) -> None:
        """A ``[data-i18n-html]`` catalog value carrying a non-allowlisted
        element renders as escaped text, while allowlisted formatting
        survives.
        """
        result = run_script(
            settings_script,
            initial_html=self._dom_with_catalog(
                {
                    "notice.env_pins": (
                        'evil <img src=x onerror="document.title=1"> but '
                        "<code>kept-code</code> and <strong>kept-strong</strong>"
                    )
                },
                '<div id="xssProbe" data-i18n-html="notice.env_pins">original</div>',
            ),
            fetch_map=DEFAULT_FETCHES,
        )
        _assert_clean_init(result)
        assert "<img" not in result.dom
        assert "&lt;img" in result.dom
        assert "<code>kept-code</code>" in result.dom
        assert "<strong>kept-strong</strong>" in result.dom

    def test_allowlisted_panel_link_is_restored(self, settings_script: str) -> None:
        """The internal tab-link shape used by ``policies.rules.empty`` is
        restored exactly; a link with any other attribute set stays escaped.
        """
        result = run_script(
            settings_script,
            initial_html=self._dom_with_catalog(
                {
                    "policies.rules.empty": (
                        'Go to the <a href="#" data-panel-link="tools">Tools</a> tab, '
                        'not <a href="https://evil.example">elsewhere</a>'
                    )
                },
                '<div id="linkProbe" data-i18n-html="policies.rules.empty">x</div>',
            ),
            fetch_map=DEFAULT_FETCHES,
        )
        _assert_clean_init(result)
        assert '<a href="#" data-panel-link="tools">Tools</a>' in result.dom
        assert '<a href="https://evil.example">' not in result.dom
        assert "&lt;a href=" in result.dom

    def test_env_pinned_note_params_substituted_after_escaping(
        self, settings_script: str
    ) -> None:
        """Placeholder params are trusted fragments injected after the
        catalog string is escaped: a hostile translation around
        ``{variable}`` is neutralized while the built ``<code>`` fragment
        with the env var name still renders as markup.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [
                        {
                            "name": "ha_foo",
                            "title": "Foo",
                            "primary_tag": "Utilities",
                            "tags": ["Utilities"],
                            "description": "Test tool",
                            "annotations": {},
                        }
                    ],
                    "states": {"ha_foo": "disabled"},
                    "env_pinned": {"ha_foo": "disabled"},
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=self._dom_with_catalog(
                {
                    "tools.notes.env_pinned": (
                        'Pwn <em onmouseover="x()">{variable}</em> here'
                    )
                }
            ),
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert "<code>DISABLED_TOOLS</code>" in result.dom
        assert "&lt;em" in result.dom
        assert "<em onmouseover" not in result.dom


def _assert_clean_init(result: HarnessResult) -> None:
    """Fail loud on any script-init or transpile error.

    Each test in this file asserts on side effects (fetches, broadcasts,
    reloads, alerts). When the script throws at init time the side
    effects come back empty, the side-effect assertion fires with a
    misleading message ("expected 1 POST, got 0"), and the actual root
    cause sits in ``result.errors``. Calling this at the top of every
    test surfaces the real error first.
    """
    init_errors = [
        e
        for e in result.errors
        if e.startswith(("script init:", "transpile failure", "invoke:", "jsdom error"))
    ]
    assert not init_errors, f"script failed to initialise: {init_errors}"


# ---------------------------------------------------------------------------
# restartAddon() flow
# ---------------------------------------------------------------------------


class TestVersionFooter:
    """The footer at the bottom of the settings page reads from
    /api/settings/info on init and renders the running version and installation
    method so an operator can identify the active build without leaving the UI.
    """

    @staticmethod
    def _footer_text(result: HarnessResult) -> str:
        match = re.search(r'id="versionFooterText">([^<]*)</div>', result.dom)
        assert match is not None, (
            f"version footer missing; dom tail: {result.dom[-1500:]}"
        )
        return match.group(1)

    def test_version_rendered_from_settings_info(self, settings_script: str) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/info": {
                "status": 200,
                "json": {
                    "is_addon": True,
                    "is_sidecar": False,
                    "instance_id": "test-id",
                    "started_at": 0,
                    "version": "7.5.0.dev400",
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert self._footer_text(result) == "ha-mcp 7.5.0.dev400"

    @pytest.mark.parametrize(
        ("deployment_mode", "expected_label"),
        [
            ("embedded", "embedded"),
            ("sidecar", "sidecar"),
            ("addon", "app/add-on"),
            ("docker", "container/docker"),
            ("pyinstaller", "standalone binary"),
            ("git", "source checkout"),
            ("pypi", "python package"),
            ("unknown", "unknown"),
            ("future-mode", "future-mode"),
            ("constructor", "constructor"),
        ],
    )
    def test_installation_method_rendered_from_settings_info(
        self,
        settings_script: str,
        deployment_mode: str,
        expected_label: str,
    ) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/info": {
                "status": 200,
                "json": {
                    "is_addon": deployment_mode == "addon",
                    "is_sidecar": deployment_mode == "sidecar",
                    "deployment_mode": deployment_mode,
                    "instance_id": "test-id",
                    "started_at": 0,
                    "version": "8.2.0",
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert (
            self._footer_text(result)
            == f"ha-mcp 8.2.0 · installation: {expected_label}"
        )

    @pytest.mark.parametrize(
        ("deployment_mode", "translated_label"),
        [("embedded", "intégré"), ("sidecar", "accompagnement")],
    )
    def test_server_mode_labels_stay_raw_when_footer_phrase_is_localized(
        self,
        settings_script: str,
        deployment_mode: str,
        translated_label: str,
    ) -> None:
        from ha_mcp.settings_ui._i18n import build_payload, serialize_payload

        catalog = build_payload("fr")
        catalog["messages"].update(
            {
                "footer.installation": "mode d’installation : {method}",
                f"footer.deployment.{deployment_mode}": translated_label,
            }
        )
        payload = serialize_payload(catalog)
        localized_dom = MIN_DOM.replace(
            "</body>",
            f'<script id="ha-mcp-i18n" type="application/json">{payload}</script>'
            "</body>",
        )
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/info": {
                "status": 200,
                "json": {
                    "is_addon": False,
                    "is_sidecar": deployment_mode == "sidecar",
                    "deployment_mode": deployment_mode,
                    "instance_id": "test-id",
                    "started_at": 0,
                    "version": "8.2.0",
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=localized_dom,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert self._footer_text(result) == (
            f"ha-mcp 8.2.0 · mode d’installation : {deployment_mode}"
        )

    def test_version_omitted_when_info_response_lacks_version(
        self, settings_script: str
    ) -> None:
        """Defensive — older standalone deployments without the version
        field in /api/settings/info must leave the footer empty rather
        than rendering ``ha-mcp undefined`` or throwing.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/info": {
                "status": 200,
                "json": {
                    "is_addon": False,
                    "is_sidecar": False,
                    "instance_id": "test-id",
                    "started_at": 0,
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert self._footer_text(result) == ""


class TestXssGuard:
    """Defense-in-depth check on the leaf-level escaping discipline of
    the settings JS. Inject ``<script>`` tags into a setting's value
    via the fetched API response and assert the resulting DOM holds
    the text content escaped, not as a live script element.
    """

    def test_setting_value_with_script_tag_renders_as_text(
        self, settings_script: str
    ) -> None:
        adv_field = {
            "field": "mcp_server_name",
            "env_var": "MCP_SERVER_NAME",
            "value": "<script>window.__xss_pwned = true;</script>",
            "type": "str",
            "section": "diagnostics",
            "origin": "default",
            "editable": True,
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [adv_field], "is_addon": False},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const probe = document.createElement('div');
              probe.id = '__xss_probe';
              probe.dataset.pwned = String(!!window.__xss_pwned);
              const input = document.querySelector(
                'input[data-adv-field="mcp_server_name"]'
              );
              probe.dataset.inputValue = input ? input.value : '__no_input__';
              probe.dataset.scriptInDoc = String(
                document.querySelectorAll('script').length
              );
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        # The probe div's ``data-input-value`` attribute carries the
        # full XSS payload as a literal string, containing both ``<``
        # and ``>``. Slicing a clean substring with a single regex is
        # fragile — use the probe id as an anchor and slice from there
        # to the next ``</div>`` (the probe is the last element we
        # appended, no nested children).
        anchor = 'id="__xss_probe"'
        idx = result.dom.find(anchor)
        assert idx != -1, f"xss probe missing; dom tail: {result.dom[-1500:]}"
        end = result.dom.find("</div>", idx)
        probe_html = result.dom[idx : end if end != -1 else len(result.dom)]
        # 1. The injected script payload must NOT have executed.
        assert 'data-pwned="false"' in probe_html, (
            f"injected <script> executed — escapeHtml regressed: {probe_html}"
        )
        # 2. The payload landed in the input's value PROPERTY as a plain
        #    string (escapeHtml prevented HTML parsing). HTML5 allows ``<``
        #    in attribute values literally, so DOM serialization of the
        #    attribute may not re-escape it — checking the IDL value
        #    property is the unambiguous assertion that the payload was
        #    treated as text, not parsed as a tag.
        assert (
            "&lt;script&gt;window.__xss_pwned" in probe_html
            or "<script>window.__xss_pwned" in probe_html
        ), f"input.value didn't receive payload as text: {probe_html}"
        # 3. No <script> element should have been parsed into the document
        #    from the rendered field. The MIN_DOM has no scripts of its
        #    own, so the count must be 0.
        assert 'data-script-in-doc="0"' in probe_html, (
            f"a <script> element was parsed into the document — escape leak: "
            f"{probe_html}"
        )


class TestRestartAddonFlow:
    """Covers the ``restartAddon`` concurrency guard, 4xx-suppress-reload
    branch, and 5xx fall-through path.
    """

    def test_concurrent_calls_issue_only_one_supervisor_restart(
        self, settings_script: str
    ) -> None:
        """The ``restartInProgress`` flag must block a second invocation.

        DevTools re-entry, accessibility tools, and cross-tab broadcasts
        can all queue a second ``restartAddon()`` call before the first
        finishes. Without the guard, each call would POST
        ``/api/settings/restart`` again — queueing redundant supervisor
        restarts and a redundant page reload.
        """
        # Info returns a baseline that never flips, so the probe loop
        # keeps running for the full probe-timeout window — plenty of
        # time for both restartAddon() invocations to race.
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/restart": {"status": 200, "json": {}},
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              window.restartAddon();
              window.restartAddon();
            """,
        )
        _assert_clean_init(result)

        restart_posts = [
            f
            for f in result.fetches
            if "/api/settings/restart" in f["url"] and f["method"] == "POST"
        ]
        assert len(restart_posts) == 1, (
            f"expected exactly one POST to /api/settings/restart, "
            f"got {len(restart_posts)}: {restart_posts}"
        )

    def test_4xx_response_suppresses_reload_keeps_button_enabled(
        self, settings_script: str
    ) -> None:
        """4xx is a genuine config error (e.g. SUPERVISOR_TOKEN unset).

        Restart was NOT initiated — the page must stay, the user must
        see the failure message, and the button must remain enabled so
        they can retry after fixing the config. No broadcast — other
        tabs would only see a misleading "restart in progress".
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/restart": {
                "status": 400,
                "json": {"error": {"message": "SUPERVISOR_TOKEN unset"}},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await window.restartAddon();
              // Snapshot the button's post-flow state on body for the
              // assertion below — querying via JS is more robust than
              // string-slicing the serialised dom.
              const btn = document.getElementById('restartBtn');
              document.body.dataset.restartBtnDisabled = String(btn.disabled);
              document.body.dataset.restartBtnText = btn.textContent || '';
            """,
        )
        _assert_clean_init(result)

        assert result.reloads == 0, "4xx response must not trigger a page reload"
        assert result.broadcasts_of_type("restart-initiated") == [], (
            "4xx response must not broadcast restart-initiated to other tabs"
        )
        assert any("SUPERVISOR_TOKEN unset" in a for a in result.alerts), (
            f"expected alert with config error, got alerts={result.alerts}"
        )
        assert 'data-restart-btn-disabled="false"' in result.dom, (
            "restartBtn.disabled should be false after a 4xx retry path"
        )

    def test_5xx_response_falls_through_to_reload_cycle(
        self, settings_script: str
    ) -> None:
        """5xx means supervisor IS killing our upstream mid-response.

        The restart IS in flight even though we got an error code — the
        ingress dropped because our process is going away. Must fall
        through to the poll-and-reload cycle, not surface the 5xx as a
        config error.
        """
        # Seed the info endpoint with two baseline responses then a
        # flipped one. The harness's `responses: [...]` shape advances
        # per call and sticks on the last entry, so any number of
        # probe iterations beyond the first sees the flip — that's
        # what lets the probe terminate with restarted=true.
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/restart": {"status": 503, "body": ""},
            "/api/settings/info": {
                "responses": [
                    {"status": 200, "json": {"instance_id": "baseline-id"}},
                    {"status": 200, "json": {"instance_id": "baseline-id"}},
                    {"status": 200, "json": {"instance_id": "flipped-id"}},
                ],
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await window.restartAddon();",
        )
        _assert_clean_init(result)

        assert result.reloads >= 1, (
            f"5xx restart response must still trigger reload cycle, "
            f"reloads={result.reloads}, errors={result.errors}"
        )

    def test_probe_waits_for_instance_id_flip_not_just_200(
        self, settings_script: str
    ) -> None:
        """The whole point of the ``previousInstanceId`` plumbing.

        If supervisor silently fails to restart, the OLD process keeps
        answering 200. Pre-fix, the probe would see a 200 and reload —
        but the user would land back on the same broken instance.
        Post-fix, the probe must keep polling until ``instance_id``
        differs.

        Here we force info to always return the same id as the
        baseline, confirm the probe DOES NOT reload, and confirm the
        manual-reload fallback UI lands (so the user isn't left in an
        indefinite spinner).
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/restart": {"status": 200, "json": {}},
            "/api/settings/info": {
                "status": 200,
                "json": {"instance_id": "baseline-id"},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await window.restartAddon();",
            # 80s of virtual time covers the 60s probe window with margin.
            settle_ms=80000,
        )
        _assert_clean_init(result)

        assert result.reloads == 0, (
            "probe must not reload when instance_id never flips — "
            "that would land the user back on the same broken instance"
        )
        assert (
            "did not come back online. reload the page manually." in result.dom.lower()
        ), f"expected manual-reload fallback message, dom={result.dom[:600]}"


# ---------------------------------------------------------------------------
# BroadcastChannel listener + null-guard
# ---------------------------------------------------------------------------


class TestBroadcastChannelListener:
    """The cross-tab restart UX hinges on every open tab reacting to the
    originating tab's broadcasts. These tests pin the listener contract,
    plus the null-guard that lets the page boot in browsing contexts
    where BroadcastChannel is unavailable.
    """

    def test_restart_required_event_shows_notice_banner(
        self, settings_script: str
    ) -> None:
        """When ANY tab saves a flag needing restart, all tabs see the banner."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=DEFAULT_FETCHES,
            broadcast_events=[
                {
                    "channel": "ha-mcp-settings",
                    "data": {"type": "restart-required"},
                    "delayMs": 50,
                },
            ],
        )
        _assert_clean_init(result)

        # The notice div picks up the "show" class via classList.add.
        assert 'class="show"' in result.dom or "show" in result.dom, (
            "restartNotice should have 'show' class after restart-required broadcast"
        )

    def test_restart_initiated_event_runs_reload_cycle_in_listening_tab(
        self, settings_script: str
    ) -> None:
        """The originating tab broadcasts ``restart-initiated`` so every
        OTHER tab runs its own poll-then-reload cycle. Without this,
        the non-originating tabs stay on a stale connection to a dead
        addon.
        """
        fetches = {
            **DEFAULT_FETCHES,
            # Probe sees the flip on first call after broadcast fires.
            "/api/settings/info": {
                "status": 200,
                "json": {"instance_id": "flipped"},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            broadcast_events=[
                {
                    "channel": "ha-mcp-settings",
                    "data": {
                        "type": "restart-initiated",
                        "previousInstanceId": "baseline-id",
                    },
                    "delayMs": 50,
                },
            ],
        )
        _assert_clean_init(result)

        assert result.reloads >= 1, (
            f"listening tab should reload after restart-initiated broadcast; "
            f"reloads={result.reloads}, errors={result.errors}"
        )

    def test_script_boots_without_broadcastchannel_global(
        self, settings_script: str
    ) -> None:
        """The ``typeof BroadcastChannel === 'function'`` null-guard at
        module init lets the page render in iframe / older-browser
        contexts where BroadcastChannel is undefined.

        Removing the guard would throw a ReferenceError during init and
        every page interaction (including the restart button, which is
        the user's recovery path) would fail.
        """
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=DEFAULT_FETCHES,
            broadcast_channel_unavailable=True,
            invoke="""
              // Confirm the page reached interactive state — restartBtn
              // listener wired, no init throw.
              const btn = document.getElementById('restartBtn');
              document.body.dataset.boot = btn ? 'ok' : 'no-btn';
            """,
        )
        _assert_clean_init(result)
        assert 'data-boot="ok"' in result.dom, (
            "page must boot when BroadcastChannel is undefined"
        )


# ---------------------------------------------------------------------------
# saveFeatureFlag JSON-parse fallback
# ---------------------------------------------------------------------------


class TestSaveFeatureFlagJsonParseFallback:
    """Pins the contract for the truncated-body fallback in
    ``saveFeatureFlag``.

    A 200 OK with a body that can't be parsed as JSON used to silently
    skip the restart-required surface, leaving the user thinking the
    change was live when it actually requires a restart. The fallback
    defaults ``restart_required`` to true on that path.
    """

    def test_truncated_200_body_defaults_to_restart_required(
        self, settings_script: str
    ) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            # 200 OK + intentionally broken JSON body — the production
            # try/catch around resp.json() falls back to {restart_required: true}.
            "/api/settings/features": {
                "status": 200,
                "body": "{not-json",
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await window.saveFeatureFlag('foo', true);",
        )
        _assert_clean_init(result)

        assert "show" in result.dom, (
            "restartNotice should have 'show' class after truncated-body save"
        )
        assert result.broadcasts_of_type("restart-required"), (
            f"truncated-body save should broadcast restart-required, "
            f"got broadcasts={result.broadcasts}"
        )

    def test_error_response_does_not_default_to_restart_required(
        self, settings_script: str
    ) -> None:
        """The fallback only applies on ``resp.ok`` — error responses
        must surface the HTTP status, not silently claim success.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 500,
                "body": "{not-json",
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await window.saveFeatureFlag('foo', true);",
        )
        _assert_clean_init(result)

        assert not result.broadcasts_of_type("restart-required"), (
            f"failed save must not broadcast restart-required; "
            f"got broadcasts={result.broadcasts}"
        )


# ---------------------------------------------------------------------------
# Tool Security Policies tab (#966)
# ---------------------------------------------------------------------------


def _policy_panel_dom() -> str:
    """MIN_DOM stub plus the elements the policy tab queries.

    The script binds top-level handlers on policy-master-toggle /
    policy-save-global-btn (covered by MIN_DOM), but the bodies of
    policyLoadConfig / policyLoadPending fetch and write into
    policy-* sub-elements that don't appear in MIN_DOM. Add the
    minimum set so the policy-tab invocations don't throw on missing
    elements when we exercise them directly.
    """
    extras = """
      <div id="policy-pending-list"></div>
      <div id="policy-load-error" style="display:none"></div>
      <div id="policy-rules-empty" style="display:none"></div>
      <div id="policy-rules-list"></div>
      <input id="policy-wait-seconds" />
      <input id="policy-ttl-minutes" />
      <div class="pin-notice" id="policyUnknownNotice"></div>
      <div class="feature-locked-note" id="policy-master-locked"></div>
      <div class="feature-locked-note" id="policy-manage-tool-locked"></div>
    """
    return MIN_DOM.replace("</body>", extras + "</body>")


def _server_503_body() -> str:
    """The exact 503 body the server sends, not a copy of it.

    A hand-written fixture drifts, and drifts in the direction that keeps its
    own assertion passing: the first two copies of this paragraph in this file
    both dropped the sentence naming the three causes and wrote ``addon log``
    where the server writes ``app (add-on) log``, so an assertion on
    ``addon log`` passed only because the fixture was wrong.
    """
    from ha_mcp.settings_ui import POLICY_UNAVAILABLE_MESSAGE

    return POLICY_UNAVAILABLE_MESSAGE


# One /api/settings/features reply carrying the policy-tool flag as off.
# Reused across the save-flow tests below, whose `responses` arrays must
# account for EVERY call to that URL, GET and POST alike — the queue is
# keyed by URL, not method. Init alone makes TWO reads (loadTools ->
# loadPolicyState, and loadFeatureFlags), so each sequence below opens with
# three of these: the two init reads plus the explicit sync in `invoke`.
_OFF_FLAG_RESPONSE: dict = {
    "status": 200,
    "json": {"flags": {"enable_security_policy_tool": {"value": False}}},
}

# Both Policies-tab flags reported and off: the state a real operator starts
# from. The POST-body tests need it because an ABSENT flag entry now renders
# its switch indeterminate and disabled — dispatching a synthetic change on
# that switch would exercise a click no user could make.
_BOTH_POLICY_FLAGS_OFF_RESPONSE: dict = {
    "status": 200,
    "json": {
        "flags": {
            "enable_tool_security_policies": {"value": False},
            "enable_security_policy_tool": {"value": False},
        }
    },
}


class TestPolicyTabFlow:
    """Locks in the new condition-builder UX wiring: master toggle
    posts to the same feature-flag endpoint the Server-Settings tab
    uses, and the pending-list shows the right copy depending on
    whether the feature is on or off."""

    def test_master_toggle_change_posts_to_features_endpoint(
        self, settings_script: str
    ) -> None:
        """Clicking the master toggle on the Policies tab must POST
        ``{flags: {enable_tool_security_policies: true}}`` to
        ``/api/settings/features`` — same endpoint as the Server
        Settings checkbox. Without this the two surfaces would drift
        and the user couldn't trust the on-tab toggle to actually
        flip addon config."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "responses": [
                    _BOTH_POLICY_FLAGS_OFF_RESPONSE,  # 1-2: the two init reads
                    _BOTH_POLICY_FLAGS_OFF_RESPONSE,
                    _BOTH_POLICY_FLAGS_OFF_RESPONSE,  # 3: the explicit sync below
                    # 4: the save POST (sticks for the follow-up re-read;
                    # the applied echo then paints the confirmed state).
                    {
                        "status": 200,
                        "json": {
                            "applied": {"enable_tool_security_policies": True},
                            "restart_required": True,
                        },
                    },
                ]
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              await window.syncPolicyGlobalToggles();
              const cb = document.getElementById('policy-master-toggle');
              const pre = document.createElement('div');
              pre.id = '__master_pre_dispatch_probe';
              pre.dataset.disabled = String(cb.disabled);
              pre.dataset.indeterminate = String(cb.indeterminate);
              document.body.appendChild(pre);
              cb.checked = true;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 100));
            """,
        )
        _assert_clean_init(result)
        pre = re.search(r'<div[^>]*id="__master_pre_dispatch_probe"[^>]*>', result.dom)
        assert pre is not None, f"probe missing; dom tail: {result.dom[-1500:]}"
        assert 'data-disabled="false"' in pre.group(0), (
            f"switch must be editable before the synthetic click: {pre.group(0)}"
        )
        assert 'data-indeterminate="false"' in pre.group(0), pre.group(0)
        flag_posts = [
            f
            for f in result.fetches
            if f["method"] == "POST" and "/api/settings/features" in f["url"]
        ]
        assert len(flag_posts) >= 1, (
            f"expected POST to /api/settings/features; got {result.fetches}"
        )
        # The body is JSON-serialised — parse and assert structurally
        # rather than substring-matching, so a body like
        # ``{"flags":{"x":"enable_tool_security_policies"}}`` (which
        # would pass the loose match) doesn't false-positive.
        matched = False
        for f in flag_posts:
            raw = f.get("body", "")
            if not raw:
                continue
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            flags = body.get("flags") if isinstance(body, dict) else None
            if (
                isinstance(flags, dict)
                and flags.get("enable_tool_security_policies") is True
            ):
                matched = True
                break
        assert matched, (
            "expected POST body containing "
            f"{{'flags': {{'enable_tool_security_policies': True}}}}; "
            f"got {[f.get('body') for f in flag_posts]}"
        )

    def test_manage_tool_toggle_change_posts_to_features_endpoint(
        self, settings_script: str
    ) -> None:
        """The policy-editing-tool toggle must POST
        ``{flags: {enable_security_policy_tool: true}}`` to the same
        feature-flag endpoint the master toggle uses (#2148) — it is an
        ordinary feature flag, not a policy-document field."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "responses": [
                    _BOTH_POLICY_FLAGS_OFF_RESPONSE,  # 1-2: the two init reads
                    _BOTH_POLICY_FLAGS_OFF_RESPONSE,
                    _BOTH_POLICY_FLAGS_OFF_RESPONSE,  # 3: the explicit sync below
                    # 4: the save POST (sticks for the follow-up re-read;
                    # the applied echo then paints the confirmed state).
                    {
                        "status": 200,
                        "json": {
                            "applied": {"enable_security_policy_tool": True},
                            "restart_required": True,
                        },
                    },
                ]
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              await window.syncPolicyGlobalToggles();
              const cb = document.getElementById('policy-manage-tool-toggle');
              const pre = document.createElement('div');
              pre.id = '__manage_pre_dispatch_probe';
              pre.dataset.disabled = String(cb.disabled);
              pre.dataset.indeterminate = String(cb.indeterminate);
              document.body.appendChild(pre);
              cb.checked = true;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 100));
            """,
        )
        _assert_clean_init(result)
        pre = re.search(r'<div[^>]*id="__manage_pre_dispatch_probe"[^>]*>', result.dom)
        assert pre is not None, f"probe missing; dom tail: {result.dom[-1500:]}"
        assert 'data-disabled="false"' in pre.group(0), (
            f"switch must be editable before the synthetic click: {pre.group(0)}"
        )
        assert 'data-indeterminate="false"' in pre.group(0), pre.group(0)
        matched = False
        for f in result.fetches:
            if f["method"] != "POST" or "/api/settings/features" not in f["url"]:
                continue
            try:
                body = json.loads(f.get("body", ""))
            except (json.JSONDecodeError, TypeError):
                continue
            flags = body.get("flags") if isinstance(body, dict) else None
            if (
                isinstance(flags, dict)
                and flags.get("enable_security_policy_tool") is True
            ):
                matched = True
                break
        assert matched, (
            "expected POST body containing "
            "{'flags': {'enable_security_policy_tool': True}}; got "
            f"{[f.get('body') for f in result.fetches if f['method'] == 'POST']}"
        )

    def test_manage_tool_toggle_reverts_when_save_fails(
        self, settings_script: str
    ) -> None:
        """A failed save must snap the switch back to its previous value.
        Leaving it visually on would tell the operator that agents can edit
        the policies when the server still says they cannot (or vice versa)."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "responses": [
                    _OFF_FLAG_RESPONSE,  # 1-2: the two init reads
                    _OFF_FLAG_RESPONSE,
                    _OFF_FLAG_RESPONSE,  # 3: the explicit sync below
                    # 4: the save itself fails — the server still holds the
                    # previous value, so reverting IS correct here.
                    {"status": 500, "json": {"error": {"message": "nope"}}},
                ]
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              await window.syncPolicyGlobalToggles();
              const cb = document.getElementById('policy-manage-tool-toggle');
              cb.checked = true;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 100));
              const probe = document.createElement('div');
              probe.id = '__manage_tool_probe';
              probe.dataset.checked = String(cb.checked);
              probe.dataset.indeterminate = String(cb.indeterminate);
              probe.dataset.disabled = String(cb.disabled);
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'<div[^>]*id="__manage_tool_probe"[^>]*>', result.dom)
        assert m is not None, f"probe missing; dom tail: {result.dom[-1500:]}"
        assert 'data-checked="false"' in m.group(0), (
            f"failed save must revert the switch to its previous value: {m.group(0)}"
        )
        # Reverted must also mean retryable: a rejected save leaves the
        # server's value known, so the switch stays editable, not unknown.
        assert 'data-indeterminate="false"' in m.group(0), m.group(0)
        assert 'data-disabled="false"' in m.group(0), m.group(0)

    def test_unconfirmed_save_keeps_the_applied_value(
        self, settings_script: str
    ) -> None:
        """Save succeeded, follow-up read failed: the POST echoes `applied`
        — the server stating what it persisted — so the switch shows that
        instead of reverting to a pre-flip state the server no longer has."""
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map={
                **DEFAULT_FETCHES,
                "/api/settings/features": {
                    "responses": [
                        _OFF_FLAG_RESPONSE,  # 1-2: the two init reads
                        _OFF_FLAG_RESPONSE,
                        _OFF_FLAG_RESPONSE,  # 3: the explicit sync below
                        # 4: the save POST, echoing what it wrote
                        {
                            "status": 200,
                            "json": {
                                "applied": {"enable_security_policy_tool": True},
                                "restart_required": True,
                            },
                        },
                        # 5: the confirming re-read fails
                        {"status": 503, "json": {}},
                    ]
                },
            },
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              await window.syncPolicyGlobalToggles();
              const cb = document.getElementById('policy-manage-tool-toggle');
              cb.checked = true;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 100));
              const probe = document.createElement('div');
              probe.id = '__applied_probe';
              probe.dataset.checked = String(cb.checked);
              probe.dataset.indeterminate = String(cb.indeterminate);
              probe.dataset.disabled = String(cb.disabled);
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'<div[^>]*id="__applied_probe"[^>]*>', result.dom)
        assert m is not None, f"probe missing; dom tail: {result.dom[-1500:]}"
        tag = m.group(0)
        assert 'data-checked="true"' in tag, (
            f"the echoed applied value must survive a failed re-read: {tag}"
        )
        assert 'data-indeterminate="false"' in tag, tag
        assert 'data-disabled="false"' in tag, tag

    def test_unconfirmed_save_without_echo_goes_unknown(
        self, settings_script: str
    ) -> None:
        """Save succeeded but neither the response nor the re-read says
        what the server now holds: the switch goes to the unknown
        treatment. Reverting to the pre-flip value would assert a state
        nobody verified."""
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map={
                **DEFAULT_FETCHES,
                "/api/settings/features": {
                    "responses": [
                        _OFF_FLAG_RESPONSE,  # 1-2: the two init reads
                        _OFF_FLAG_RESPONSE,
                        _OFF_FLAG_RESPONSE,  # 3: the explicit sync below
                        # 4: the save — 200 with no `applied` echo
                        # (truncated body).
                        {"status": 200, "json": {"restart_required": True}},
                        {"status": 503, "json": {}},  # 5: re-read fails
                    ]
                },
            },
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              await window.syncPolicyGlobalToggles();
              const cb = document.getElementById('policy-manage-tool-toggle');
              cb.checked = true;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 100));
              const notice = document.getElementById('policyUnknownNotice');
              const probe = document.createElement('div');
              probe.id = '__unconfirmed_probe';
              probe.dataset.blocked = String(cb.indeterminate && cb.disabled);
              probe.dataset.shown = String(
                !!notice && notice.classList.contains('show'));
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'<div[^>]*id="__unconfirmed_probe"[^>]*>', result.dom)
        assert m is not None, f"probe missing; dom tail: {result.dom[-1500:]}"
        tag = m.group(0)
        assert 'data-blocked="true"' in tag, (
            f"an unconfirmed save must leave the switch unknown, not reverted "
            f"and editable: {tag}"
        )
        assert 'data-shown="true"' in tag, f"unknown notice must show: {tag}"

    def test_missing_flag_entry_is_unknown_not_off(self, settings_script: str) -> None:
        """Known is per FLAG, not per response. A 200 that omits one flag
        entry says nothing about that flag; rendering it as an editable
        "off" invites a save that overwrites an enabled server value (a
        server build older than this UI, or an overlay dropping the key).
        The flag that IS present must still render normally."""
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map={
                **DEFAULT_FETCHES,
                "/api/settings/features": {
                    "status": 200,
                    "json": {
                        "flags": {
                            # Master present and on; the policy-tool flag is
                            # absent from the payload entirely.
                            "enable_tool_security_policies": {
                                "value": True,
                                "origin": "file",
                                "editable": True,
                            }
                        }
                    },
                },
            },
            invoke="""
              await window.syncPolicyGlobalToggles();
              const master = document.getElementById('policy-master-toggle');
              const tool = document.getElementById('policy-manage-tool-toggle');
              const notice = document.getElementById('policyUnknownNotice');
              const probe = document.createElement('div');
              probe.id = '__missing_flag_probe';
              probe.dataset.masterChecked = String(master.checked);
              probe.dataset.masterBlocked = String(
                master.indeterminate || master.disabled);
              probe.dataset.toolBlocked = String(
                tool.indeterminate && tool.disabled);
              probe.dataset.shown = String(
                !!notice && notice.classList.contains('show'));
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'<div[^>]*id="__missing_flag_probe"[^>]*>', result.dom)
        assert m is not None, f"probe missing; dom tail: {result.dom[-1500:]}"
        tag = m.group(0)
        assert 'data-master-checked="true"' in tag, (
            f"the flag the payload DID carry must render from its value: {tag}"
        )
        assert 'data-master-blocked="false"' in tag, (
            f"a present flag must stay editable: {tag}"
        )
        assert 'data-tool-blocked="true"' in tag, (
            f"an omitted flag entry must render unknown, not off-and-editable: {tag}"
        )
        assert 'data-shown="true"' in tag, (
            f"the notice must show when EITHER policy flag is unknown: {tag}"
        )

    def test_env_pinned_flag_locks_the_switch(self, settings_script: str) -> None:
        """editable:false means every save is rejected server-side, so the
        switch must lock and name the env var instead of looking usable
        (the treatment the generated Server Settings rows already get)."""
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map={
                **DEFAULT_FETCHES,
                "/api/settings/features": {
                    "status": 200,
                    "json": {
                        "is_addon": False,
                        "flags": {
                            "enable_security_policy_tool": {
                                "value": True,
                                "origin": "env",
                                "editable": False,
                                "env_var": "ENABLE_SECURITY_POLICY_TOOL",
                            }
                        },
                    },
                },
            },
            invoke="""
              await window.syncPolicyGlobalToggles();
              const cb = document.getElementById('policy-manage-tool-toggle');
              const probe = document.createElement('div');
              probe.id = '__pinned_probe';
              probe.dataset.checked = String(cb.checked);
              probe.dataset.disabled = String(cb.disabled);
              probe.dataset.indeterminate = String(cb.indeterminate);
              // Booleans, not the note's HTML: markup inside a data-
              // attribute closes the probe tag early for the reader below.
              const note = document.getElementById('policy-manage-tool-locked');
              probe.dataset.noteNamesVar = String(
                note.innerHTML.includes('ENABLE_SECURITY_POLICY_TOOL'));
              probe.dataset.noteShown = String(note.style.display !== 'none');
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'<div[^>]*id="__pinned_probe"[^>]*>', result.dom)
        assert m is not None, f"probe missing; dom tail: {result.dom[-1500:]}"
        tag = m.group(0)
        assert 'data-disabled="true"' in tag, (
            f"an env-pinned flag's switch must be disabled: {tag}"
        )
        # Known-but-locked is not the same as unknown: the value IS known.
        assert 'data-indeterminate="false"' in tag, tag
        assert 'data-checked="true"' in tag, tag
        assert 'data-note-shown="true"' in tag, (
            f"the locked note must be visible: {tag}"
        )
        assert 'data-note-names-var="true"' in tag, (
            f"the locked note must name the env var that pins it: {tag}"
        )

    def test_policy_toggles_unknown_when_features_fetch_fails(
        self, settings_script: str
    ) -> None:
        """When /api/settings/features fails, both Policies-tab switches are
        unknown: the notice shows and each switch goes indeterminate +
        disabled rather than rendering a confident "off" (#2148). Mirrors the
        Tools-tab read-only unknown-state treatment."""
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map={
                **DEFAULT_FETCHES,
                "/api/settings/features": {"status": 503, "json": {}},
            },
            invoke="""
              await window.syncPolicyGlobalToggles();
              const notice = document.getElementById('policyUnknownNotice');
              const master = document.getElementById('policy-master-toggle');
              const tool = document.getElementById('policy-manage-tool-toggle');
              const probe = document.createElement('div');
              probe.id = '__policy_unknown_probe';
              probe.dataset.shown = String(
                !!notice && notice.classList.contains('show'));
              probe.dataset.masterBlocked = String(
                master.indeterminate && master.disabled);
              probe.dataset.toolBlocked = String(
                tool.indeterminate && tool.disabled);
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'<div[^>]*id="__policy_unknown_probe"[^>]*>', result.dom)
        assert m is not None, f"probe missing; dom tail: {result.dom[-1500:]}"
        tag = m.group(0)
        assert 'data-shown="true"' in tag, f"unknown notice must show: {tag}"
        assert 'data-master-blocked="true"' in tag, (
            f"master switch must be indeterminate + disabled while unknown: {tag}"
        )
        assert 'data-tool-blocked="true"' in tag, (
            f"policy-tool switch must be indeterminate + disabled while unknown: {tag}"
        )

    def test_gate_toggle_preserves_conditional_rules(
        self, settings_script: str
    ) -> None:
        """Un-gating a tool must remove only the bare unconditional rule, not
        a predicate-bearing rule the user authored in the policy editor. The
        gate toggle keys on the ``when == []`` rule; conditional rules survive."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/policy/config": {
                "status": 200,
                "json": {
                    "wait_seconds": 60,
                    "approval_ttl_minutes": 5,
                    "version": 3,
                    "rules": [
                        {
                            "tool_name": "ha_call_service",
                            "when": [
                                {"path": "args.domain", "op": "eq", "value": "lock"}
                            ],
                            "remember_minutes": 0,
                        }
                    ],
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="await window.syncPolicyRule('ha_call_service', false);",
        )
        _assert_clean_init(result)
        puts = [
            f
            for f in result.fetches
            if f["method"] == "PUT" and "/api/policy/config" in f["url"]
        ]
        assert len(puts) == 1, (
            f"expected one PUT to policy config; got {result.fetches}"
        )
        body = json.loads(puts[0]["body"])
        rules = body.get("rules", [])
        # The conditional rule survives un-gating; nothing was wiped.
        assert len(rules) == 1, f"conditional rule was wrongly removed: {rules}"
        assert rules[0]["when"], "expected the predicate-bearing rule to remain"

    def test_gate_toggle_on_adds_bare_rule_beside_conditional(
        self, settings_script: str
    ) -> None:
        """Enable direction: turning the Tools-tab gate ON when a conditional
        rule already exists must ADD the bare unconditional rule (not silently
        no-op), so the tool is gated for every call."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/policy/config": {
                "status": 200,
                "json": {
                    "wait_seconds": 60,
                    "approval_ttl_minutes": 5,
                    "version": 2,
                    "rules": [
                        {
                            "tool_name": "ha_call_service",
                            "when": [
                                {"path": "args.domain", "op": "eq", "value": "lock"}
                            ],
                            "remember_minutes": 0,
                        }
                    ],
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="await window.syncPolicyRule('ha_call_service', true);",
        )
        _assert_clean_init(result)
        puts = [
            f
            for f in result.fetches
            if f["method"] == "PUT" and "/api/policy/config" in f["url"]
        ]
        assert len(puts) == 1
        rules = json.loads(puts[0]["body"])["rules"]
        assert len(rules) == 2
        assert any(r["tool_name"] == "ha_call_service" and not r["when"] for r in rules)
        assert any(r["when"] for r in rules)  # conditional rule preserved

    def test_save_rule_expands_conditions_into_separate_rules(
        self, settings_script: str
    ) -> None:
        """Each condition on a card persists as its OWN rule so they OR at
        evaluation (gate if ANY matches), not one AND-ed rule."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/policy/config": {
                "status": 200,
                "json": {
                    "wait_seconds": 60,
                    "approval_ttl_minutes": 5,
                    "version": 0,
                    "rules": [],
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await window.savePolicyRule('ha_call_service', {
                tool_name: 'ha_call_service',
                conditions: [
                  [{path: 'args.domain', op: 'eq', value: 'lock'}],
                  [{path: 'args.domain', op: 'eq', value: 'alarm_control_panel'}]
                ],
                remember_minutes: 0
              });
            """,
        )
        _assert_clean_init(result)
        puts = [
            f
            for f in result.fetches
            if f["method"] == "PUT" and "/api/policy/config" in f["url"]
        ]
        assert len(puts) == 1
        rules = json.loads(puts[0]["body"])["rules"]
        assert len(rules) == 2, f"expected one rule per condition; got {rules}"
        assert all(
            r["tool_name"] == "ha_call_service" and len(r["when"]) == 1 for r in rules
        )

    def test_save_rule_preserves_multi_predicate_condition(
        self, settings_script: str
    ) -> None:
        """A condition with AND-ed sub-parameters (multi-predicate rule) must
        round-trip as ONE rule — not be flattened into separate OR rules."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/policy/config": {
                "status": 200,
                "json": {
                    "wait_seconds": 60,
                    "approval_ttl_minutes": 5,
                    "version": 0,
                    "rules": [],
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await window.savePolicyRule('ha_call_service', {
                tool_name: 'ha_call_service',
                conditions: [
                  [
                    {path: 'args.domain', op: 'eq', value: 'lock'},
                    {path: 'args.service', op: 'eq', value: 'unlock'}
                  ]
                ],
                remember_minutes: 0
              });
            """,
        )
        _assert_clean_init(result)
        puts = [
            f
            for f in result.fetches
            if f["method"] == "PUT" and "/api/policy/config" in f["url"]
        ]
        assert len(puts) == 1
        rules = json.loads(puts[0]["body"])["rules"]
        assert len(rules) == 1, f"multi-predicate condition was split: {rules}"
        assert len(rules[0]["when"]) == 2

    def test_save_rule_preserves_rule_order(self, settings_script: str) -> None:
        """Saving a card must replace the tool's rules IN PLACE — rule order
        is behaviorally significant (find_matching_rule takes the FIRST
        match's remember_minutes), so a tool rule must not slide behind a
        wildcard rule on edit (Codex #1993 round 3)."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/policy/config": {
                "status": 200,
                "json": {
                    "wait_seconds": 60,
                    "approval_ttl_minutes": 5,
                    "version": 1,
                    "rules": [
                        {
                            "tool_name": "ha_call_service",
                            "when": [
                                {"path": "args.domain", "op": "eq", "value": "lock"}
                            ],
                            "remember_minutes": 5,
                        },
                        {"tool_name": "*", "when": [], "remember_minutes": 60},
                    ],
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await window.savePolicyRule('ha_call_service', {
                tool_name: 'ha_call_service',
                conditions: [[{path: 'args.domain', op: 'eq', value: 'lock'}]],
                remember_minutes: 5
              });
            """,
        )
        _assert_clean_init(result)
        puts = [
            f
            for f in result.fetches
            if f["method"] == "PUT" and "/api/policy/config" in f["url"]
        ]
        assert len(puts) == 1
        rules = json.loads(puts[0]["body"])["rules"]
        assert [r["tool_name"] for r in rules] == ["ha_call_service", "*"], (
            f"tool rule moved behind the wildcard: {rules}"
        )

    def test_new_tool_rule_inserts_before_wildcard(self, settings_script: str) -> None:
        """A tool with NO existing rule must insert BEFORE a wildcard rule,
        not append after it — first-match would otherwise resolve the tool's
        calls to the wildcard's remember_minutes (Patch76 review: the branch
        the in-place reorder fix doesn't cover)."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/policy/config": {
                "status": 200,
                "json": {
                    "wait_seconds": 60,
                    "approval_ttl_minutes": 5,
                    "version": 1,
                    "rules": [
                        {"tool_name": "ha_restart", "when": [], "remember_minutes": 0},
                        {"tool_name": "*", "when": [], "remember_minutes": 60},
                    ],
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await window.savePolicyRule('ha_call_service', {
                tool_name: 'ha_call_service',
                conditions: [[{path: 'args.domain', op: 'eq', value: 'lock'}]],
                remember_minutes: 5
              });
            """,
        )
        _assert_clean_init(result)
        puts = [
            f
            for f in result.fetches
            if f["method"] == "PUT" and "/api/policy/config" in f["url"]
        ]
        assert len(puts) == 1
        rules = json.loads(puts[0]["body"])["rules"]
        assert [r["tool_name"] for r in rules] == [
            "ha_restart",
            "ha_call_service",
            "*",
        ], f"new tool rule appended after the wildcard: {rules}"

    def test_gate_toggle_on_inserts_before_wildcard(self, settings_script: str) -> None:
        """The Tools-tab gate toggle has the same new-rule case: a fresh bare
        gate must land before an existing wildcard rule."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/policy/config": {
                "status": 200,
                "json": {
                    "wait_seconds": 60,
                    "approval_ttl_minutes": 5,
                    "version": 1,
                    "rules": [
                        {"tool_name": "*", "when": [], "remember_minutes": 60},
                    ],
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="await window.syncPolicyRule('ha_call_service', true);",
        )
        _assert_clean_init(result)
        puts = [
            f
            for f in result.fetches
            if f["method"] == "PUT" and "/api/policy/config" in f["url"]
        ]
        assert len(puts) == 1
        rules = json.loads(puts[0]["body"])["rules"]
        assert [r["tool_name"] for r in rules] == ["ha_call_service", "*"], (
            f"bare gate appended after the wildcard: {rules}"
        )

    def test_load_collapses_tool_rules_into_one_card(
        self, settings_script: str
    ) -> None:
        """Read side of renderPolicyCards: a tool's on-disk rules collapse into
        ONE card, one condition row per rule in order. Only single-predicate
        rows get the edit button; the hand-authored multi-predicate row joins
        its predicates with ' AND ' and offers no edit. The single remember
        input shows the MAX across the rules (60), so a save that never touches
        it can't silently shorten a longer window."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/policy/config": {
                "status": 200,
                "json": {
                    "wait_seconds": 60,
                    "approval_ttl_minutes": 5,
                    "version": 4,
                    "rules": [
                        {
                            "tool_name": "ha_call_service",
                            "when": [
                                {"path": "args.domain", "op": "eq", "value": "lock"}
                            ],
                            "remember_minutes": 1,
                        },
                        {
                            "tool_name": "ha_call_service",
                            "when": [
                                {"path": "args.service", "op": "eq", "value": "unlock"}
                            ],
                            "remember_minutes": 60,
                        },
                        {
                            "tool_name": "ha_call_service",
                            "when": [
                                {"path": "args.domain", "op": "eq", "value": "lock"},
                                {"path": "args.service", "op": "eq", "value": "unlock"},
                            ],
                            "remember_minutes": 0,
                        },
                    ],
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await window.policyLoadConfig();
              const cards = document.querySelectorAll('.policy-rule-card');
              document.body.setAttribute('data-card-count', String(cards.length));
              const card = cards[0];
              const rows = card
                ? Array.from(card.querySelectorAll('.policy-predicate-row'))
                : [];
              document.body.setAttribute('data-row-count', String(rows.length));
              const codeText = (r) =>
                (r && r.querySelector('code')) ? r.querySelector('code').textContent : '';
              const hasEdit = (r) =>
                String(!!(r && r.querySelector('.policy-edit-predicate')));
              document.body.setAttribute(
                'data-multi-has-and', String(codeText(rows[2]).includes(' AND ')));
              document.body.setAttribute('data-multi-has-edit', hasEdit(rows[2]));
              document.body.setAttribute('data-single0-has-edit', hasEdit(rows[0]));
              document.body.setAttribute('data-single1-has-edit', hasEdit(rows[1]));
              const rem = card ? card.querySelector('.policy-remember-minutes') : null;
              document.body.setAttribute('data-remember', rem ? String(rem.value) : '');
            """,
        )
        _assert_clean_init(result)
        assert _probe(result, "card-count") == "1", (
            "tool rules did not collapse to one card"
        )
        assert _probe(result, "row-count") == "3", "expected one condition row per rule"
        assert _probe(result, "multi-has-and") == "true", (
            "multi-predicate row missing ' AND ' join"
        )
        assert _probe(result, "multi-has-edit") == "false", (
            "multi-predicate row should not be editable"
        )
        assert _probe(result, "single0-has-edit") == "true"
        assert _probe(result, "single1-has-edit") == "true"
        assert _probe(result, "remember") == "60", (
            "remember input should show the max across rules"
        )

    def test_pending_list_shows_off_message_when_feature_disabled(
        self, settings_script: str
    ) -> None:
        """When the addon flag is off, /api/policy/pending 503s. The
        UI should tell the user the feature is just turned off — NOT
        the misleading "sidecar / unavailable" text the earlier code
        showed. This catches regressions where the new copy gets
        clobbered back to the generic message."""
        fetches = {
            **DEFAULT_FETCHES,
            # Feature flag explicitly disabled
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_tool_security_policies": {"value": False},
                    },
                },
            },
            # Stub policy config endpoints so policyLoadConfig doesn't 500
            "/api/policy/config": {
                "status": 200,
                "json": {"wait_seconds": 60, "approval_ttl_minutes": 5, "rules": []},
            },
            "/api/policy/pending": {
                "status": 503,
                "json": {"error": "irrelevant when flag is off"},
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await window.policyLoadConfig();
              await window.policyLoadPending();
            """,
        )
        _assert_clean_init(result)
        # `dom` is the full final-state document snapshot; grep the
        # pending-list region for the new off-state copy.
        assert "turned off" in result.dom.lower(), (
            f"expected 'turned off' in pending-list snapshot; "
            f"dom contains: {result.dom[-2000:]}"
        )

    def test_pending_list_shows_server_message_when_feature_on_but_503(
        self, settings_script: str
    ) -> None:
        """Feature is on but the queue is unreachable (sidecar mode or
        ImportError at startup). The server's 503 message should
        propagate verbatim — it names which cause applied — instead of
        the generic "feature off" text."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_tool_security_policies": {"value": True},
                    },
                },
            },
            "/api/policy/config": {
                "status": 200,
                "json": {"wait_seconds": 60, "approval_ttl_minutes": 5, "rules": []},
            },
            "/api/policy/pending": {
                "status": 503,
                "json": {"error": _server_503_body()},
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await window.policyLoadConfig();
              await window.policyLoadPending();
            """,
        )
        _assert_clean_init(result)
        assert _server_503_body() in result.dom, (
            f"expected the server's 503 paragraph in the pending-list "
            f"snapshot; dom contains: {result.dom[-2000:]}"
        )


# ---------------------------------------------------------------------------
# "Already decided" copy on the 409 path
# ---------------------------------------------------------------------------


def _localized_policy_dom(locale: str) -> str:
    """Policy-tab DOM carrying ``locale``'s catalog as the i18n payload."""
    from ha_mcp.settings_ui._i18n import build_payload, serialize_payload

    payload = serialize_payload(build_payload(locale))
    element = f'<script id="ha-mcp-i18n" type="application/json">{payload}</script>'
    return _policy_panel_dom().replace("</body>", element + "</body>")


def _already_decided_cases() -> list[tuple[str, str]]:
    """Every (non-English locale, decided outcome) pair the 409 can produce.

    Derived from ``Decision`` rather than listed, so a new outcome arrives
    here as new cases instead of as untested copy.
    """
    from ha_mcp.policy.approval_queue import Decision
    from ha_mcp.settings_ui._i18n import CATALOGS, DEFAULT_LOCALE
    from ha_mcp.settings_ui._locale_policy import BEST_EFFORT_LOCALES

    locales = sorted(set(CATALOGS) - {DEFAULT_LOCALE} - BEST_EFFORT_LOCALES)
    outcomes = sorted(set(get_args(Decision)) - {"pending"})
    # Both sides are discovered, so either emptying out collects zero cases
    # and the class below passes without driving the handler once.
    assert locales, (
        "no non-English catalog is registered — every case here derives from "
        "one, so this file would pin the 409 copy in no language at all"
    )
    assert outcomes, (
        "Decision no longer names any decided outcome — the 409 branch these "
        "cases drive cannot be reached, so either the branch or this is dead"
    )
    return [(locale, outcome) for locale in locales for outcome in outcomes]


def test_best_effort_locales_are_outside_hard_js_copy_cases() -> None:
    from ha_mcp.settings_ui._locale_policy import BEST_EFFORT_LOCALES

    assert BEST_EFFORT_LOCALES.isdisjoint(
        locale for locale, _ in _already_decided_cases()
    )


class TestAlreadyDecidedCopy:
    """The 409 alert must read as one translated sentence.

    ``current_decision`` is a backend enum. Interpolating it raw put an
    English word inside otherwise translated copy — Italian read ``Questa
    approvazione è già stata approved``. It is resolved through the catalog
    now, and this drives the real handler and compares the whole sentence
    the user is shown against the locale's own copy. Asserting on the
    rendered alert covers the ways source-text matching cannot — a catalog
    whose value is still the English placeholder, a key the concatenation
    builds but no catalog holds, a resolved word no message consumes.
    """

    @pytest.mark.parametrize(("locale", "outcome"), _already_decided_cases())
    def test_alert_shows_the_catalog_word_and_never_the_enum(
        self, settings_script: str, locale: str, outcome: str
    ) -> None:
        from ha_mcp.settings_ui._i18n import CATALOGS

        fetches = {
            **DEFAULT_FETCHES,
            "/api/policy/approve": {
                "status": 409,
                "json": {"error": "already decided", "current_decision": outcome},
            },
            "/api/policy/pending": {"status": 200, "json": {"pending": []}},
        }
        result = run_script(
            settings_script,
            initial_html=_localized_policy_dom(locale),
            fetch_map=fetches,
            invoke="await window.policyDecide('tok-1', 'approve');",
        )
        _assert_clean_init(result)

        assert len(result.alerts) == 1, (
            f"expected the single 409 alert; got {result.alerts}"
        )
        alert = result.alerts[0]
        messages = CATALOGS[locale]["messages"]
        word = messages[f"policies.pending.decision.{outcome}"]
        # Whole sentence, not containment: the host a containment check
        # searches is that same catalog value with the word already
        # substituted, so it cannot fail on catalog content. Lose the host to
        # the English fallback and "This approval was already approvata"
        # satisfies both a containment and a not-the-enum check.
        expected = messages["policies.pending.already_decided"].replace(
            "{decision}", word
        )
        assert alert == expected, (
            f"{locale}: the alert reads {alert!r}, not the {locale} sentence "
            f"{expected!r}. Either the host sentence fell back to English or "
            f"the {outcome!r} slot is not the catalog word — both put English "
            "inside translated copy, which is the bug this path prevents."
        )


class TestDecideUnavailableCopy:
    """A 503 on the decide path must not splice English into translated copy.

    ``/api/policy/{approve,deny}`` answers 503 with a fixed English paragraph
    when live approvals are not active. Folding that body into
    ``policies.pending.action_failed`` put that whole paragraph inside an
    Italian clause. The path now answers the way ``policyLoadPending``
    already does: the translated off-message when the flag is known off, and
    otherwise the server's diagnostic on its own, because it names which of the
    remaining causes applied and the generic line cannot.
    """

    def _decide_under_503(
        self,
        settings_script: str,
        *,
        enabled: bool,
        approve_response: dict[str, object] | None = None,
    ) -> HarnessResult:
        """Drive an Italian UI through a 503 decide with the flag at ``enabled``.

        ``loadPolicyState`` is invoked explicitly rather than left to page
        init, so the branch reads a settled ``policyState`` instead of racing
        the init fetch. ``approve_response`` overrides the 503 the decide
        call receives, for the case where it carries no JSON at all.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {"enable_tool_security_policies": {"value": enabled}}
                },
            },
            "/api/policy/config": {
                "status": 200,
                "json": {"wait_seconds": 60, "approval_ttl_minutes": 5, "rules": []},
            },
            "/api/policy/approve": approve_response
            or {"status": 503, "json": {"error": _server_503_body()}},
            "/api/policy/pending": {"status": 200, "json": {"pending": []}},
        }
        return run_script(
            settings_script,
            initial_html=_localized_policy_dom("it"),
            fetch_map=fetches,
            invoke=(
                "await window.loadPolicyState();"
                "await window.policyDecide('tok-1', 'approve');"
            ),
        )

    def test_flag_known_off_shows_the_translated_off_message(
        self, settings_script: str
    ) -> None:
        from ha_mcp.settings_ui._i18n import CATALOGS

        result = self._decide_under_503(settings_script, enabled=False)
        _assert_clean_init(result)

        expected = CATALOGS["it"]["messages"]["policies.pending.disabled"]
        assert result.alerts == [expected], (
            f"expected the Italian off-message {expected!r}; got {result.alerts}. "
            "A user who simply turned the feature off is being told to read the "
            "add-on log instead."
        )

    def test_flag_on_shows_the_server_diagnostic_on_its_own(
        self, settings_script: str
    ) -> None:
        result = self._decide_under_503(settings_script, enabled=True)
        _assert_clean_init(result)

        assert result.alerts == [_server_503_body()], (
            f"expected the server paragraph alone; got {result.alerts}. Equality "
            "is the point: anything longer means it was spliced into a "
            "translated sentence, which is the mixed-language render this fixes."
        )

    def test_503_without_a_json_body_shows_the_translated_line(
        self, settings_script: str
    ) -> None:
        """No JSON body means no server message, so the catalog line is all
        there is — and it has to be the translated one.

        Our own routes always send JSON, but an ingress or reverse proxy in
        front of them does not, and that is the deployment this branch exists
        for. A stand-in error synthesised from the status line looks like a
        server message to the branch, so it would shadow the catalog line and
        alert a bare ``HTTP 503`` in every language.
        """
        from ha_mcp.settings_ui._i18n import CATALOGS

        result = self._decide_under_503(
            settings_script,
            enabled=True,
            approve_response={
                "status": 503,
                "body": "<html><body>503 Service Unavailable</body></html>",
            },
        )
        _assert_clean_init(result)

        expected = CATALOGS["it"]["messages"]["policies.pending.unavailable"]
        assert result.alerts == [expected], (
            f"expected the Italian unavailable line {expected!r}; got "
            f"{result.alerts}. Anything else here is untranslated text — or the "
            "proxy's HTML — put in front of an Italian user."
        )


# ---------------------------------------------------------------------------
# Env-pinned tool rows
# ---------------------------------------------------------------------------


class TestEnvPinnedToolRows:
    """Env-pinned tools (DISABLED_TOOLS / PINNED_TOOLS) must render with
    disabled inputs and a banner naming the env var so the user knows
    why the toggles are locked.
    """

    def test_env_pinned_disabled_tool_renders_locked_with_env_var_label(
        self, settings_script: str
    ) -> None:
        """A tool listed in DISABLED_TOOLS renders with all inputs disabled
        and shows 'env-pinned via DISABLED_TOOLS' in the row.

        The Tools tab must surface the env-pinned state so the user is not
        confused by a toggle that silently refuses to save.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [
                        {
                            "name": "ha_foo",
                            "title": "Foo",
                            "primary_tag": "Utilities",
                            "tags": ["Utilities"],
                            "description": "Test tool",
                            "annotations": {},
                        }
                    ],
                    "states": {"ha_foo": "disabled"},
                    "env_pinned": {"ha_foo": "disabled"},
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)

        # The row must carry the env-pinned class.
        assert "env-pinned" in result.dom, (
            f"expected .env-pinned class on locked tool row; "
            f"dom tail: {result.dom[-3000:]}"
        )
        # The banner must name the env var.
        assert "DISABLED_TOOLS" in result.dom, (
            f"expected 'DISABLED_TOOLS' in env-pinned banner; "
            f"dom tail: {result.dom[-3000:]}"
        )
        # Specifically: the enabled-toggle input for ha_foo must carry
        # the HTML `disabled` attribute. Coarse `"disabled" in result.dom`
        # would also pass on CSS class names elsewhere in the page; pin
        # the regex to the actual input element.

        enabled_input_match = re.search(
            r'<input[^>]*data-field="enabled"[^>]*>',
            result.dom,
        )
        assert enabled_input_match is not None, (
            "expected enabled-toggle <input data-field='enabled'> in DOM"
        )
        assert " disabled" in enabled_input_match.group(0), (
            f"expected disabled attribute on enabled-toggle input; got: "
            f"{enabled_input_match.group(0)}"
        )

    def test_env_pinned_pinned_tool_renders_locked_with_env_var_label(
        self, settings_script: str
    ) -> None:
        """A tool listed in PINNED_TOOLS renders with toggles disabled
        and shows 'env-pinned via PINNED_TOOLS' in the row.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [
                        {
                            "name": "ha_bar",
                            "title": "Bar",
                            "primary_tag": "Utilities",
                            "tags": ["Utilities"],
                            "description": "Another test tool",
                            "annotations": {},
                        }
                    ],
                    "states": {},
                    "env_pinned": {"ha_bar": "pinned"},
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)

        assert "env-pinned" in result.dom, (
            f"expected .env-pinned class on pinned-via-env tool row; "
            f"dom tail: {result.dom[-3000:]}"
        )
        assert "PINNED_TOOLS" in result.dom, (
            f"expected 'PINNED_TOOLS' in env-pinned banner; "
            f"dom tail: {result.dom[-3000:]}"
        )
        # Pinned-toggle input for ha_bar must be disabled.

        pin_input_match = re.search(
            r'<input[^>]*data-field="pinned"[^>]*>',
            result.dom,
        )
        assert pin_input_match is not None, (
            "expected pin-toggle <input data-field='pinned'> in DOM"
        )
        assert " disabled" in pin_input_match.group(0), (
            f"expected disabled attribute on pin-toggle input; got: "
            f"{pin_input_match.group(0)}"
        )


class TestGroupMasterToggle:
    """The per-group master switch must reflect the group's real enabled
    state.

    A group whose tools are all mandatory (always-on, non-toggleable) has an
    empty ``toggleable`` set, so the pre-fix ``anyEnabled`` computation was
    always false and the switch rendered unchecked+disabled — reading as "the
    whole group is off" while the count next to it said "N/N enabled". The
    Search & Discovery group (ha_search / ha_get_overview / ha_get_state, all
    mandatory) hit this. The master switch now shows the group's real state.
    """

    @staticmethod
    def _tools_fetch(
        tools: list[dict],
        states: dict[str, str],
        env_pinned: dict[str, str] | None = None,
    ) -> dict:
        payload = {"tools": tools, "states": states}
        if env_pinned is not None:
            payload["env_pinned"] = env_pinned
        return {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {"status": 200, "json": payload},
        }

    @staticmethod
    def _master_input(dom: str, tag: str) -> str:
        m = re.search(rf'<input[^>]*name="tool-group:{re.escape(tag)}"[^>]*>', dom)
        assert m is not None, (
            f"group-master input for {tag!r} not rendered; dom tail: {dom[-2000:]}"
        )
        return m.group(0)

    def test_all_mandatory_group_master_is_checked_and_disabled(
        self, settings_script: str
    ) -> None:
        """The reported bug: an all-mandatory group showed the master switch
        OFF (unchecked) even though every tool is enabled and cannot be
        disabled. It must render checked (on) AND disabled (unchangeable).
        """
        tools = [
            {
                "name": name,
                "title": name,
                "primary_tag": "Search",
                "tags": ["Search"],
                "description": "d",
                "annotations": {"readOnlyHint": True},
            }
            # All three are members of MANDATORY_TOOLS.
            for name in ("ha_search", "ha_get_overview", "ha_get_state")
        ]
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._tools_fetch(tools, {}),
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        tag_html = self._master_input(result.dom, "Search")
        assert "checked" in tag_html, (
            f"all-mandatory group master must be checked (group is fully "
            f"enabled); got: {tag_html}"
        )
        assert " disabled" in tag_html, (
            f"all-mandatory group master must be disabled (nothing to flip); "
            f"got: {tag_html}"
        )

    def test_bps_locked_tool_row_renders_locked_with_note(
        self, settings_script: str
    ) -> None:
        """#1886: a tool named in ``bps_locked_tools`` renders like a
        mandatory tool — enabled checkbox checked AND disabled — plus a
        note naming strict best-practices mode as the lock to lift."""
        tools = [
            {
                "name": "ha_get_skill_guide",
                "title": "Skill Guide",
                "primary_tag": "Docs",
                "tags": ["Docs"],
                "description": "d",
                "annotations": {"readOnlyHint": True},
            }
        ]
        fetch_map = self._tools_fetch(tools, {})
        fetch_map["/api/settings/tools"]["json"]["bps_locked_tools"] = [
            "ha_get_skill_guide"
        ]
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetch_map,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        m = re.search(
            r'<input[^>]*name="tool:ha_get_skill_guide:enabled"[^>]*>',
            result.dom,
        )
        assert m is not None, (
            f"skill-guide row not rendered; dom tail: {result.dom[-2000:]}"
        )
        assert "checked" in m.group(0), (
            f"bps-locked tool must render enabled; got: {m.group(0)}"
        )
        assert " disabled" in m.group(0), (
            f"bps-locked tool's enabled toggle must be locked; got: {m.group(0)}"
        )
        assert "Strict best-practices mode" in result.dom, (
            "bps-locked row must carry the note naming strict mode"
        )

    def test_bps_unlocked_tool_row_stays_editable(self, settings_script: str) -> None:
        """With ``bps_locked_tools`` empty (strict mode off), the skill
        guide renders as a normal editable row — no lock, no note."""
        tools = [
            {
                "name": "ha_get_skill_guide",
                "title": "Skill Guide",
                "primary_tag": "Docs",
                "tags": ["Docs"],
                "description": "d",
                "annotations": {"readOnlyHint": True},
            }
        ]
        fetch_map = self._tools_fetch(tools, {})
        fetch_map["/api/settings/tools"]["json"]["bps_locked_tools"] = []
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetch_map,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        m = re.search(
            r'<input[^>]*name="tool:ha_get_skill_guide:enabled"[^>]*>',
            result.dom,
        )
        assert m is not None
        assert " disabled" not in m.group(0), (
            f"skill guide must be editable when strict mode is off; got: {m.group(0)}"
        )
        assert "Strict best-practices mode" not in result.dom

    def test_all_env_pinned_enabled_group_master_is_checked_and_disabled(
        self, settings_script: str
    ) -> None:
        """The fix generalizes past mandatory tools: env-pinned tools are also
        excluded from ``toggleable`` and still counted as enabled. A group whose
        only tool is env-pinned to ``pinned`` (locked on) is fully enabled and
        non-toggleable, so its master must render checked+disabled — the same
        bug class as the all-mandatory case, via the env-pin branch.
        """
        tools = [
            {
                "name": "ha_foo",
                "title": "Foo",
                "primary_tag": "Utilities",
                "tags": ["Utilities"],
                "description": "d",
                "annotations": {"readOnlyHint": True},
            }
        ]
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._tools_fetch(tools, {}, {"ha_foo": "pinned"}),
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        tag_html = self._master_input(result.dom, "Utilities")
        assert "checked" in tag_html, (
            f"all-env-pinned-on group master must be checked; got: {tag_html}"
        )
        assert " disabled" in tag_html, (
            f"all-env-pinned group master must be disabled; got: {tag_html}"
        )

    def test_partially_enabled_locked_group_master_is_unchecked(
        self, settings_script: str
    ) -> None:
        """A fully-locked group that is only PARTIALLY enabled must render the
        master unchecked — checked means "fully enabled" (N/N), so a mixed
        locked group must not read as ON and contradict its "1/2 enabled" count.

        Group: a mandatory tool (always on) beside an env-pinned-disabled tool
        (off). Both are excluded from ``toggleable`` (empty set → disabled
        master), but only one is enabled, so ``groupEnabled`` (1) != tools (2).
        This is the case where ``groupEnabled > 0`` and ``groupEnabled ===
        tools.length`` diverge; the former would wrongly render it checked.
        """
        tools = [
            {
                "name": "ha_search",  # mandatory, always on
                "title": "Search",
                "primary_tag": "Mixed",
                "tags": ["Mixed"],
                "description": "d",
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "ha_foo",  # env-pinned OFF
                "title": "Foo",
                "primary_tag": "Mixed",
                "tags": ["Mixed"],
                "description": "d",
                "annotations": {"readOnlyHint": True},
            },
        ]
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._tools_fetch(tools, {}, {"ha_foo": "disabled"}),
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        tag_html = self._master_input(result.dom, "Mixed")
        assert "checked" not in tag_html, (
            f"partially-enabled locked group master must be unchecked "
            f"(1/2 enabled); got: {tag_html}"
        )
        assert " disabled" in tag_html, (
            f"fully-locked group master must be disabled; got: {tag_html}"
        )

    def test_toggleable_group_all_disabled_master_is_unchecked(
        self, settings_script: str
    ) -> None:
        """Guard the fix's boundary: a group of ordinary (toggleable) tools
        that are all disabled must still render the master switch unchecked
        and interactive (not disabled), so the user can bulk-enable them.
        """
        tools = [
            {
                "name": "ha_foo",
                "title": "Foo",
                "primary_tag": "Utilities",
                "tags": ["Utilities"],
                "description": "d",
                "annotations": {"readOnlyHint": True},
            }
        ]
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._tools_fetch(tools, {"ha_foo": "disabled"}),
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        tag_html = self._master_input(result.dom, "Utilities")
        assert "checked" not in tag_html, (
            f"all-disabled toggleable group master must be unchecked; got: {tag_html}"
        )
        assert " disabled" not in tag_html, (
            f"toggleable group master must stay interactive; got: {tag_html}"
        )

    def test_mixed_group_keeps_bulk_semantics(self, settings_script: str) -> None:
        """A group mixing a mandatory tool with a disabled toggleable tool
        keeps the interactive bulk semantics: the switch stays enabled and
        reflects ``anyEnabled`` (false here — the one toggleable tool is off),
        so flipping it bulk-enables the toggleable tool. The fix only changes
        the no-toggleable case.
        """
        tools = [
            {
                "name": "ha_search",  # mandatory, always on
                "title": "Search",
                "primary_tag": "Mixed",
                "tags": ["Mixed"],
                "description": "d",
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "ha_foo",  # toggleable, disabled
                "title": "Foo",
                "primary_tag": "Mixed",
                "tags": ["Mixed"],
                "description": "d",
                "annotations": {"readOnlyHint": True},
            },
        ]
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._tools_fetch(tools, {"ha_foo": "disabled"}),
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        tag_html = self._master_input(result.dom, "Mixed")
        assert " disabled" not in tag_html, (
            f"mixed group has a toggleable tool, master must stay interactive; "
            f"got: {tag_html}"
        )
        assert "checked" not in tag_html, (
            f"mixed group's only toggleable tool is disabled, so the bulk "
            f"master must be unchecked; got: {tag_html}"
        )


class TestAdvancedSectionRender:
    """JSDOM coverage for the Advanced Settings sections."""

    def test_locked_field_shows_env_var_name_in_banner(
        self, settings_script: str
    ) -> None:
        """Env-pinned advanced field renders with a banner naming the env var.

        Fixtures a search-section field — the connection section is
        no longer rendered in the panel, so a section: "connection"
        field would be silently dropped.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "fuzzy_threshold",
                            "env_var": "FUZZY_THRESHOLD",
                            "value": 70,
                            "type": "int",
                            "section": "search",
                            "origin": "env",
                            "editable": False,
                            "min": 1,
                            "max": 100,
                        }
                    ]
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert "FUZZY_THRESHOLD" in result.dom, (
            f"expected env var name in locked-row banner; "
            f"dom tail: {result.dom[-2000:]}"
        )
        assert "adv-row" in result.dom and "locked" in result.dom, (
            "expected .adv-row.locked class"
        )

    def test_log_level_renders_as_select_with_choices(
        self, settings_script: str
    ) -> None:
        """str field with choices renders as <select>, not <input>."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "log_level",
                            "env_var": "LOG_LEVEL",
                            "value": "INFO",
                            "type": "str",
                            "section": "diagnostics",
                            "origin": "default",
                            "editable": True,
                            "choices": [
                                "DEBUG",
                                "INFO",
                                "WARNING",
                                "ERROR",
                                "CRITICAL",
                            ],
                        }
                    ]
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)

        select_match = re.search(
            r'<select[^>]*data-adv-field="log_level"[^>]*>(.*?)</select>',
            result.dom,
            re.DOTALL,
        )
        assert select_match is not None, "expected <select> for log_level"
        assert "DEBUG" in select_match.group(1)
        assert "CRITICAL" in select_match.group(1)

    def test_int_field_emits_min_max_attrs(self, settings_script: str) -> None:
        # Same connection-removed migration as test_locked_field above.
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "fuzzy_threshold",
                            "env_var": "FUZZY_THRESHOLD",
                            "value": 70,
                            "type": "int",
                            "section": "search",
                            "origin": "default",
                            "editable": True,
                            "min": 1,
                            "max": 100,
                        }
                    ]
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)

        m = re.search(
            r'<input[^>]*data-adv-field="fuzzy_threshold"[^>]*>',
            result.dom,
        )
        assert m is not None, "expected number input for fuzzy_threshold"
        assert 'min="1"' in m.group(0)
        assert 'max="100"' in m.group(0)


class TestFormControlAccessibility:
    """Every generated form control must carry a ``name`` (or ``id``) so it
    is not flagged by the "form field should have an id or name attribute"
    accessibility rule. ``name`` is additive — no JS selects on it
    (selection is via ``data-*`` attributes), so behaviour is unchanged.
    """

    def test_tool_toggles_carry_name_attribute(self, settings_script: str) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [
                        {
                            "name": "ha_foo",
                            "title": "Foo",
                            "primary_tag": "Utilities",
                            "tags": ["Utilities"],
                            "description": "Test tool",
                            "annotations": {},
                        }
                    ],
                    "states": {"ha_foo": "enabled"},
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        for field in ("enabled", "pinned", "gated"):
            m = re.search(rf'<input[^>]*data-field="{field}"[^>]*>', result.dom)
            assert m is not None, f"expected {field} toggle in DOM"
            assert f'name="tool:ha_foo:{field}"' in m.group(0), (
                f"expected name on {field} toggle; got {m.group(0)}"
            )

    def test_advanced_field_input_carries_name_attribute(
        self, settings_script: str
    ) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "fuzzy_threshold",
                            "env_var": "FUZZY_THRESHOLD",
                            "value": 70,
                            "type": "int",
                            "section": "search",
                            "origin": "default",
                            "editable": True,
                            "min": 1,
                            "max": 100,
                        }
                    ]
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        m = re.search(r'<input[^>]*data-adv-field="fuzzy_threshold"[^>]*>', result.dom)
        assert m is not None, "expected advanced input for fuzzy_threshold"
        assert 'name="adv:fuzzy_threshold"' in m.group(0), (
            f"expected name on advanced input; got {m.group(0)}"
        )

    def test_no_rendered_input_lacks_name_or_id(self, settings_script: str) -> None:
        """Holistic guard: render every form surface present at default page
        init — tool toggles + group master, feature flags, the yaml-packages
        sub-flags, the code-mode numeric sub-rows, advanced fields and a
        choices dropdown — then assert every rendered <input> AND <select>
        carries a name or id, exactly the rule the accessibility audit flags.
        (The backup-config and policy forms load on tab activation — including
        via the ?tab= deep-link path — not at default init, so they are
        covered by their own tests below; the policy predicate <select>s
        render there.)
        """

        def flag(env: str) -> dict:
            return {
                "value": True,
                "origin": "default",
                "editable": True,
                "type": "bool",
                "env_var": env,
            }

        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [
                        {
                            "name": "ha_foo",
                            "title": "Foo",
                            "primary_tag": "Utilities",
                            "tags": ["Utilities"],
                            "description": "Test tool",
                            "annotations": {},
                        }
                    ],
                    "states": {"ha_foo": "enabled"},
                },
            },
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": flag("ENABLE_BETA_FEATURES"),
                        "enable_code_mode": flag("ENABLE_CODE_MODE"),
                        "enable_yaml_config_editing": flag(
                            "ENABLE_YAML_CONFIG_EDITING"
                        ),
                        "enable_yaml_packages_automation": flag(
                            "ENABLE_YAML_PACKAGES_AUTOMATION"
                        ),
                        "enable_yaml_packages_script": flag(
                            "ENABLE_YAML_PACKAGES_SCRIPT"
                        ),
                        "enable_yaml_packages_scene": flag(
                            "ENABLE_YAML_PACKAGES_SCENE"
                        ),
                        # int-typed feature flag -> exercises the number-input
                        # branch of the feature-flag generator.
                        "tool_search_max_results": {
                            "value": 50,
                            "origin": "default",
                            "editable": True,
                            "type": "int",
                            "env_var": "TOOL_SEARCH_MAX_RESULTS",
                            "min": 1,
                            "max": 200,
                        },
                    },
                    "beta_sub_flags": [
                        "enable_code_mode",
                        "enable_yaml_config_editing",
                    ],
                    "is_addon": False,
                },
            },
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "fuzzy_threshold",
                            "env_var": "FUZZY_THRESHOLD",
                            "value": 70,
                            "type": "int",
                            "section": "search",
                            "origin": "default",
                            "editable": True,
                            "min": 1,
                            "max": 100,
                        },
                        {
                            "field": "code_mode_max_duration",
                            "env_var": "CODE_MODE_MAX_DURATION",
                            "value": 30.0,
                            "type": "float",
                            "section": "beta_codemode",
                            "origin": "default",
                            "editable": True,
                            "min": 1.0,
                            "max": 300.0,
                        },
                        {
                            # choices -> renders a <select>, so the guard
                            # also exercises the dropdown generator.
                            "field": "log_level",
                            "env_var": "LOG_LEVEL",
                            "value": "INFO",
                            "type": "str",
                            "section": "diagnostics",
                            "origin": "default",
                            "editable": True,
                            "choices": ["DEBUG", "INFO", "WARNING", "ERROR"],
                        },
                    ]
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)
        controls = re.findall(r"<(?:input|select)\b[^>]*>", result.dom)
        assert controls, "expected at least one rendered form control"
        missing = [c for c in controls if "name=" not in c and "id=" not in c]
        assert not missing, (
            f"{len(missing)} rendered form control(s) lack name/id (a11y): {missing}"
        )
        # Sanity: the expanded fixture really did exercise the extra
        # surfaces, not silently render nothing. `adv:code_mode_max_duration`
        # is emitted by both the Advanced-panel field generator (the fixture
        # lists it as an advanced field) and renderAdvancedSubRows, so this
        # assert only proves *a* control with that name rendered — not the
        # code-mode sub-row specifically.
        assert 'name="adv:code_mode_max_duration"' in result.dom, (
            "no control named adv:code_mode_max_duration rendered — "
            "fixture/holistic-guard drift"
        )
        assert 'name="feature:enable_yaml_packages_automation"' in result.dom, (
            "yaml-packages sub-row did not render — fixture/holistic-guard drift"
        )
        assert '<select name="adv:log_level"' in result.dom, (
            "advanced choices <select> did not render — fixture/guard drift"
        )
        assert 'name="tool-group:' in result.dom, (
            "tool group-master toggle did not render — fixture/guard drift"
        )
        assert 'name="feature:tool_search_max_results"' in result.dom, (
            "int feature-flag input did not render — fixture/guard drift"
        )

    def test_backup_config_inputs_carry_name_attribute(
        self, settings_script: str
    ) -> None:
        """The backup-config form loads on backups-tab activation (not at
        page init), so drive ``loadBackupConfig()`` directly and assert its
        bool / text / number inputs each carry a ``name``.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/backup-config": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "auto_backup_enabled",
                            "env_var": "AUTO_BACKUP_ENABLED",
                            "value": True,
                            "origin": "default",
                            "editable": True,
                        },
                        {
                            "field": "auto_backup_dir",
                            "env_var": "AUTO_BACKUP_DIR",
                            "value": "/backups",
                            "origin": "default",
                            "editable": True,
                        },
                        {
                            "field": "auto_backup_throttle_minutes",
                            "env_var": "AUTO_BACKUP_THROTTLE_MINUTES",
                            "value": 5,
                            "origin": "default",
                            "editable": True,
                        },
                    ],
                    "is_addon": False,
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke=(
                "await loadBackupConfig(); await new Promise(r => setTimeout(r, 100));"
            ),
        )
        _assert_clean_init(result)
        for field in (
            "auto_backup_enabled",
            "auto_backup_dir",
            "auto_backup_throttle_minutes",
        ):
            # Element-scoped: the name must sit on a single <input> tag, not
            # merely appear somewhere in the whole-DOM string.
            m = re.search(
                rf'<input[^>]*name="backup:{re.escape(field)}"[^>]*>', result.dom
            )
            assert m is not None, (
                f"expected backup <input> carrying name=backup:{field}; "
                f"dom tail: {result.dom[-2000:]}"
            )

    def test_policy_predicate_controls_carry_name_attribute(
        self, settings_script: str
    ) -> None:
        """The policy-rule editor renders on tab activation, not page init.
        Drive ``policyLoadConfig()`` with one rule so ``renderPolicyCard``
        emits the predicate form, and assert its always-rendered controls
        carry a ``name``. (The predicate *value* control renders only after
        the predicate form is opened, and re-renders on op/path edits, so it
        has its own dedicated test below —
        ``test_policy_predicate_value_control_carries_name_attribute`` —
        which drives both generator sites.)
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {"enable_tool_security_policies": {"value": True}},
                },
            },
            "/api/policy/config": {
                "status": 200,
                "json": {
                    "wait_seconds": 60,
                    "approval_ttl_minutes": 5,
                    "rules": [
                        {
                            "tool_name": "ha_config_set_helper",
                            "when": [
                                {
                                    "path": "args.helper_type",
                                    "op": "eq",
                                    "value": "input_boolean",
                                }
                            ],
                            "remember_minutes": 0,
                        }
                    ],
                },
            },
            "/api/policy/pending": {
                "status": 503,
                "json": {"error": "irrelevant for this test"},
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="await window.policyLoadConfig();",
        )
        _assert_clean_init(result)
        for nm in (
            "policy:predicate-path",
            "policy:predicate-op",
            "policy:predicate-path-custom",
            "policy:remember-minutes",
        ):
            # Element-scoped: the name must sit on a single <input>/<select>
            # tag, not merely appear somewhere in the whole-DOM string.
            m = re.search(
                rf'<(?:input|select)\b[^>]*name="{re.escape(nm)}"[^>]*>', result.dom
            )
            assert m is not None, (
                f"expected policy control element carrying name={nm!r} in the "
                f"rendered card; dom tail: {result.dom[-2500:]}"
            )

    def test_policy_predicate_value_control_carries_name_attribute(
        self, settings_script: str
    ) -> None:
        """The predicate *value* control is the last generated surface that
        renders only after the predicate form is opened (click add-condition),
        re-rendering on op/path edits. ``name="policy:predicate-value"`` is
        emitted at both generator sites, so drive each through the JS harness:

        - **Free-text branch** (``renderFreeTextValue`` -> ``<input>``): a
          failed tool-schema fetch degrades gracefully to the free-text JSON
          input, exactly the default path on form-open with no schema.
        - **<select> branch** (``renderChoiceSelect`` -> ``<select>``): a
          schema ``enum`` on the chosen path upgrades the value control to a
          choice dropdown.

        Opening the form is driven via ``policyLoadConfig()`` + a real click on
        ``.policy-add-predicate`` (``openForm`` precedent already in this
        class), so no internal function is poked directly.
        """
        base_fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {"enable_tool_security_policies": {"value": True}},
                },
            },
            "/api/policy/config": {
                "status": 200,
                "json": {
                    "wait_seconds": 60,
                    "approval_ttl_minutes": 5,
                    "rules": [
                        {
                            "tool_name": "ha_config_set_helper",
                            "when": [],
                            "remember_minutes": 0,
                        }
                    ],
                },
            },
            "/api/policy/pending": {
                "status": 503,
                "json": {"error": "irrelevant for this test"},
            },
        }

        # --- Free-text branch: tool-schema 503 -> free-text <input> ---------
        free_text = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map={
                **base_fetches,
                "/api/policy/tool-schema": {
                    "status": 503,
                    "json": {"error": "no schema available"},
                },
            },
            invoke="""
              await window.policyLoadConfig();
              const card = document.querySelector('.policy-rule-card');
              card.querySelector('.policy-add-predicate').click();
              await new Promise(r => setTimeout(r, 100));
            """,
        )
        _assert_clean_init(free_text)
        m = re.search(
            r'<input[^>]*class="[^"]*policy-predicate-value\b[^"]*"[^>]*>',
            free_text.dom,
        )
        assert m is not None, (
            "expected free-text value <input> after opening the predicate "
            f"form; dom tail: {free_text.dom[-2500:]}"
        )
        assert 'name="policy:predicate-value"' in m.group(0), (
            f"free-text value <input> missing name; got {m.group(0)}"
        )

        # --- <select> branch: schema enum on the chosen path -> <select> ----
        select_branch = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map={
                **base_fetches,
                "/api/policy/tool-schema": {
                    "status": 200,
                    "json": {
                        "paths": [
                            {
                                "path": "args.helper_type",
                                "label": "Helper type",
                                "type": "str",
                                "enum": ["input_boolean", "input_number"],
                            }
                        ],
                        "value_sources": {},
                    },
                },
            },
            invoke="""
              await window.policyLoadConfig();
              const card = document.querySelector('.policy-rule-card');
              card.querySelector('.policy-add-predicate').click();
              await new Promise(r => setTimeout(r, 100));
              // op defaults to eq; pick the enum-backed path so the value
              // control upgrades from free-text to a <select>.
              const pathSel = card.querySelector('.policy-predicate-path-select');
              pathSel.value = 'args.helper_type';
              pathSel.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 100));
            """,
        )
        _assert_clean_init(select_branch)
        m = re.search(
            r'<select[^>]*class="[^"]*policy-predicate-value-control[^"]*"[^>]*>',
            select_branch.dom,
        )
        assert m is not None, (
            "expected choice <select> after selecting the enum-backed path; "
            f"dom tail: {select_branch.dom[-2500:]}"
        )
        assert 'name="policy:predicate-value"' in m.group(0), (
            f"choice <select> missing name; got {m.group(0)}"
        )


class TestAddonModeLockedBannerCopy:
    """Locked-banner copy must avoid 'unset env var' wording in addon mode.

    Addon operators have no env-var surface to unset — the var was set
    either by start.py (from /data/options.json) or by Supervisor. The
    standalone-mode copy "unset it to edit here" is actively
    misleading there. When the features /
    advanced / backup endpoints return ``is_addon=true``, the banner
    must point at the addon Configuration tab instead.
    """

    def test_master_locked_banner_in_addon_mode_points_at_configuration(
        self, settings_script: str
    ) -> None:
        """Master `enable_beta_features` env-locked in addon mode renders
        copy pointing at addon Configuration toggles, not "unset env var".
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": {
                            "value": True,
                            "origin": "env",
                            "editable": False,
                            "type": "bool",
                            "env_var": "ENABLE_BETA_FEATURES",
                        }
                    },
                    "beta_sub_flags": [
                        "enable_yaml_config_editing",
                        "enable_filesystem_tools",
                        "enable_code_mode",
                        "enable_lite_docstrings",
                    ],
                    "is_addon": True,
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        # Standalone copy must NOT appear.
        assert "unset it to edit here" not in result.dom, (
            "addon-mode master banner still shows standalone 'unset env var' copy"
        )
        # Addon-aware copy must appear.
        assert "App (add-on) Configuration" in result.dom, (
            f"expected 'app (add-on) Configuration' hint; dom tail: {result.dom[-2000:]}"
        )

    def test_advanced_locked_banner_in_addon_mode_avoids_unset_copy(
        self, settings_script: str
    ) -> None:
        """Env-pinned advanced field in addon mode renders addon-runtime
        copy, not "unset env var".
        """
        # Use a search-section field — the connection section is no
        # longer rendered in the panel, so a fixture
        # with section: "connection" would be silently dropped by
        # loadAdvancedSettings() and the assertion would fail with an
        # empty body.
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "fuzzy_threshold",
                            "env_var": "FUZZY_THRESHOLD",
                            "value": 70,
                            "type": "int",
                            "section": "search",
                            "origin": "env",
                            "editable": False,
                        }
                    ],
                    "is_addon": True,
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert "unset it to edit here" not in result.dom, (
            "addon-mode advanced banner still shows standalone 'unset env var' copy"
        )
        assert "App (add-on) runtime environment" in result.dom, (
            f"expected 'app (add-on) runtime environment' wording; "
            f"dom tail: {result.dom[-2000:]}"
        )

    def test_standalone_mode_still_uses_unset_copy(self, settings_script: str) -> None:
        """Regression guard: when is_addon is false (or omitted), the
        existing standalone "unset env var" copy must still render —
        the addon branch should NOT take over standalone deployments.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": {
                            "value": True,
                            "origin": "env",
                            "editable": False,
                            "type": "bool",
                            "env_var": "ENABLE_BETA_FEATURES",
                        }
                    },
                    "beta_sub_flags": [],
                    "is_addon": False,
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert "unset it to edit here" in result.dom, (
            f"standalone-mode copy regressed; dom tail: {result.dom[-2000:]}"
        )

    def test_codemode_subrow_locked_banner_in_addon_mode_avoids_unset_copy(
        self, settings_script: str
    ) -> None:
        """Env-pinned code-mode sub-row in addon mode must use addon-runtime
        copy, not the standalone "unset env var" instruction.

        ``CODE_MODE_SAVED_TOOLS_PATH`` is set by the add-on's ``start.py``
        via ``os.environ.setdefault`` and is absent from the add-on
        ``config.yaml`` schema, so the Saved-tools-path row renders
        env-pinned and read-only and the add-on user cannot unset it —
        the standalone "unset it to edit here" copy is unactionable.
        ``renderAdvancedSubRows`` was
        overlooked when the addon-aware locked-note copy was added to
        the other render paths (see the master/advanced banner tests
        above); this pins the same rule for the code-mode sub-rows.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": {
                            "value": True,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_BETA_FEATURES",
                        },
                        "enable_code_mode": {
                            "value": True,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_CODE_MODE",
                        },
                    },
                    "beta_sub_flags": ["enable_code_mode"],
                    "is_addon": True,
                },
            },
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "code_mode_saved_tools_path",
                            "env_var": "CODE_MODE_SAVED_TOOLS_PATH",
                            "value": "/data/saved_tools.json",
                            "type": "str",
                            "section": "beta_codemode",
                            "origin": "env",
                            "editable": False,
                        }
                    ],
                    "is_addon": True,
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert 'data-adv-field="code_mode_saved_tools_path"' in result.dom, (
            f"expected the saved-tools code-mode sub-row to render; "
            f"dom tail: {result.dom[-2000:]}"
        )
        assert "unset it to edit here" not in result.dom, (
            "addon-mode code-mode sub-row still shows standalone "
            "'unset env var' copy the add-on user cannot act on"
        )
        # Field-specific, honest copy — NOT the generic "managed by
        # Supervisor" helper text (Supervisor never sees this start.py
        # setdefault value).
        assert "Hardcoded to" in result.dom and "cannot be changed" in result.dom, (
            f"expected field-specific 'hardcoded in app (add-on) mode' copy; "
            f"dom tail: {result.dom[-2000:]}"
        )
        assert "managed by Home Assistant Supervisor" not in result.dom, (
            "App (add-on) copy must not imply Supervisor manages this hardcoded field"
        )

    def test_codemode_subrow_standalone_mode_still_uses_unset_copy(
        self, settings_script: str
    ) -> None:
        """Regression guard: outside addon mode the code-mode sub-row keeps
        the standalone "unset env var" copy.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": {
                            "value": True,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_BETA_FEATURES",
                        },
                        "enable_code_mode": {
                            "value": True,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_CODE_MODE",
                        },
                    },
                    "beta_sub_flags": ["enable_code_mode"],
                    "is_addon": False,
                },
            },
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "code_mode_saved_tools_path",
                            "env_var": "CODE_MODE_SAVED_TOOLS_PATH",
                            "value": "/srv/saved_tools.json",
                            "type": "str",
                            "section": "beta_codemode",
                            "origin": "env",
                            "editable": False,
                        }
                    ],
                    "is_addon": False,
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert "unset it to edit here" in result.dom, (
            f"standalone code-mode sub-row copy regressed; "
            f"dom tail: {result.dom[-2000:]}"
        )

    def test_codemode_saved_tools_path_blank_warns_persistence_off(
        self, settings_script: str
    ) -> None:
        """Standalone with no path set: the saved-tools row must warn that
        custom tools are kept in memory only and lost on restart — a blank
        ``code_mode_saved_tools_path`` disables persistence.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": {
                            "value": True,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_BETA_FEATURES",
                        },
                        "enable_code_mode": {
                            "value": True,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_CODE_MODE",
                        },
                    },
                    "beta_sub_flags": ["enable_code_mode"],
                    "is_addon": False,
                },
            },
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "code_mode_saved_tools_path",
                            "env_var": "CODE_MODE_SAVED_TOOLS_PATH",
                            "value": "",
                            "type": "str",
                            "section": "beta_codemode",
                            "origin": "default",
                            "editable": True,
                        }
                    ],
                    "is_addon": False,
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert "memory only" in result.dom and "lost on restart" in result.dom, (
            f"expected blank-path persistence warning on saved-tools row; "
            f"dom tail: {result.dom[-2000:]}"
        )


class TestBetaBlockRendersAtBottom:
    """Beta master + sub-flags render into the dedicated `betaBody` div,
    NOT featuresBody, so the dangerous block sits at the bottom of the
    Server Settings panel.
    """

    def _payload(self) -> dict:
        return {
            "flags": {
                "enable_tool_search": {
                    "value": False,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "ENABLE_TOOL_SEARCH",
                },
                "enable_beta_features": {
                    "value": True,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "ENABLE_BETA_FEATURES",
                },
                "enable_yaml_config_editing": {
                    "value": False,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "ENABLE_YAML_CONFIG_EDITING",
                },
                "enable_filesystem_tools": {
                    "value": False,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "HAMCP_ENABLE_FILESYSTEM_TOOLS",
                },
            },
            "beta_sub_flags": [
                "enable_yaml_config_editing",
                "enable_filesystem_tools",
                "enable_code_mode",
                "enable_lite_docstrings",
            ],
            "is_addon": False,
        }

    def test_non_beta_rows_render_into_featuresBody(self, settings_script: str) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {"status": 200, "json": self._payload()},
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const fb = document.getElementById('featuresBody').innerHTML;
              const bb = document.getElementById('betaBody').innerHTML;
              window.__bodies = JSON.stringify({fb, bb});
            """,
        )
        _assert_clean_init(result)
        # Non-beta row goes to featuresBody only.
        assert "Enable tool search" in result.dom
        m = re.search(r'<tbody id="featuresBody">(.*?)</tbody>', result.dom, re.DOTALL)
        assert m is not None, "featuresBody tbody not found in dom"
        fb_content = m.group(1)
        assert "Enable tool search" in fb_content, (
            "tool-search row should land in featuresBody"
        )
        assert "Enable beta features" not in fb_content, (
            "beta master row leaked into featuresBody (should be in betaBody)"
        )

    def test_beta_rows_render_into_betaBody(self, settings_script: str) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {"status": 200, "json": self._payload()},
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              // Read the live container HTML directly; avoids brittle
              // regex matching against nested <div>s in serialised dom.
              const bb = document.getElementById('betaBody');
              const fb = document.getElementById('featuresBody');
              window.__bb = bb ? bb.innerHTML : '';
              window.__fb = fb ? fb.innerHTML : '';
            """,
        )
        _assert_clean_init(result)
        # The harness serialises window globals into result.dom — pick
        # the recorded innerHTML out of the trailing <script> block the
        # JSDOM serialiser emits. Easier path: match the betaBody div in
        # the serialised dom with a balanced-enough heuristic (start tag
        # to next sibling-level marker).
        bb_match = re.search(
            r'<div id="betaBody">(.*?)</div>\s*</body>', result.dom, re.DOTALL
        )
        # MIN_DOM places betaBody as the second-to-last body child;
        # everything between its open tag and the closing body wrapper
        # is its content (incl. nested children) bar the trailing
        # </div>.
        if bb_match is None:
            # Fallback: search for the rendered master-row marker inside
            # the betaBody region by anchoring on its class name.
            assert 'class="feature-row beta-master-row"' in result.dom, (
                "beta master row never rendered anywhere"
            )
            assert 'class="feature-row beta-sub"' in result.dom, (
                "beta sub-row never rendered anywhere"
            )
            # Without a clean container slice we can't prove leakage —
            # but featuresBody is the only other plausible destination
            # and renderFeatureFlags wipes both with .innerHTML = ''.
            fb_match = re.search(
                r'<tbody id="featuresBody">(.*?)</tbody>', result.dom, re.DOTALL
            )
            fb_content = fb_match.group(1) if fb_match else ""
            assert "Enable beta features" not in fb_content, (
                "beta master row leaked into featuresBody"
            )
            return
        bb_content = bb_match.group(1)
        assert "Enable beta features" in bb_content, (
            "beta master row should land in betaBody"
        )
        assert "Enable YAML config editing" in bb_content, (
            "beta sub-flag should land in betaBody"
        )
        assert "Enable tool search" not in bb_content, (
            "non-beta row leaked into betaBody"
        )

    def test_beta_section_header_present_with_danger_styling(self) -> None:
        """Section header reads 'Beta features (dangerous)' so users see
        the category boundary; styling uses .beta-section-title.
        Asserted against the production HTML (the header lives in the
        static panel-server markup, not in any JS-rendered container).
        """
        from ha_mcp.settings_ui import _SETTINGS_HTML

        assert "Beta features (dangerous)" in _SETTINGS_HTML, (
            "beta section heading copy missing or changed in production HTML"
        )
        assert "beta-section-title" in _SETTINGS_HTML, (
            "beta section title class missing in production HTML"
        )

    def test_advanced_field_autosaves_with_value(self, settings_script: str) -> None:
        """Editing an advanced field auto-saves (no Save button): on the
        native ``change`` event, after the debounce, exactly one POST goes
        to /api/settings/advanced carrying the edited value, and a success
        toast appears. Covers the file-origin routing path."""
        adv_field = {
            "field": "log_level",
            "env_var": "LOG_LEVEL",
            "value": "INFO",
            "type": "str",
            "section": "diagnostics",
            "origin": "default",
            "editable": True,
            "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [adv_field], "is_addon": False},
                "responses": [
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                    {"status": 200, "json": {"applied": {"log_level": "DEBUG"}}},
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                ],
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const sel = document.querySelector('select[data-adv-field="log_level"]');
              sel.value = 'DEBUG';
              sel.dispatchEvent(new Event('change'));
              // Past the 800ms debounce + the in-flight POST. Stamp the
              // toast into a data attribute now: the fake clock advances
              // past the toast's auto-dismiss before result.dom is
              // captured, so reading it live is the only reliable check.
              await new Promise(r => setTimeout(r, 1500));
              const t = document.querySelector('#ha-toast-region .ha-toast');
              document.body.setAttribute('data-toast',
                t ? (t.className + '::' + (t.querySelector('.ha-toast-msg')?.textContent || '')) : 'NONE');
              document.body.setAttribute('data-restart',
                String(document.getElementById('restartNotice').classList.contains('show')));
            """,
        )
        _assert_clean_init(result)
        posts = [
            f
            for f in result.fetches
            if "/api/settings/advanced" in f["url"] and f["method"] == "POST"
        ]
        assert len(posts) == 1, (
            f"expected exactly one auto-save POST, got {len(posts)}: {result.fetches}"
        )
        body = json.loads(posts[0]["body"])
        assert body.get("log_level") == "DEBUG", (
            f"auto-save POST body missing the edited value: {body}"
        )
        # log_level IS in ADVANCED_RESTART_REQUIRED, so this pins the
        # restart branch: exact toast text + banner shown + cross-tab broadcast.
        m = re.search(r'data-toast="([^"]*)"', result.dom)
        assert m and "Saved. Restart required." in m.group(1), (
            f"expected restart-required success toast; got {m.group(1) if m else None}"
        )
        assert 'data-restart="true"' in result.dom, (
            "restart banner not shown for a restart-required field"
        )
        assert result.broadcasts_of_type("restart-required"), (
            "no restart-required cross-tab broadcast"
        )

    def test_dev_mode_toggle_enable_is_confirm_gated(
        self, settings_script: str
    ) -> None:
        """Enabling the dev-mode toggle (issue #1775) must pass a
        confirm() gate: declining reverts the checkbox and saves
        nothing; accepting fires exactly one auto-save POST."""
        adv_field = {
            "field": "enable_dev_mode",
            "env_var": "HAMCP_ENABLE_DEV_MODE",
            "value": False,
            "type": "bool",
            "section": "developer",
            "origin": "default",
            "editable": True,
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [adv_field], "is_addon": False},
                "responses": [
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                    {"status": 200, "json": {"applied": {"enable_dev_mode": True}}},
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                ],
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const cb = document.querySelector(
                '#advDeveloper input[data-adv-field="enable_dev_mode"]');
              document.body.setAttribute('data-rendered', String(!!cb));
              // Decline the warning: the toggle must revert and no save fires.
              window.confirm = () => false;
              cb.checked = true;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 1200));
              document.body.setAttribute('data-declined-state', String(cb.checked));
              // Accept the warning: the save proceeds.
              window.confirm = () => true;
              cb.checked = true;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 1500));
            """,
        )
        _assert_clean_init(result)
        assert 'data-rendered="true"' in result.dom, (
            "enable_dev_mode row did not render into #advDeveloper"
        )
        assert 'data-declined-state="false"' in result.dom, (
            "declining the confirm() must revert the dev-mode checkbox"
        )
        posts = [
            f
            for f in result.fetches
            if "/api/settings/advanced" in f["url"] and f["method"] == "POST"
        ]
        assert len(posts) == 1, (
            f"expected exactly one save POST (declined change must not "
            f"save), got {len(posts)}: {result.fetches}"
        )
        assert json.loads(posts[0]["body"]).get("enable_dev_mode") is True

    def test_security_policy_access_toggle_enable_is_confirm_gated(
        self, settings_script: str
    ) -> None:
        """Enabling dev-tools security-policy access (issue #2141) must
        pass its own confirm() gate — it is stored independently of the
        dev-mode toggle (though effective only while dev mode is on):
        declining reverts the checkbox and saves nothing; accepting
        fires exactly one auto-save POST."""
        adv_field = {
            "field": "dev_tools_security_policy_access",
            "env_var": "HAMCP_DEV_SECURITY_POLICY_ACCESS",
            "value": False,
            "type": "bool",
            "section": "developer",
            "origin": "default",
            "editable": True,
        }
        applied = {"dev_tools_security_policy_access": True}
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [adv_field], "is_addon": False},
                "responses": [
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                    {"status": 200, "json": {"applied": applied}},
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                ],
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const cb = document.querySelector(
                '#advDeveloper input[data-adv-field="dev_tools_security_policy_access"]');
              document.body.setAttribute('data-rendered', String(!!cb));
              // Decline the warning: the toggle must revert and no save fires.
              window.confirm = () => false;
              cb.checked = true;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 1200));
              document.body.setAttribute('data-declined-state', String(cb.checked));
              // Accept the warning: the save proceeds.
              window.confirm = () => true;
              cb.checked = true;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 1500));
            """,
        )
        _assert_clean_init(result)
        assert 'data-rendered="true"' in result.dom, (
            "dev_tools_security_policy_access row did not render into #advDeveloper"
        )
        assert 'data-declined-state="false"' in result.dom, (
            "declining the confirm() must revert the policy-access checkbox"
        )
        posts = [
            f
            for f in result.fetches
            if "/api/settings/advanced" in f["url"] and f["method"] == "POST"
        ]
        assert len(posts) == 1, (
            f"expected exactly one save POST (declined change must not "
            f"save), got {len(posts)}: {result.fetches}"
        )
        assert (
            json.loads(posts[0]["body"]).get("dev_tools_security_policy_access") is True
        )

    def test_security_policy_access_toggle_disable_skips_confirm(
        self, settings_script: str
    ) -> None:
        """Unchecking the policy-access toggle must save immediately with
        NO confirm() prompt — the gate protects only the arming
        direction. A confirm on disable whose Cancel path force-unchecks
        would leave the UI showing OFF while the server keeps access ON."""
        adv_field = {
            "field": "dev_tools_security_policy_access",
            "env_var": "HAMCP_DEV_SECURITY_POLICY_ACCESS",
            "value": True,
            "type": "bool",
            "section": "developer",
            "origin": "file",
            "editable": True,
        }
        applied = {"dev_tools_security_policy_access": False}
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [adv_field], "is_addon": False},
                "responses": [
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                    {"status": 200, "json": {"applied": applied}},
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                ],
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const cb = document.querySelector(
                '#advDeveloper input[data-adv-field="dev_tools_security_policy_access"]');
              document.body.setAttribute('data-rendered', String(!!cb && cb.checked));
              let confirms = 0;
              window.confirm = () => { confirms += 1; return false; };
              cb.checked = false;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 1500));
              document.body.setAttribute('data-confirms', String(confirms));
            """,
        )
        _assert_clean_init(result)
        assert 'data-rendered="true"' in result.dom, (
            "policy-access row did not render checked into #advDeveloper"
        )
        assert 'data-confirms="0"' in result.dom, (
            "disabling policy access must not prompt confirm()"
        )
        posts = [
            f
            for f in result.fetches
            if "/api/settings/advanced" in f["url"] and f["method"] == "POST"
        ]
        assert len(posts) == 1, (
            f"disable must auto-save exactly once, got {len(posts)}: {result.fetches}"
        )
        assert (
            json.loads(posts[0]["body"]).get("dev_tools_security_policy_access")
            is False
        )

    def test_advanced_field_autosave_error_shows_error_toast(
        self, settings_script: str
    ) -> None:
        """A failed advanced auto-save surfaces an error toast (the
        ha-toast-error variant), not a silent failure."""
        adv_field = {
            "field": "log_level",
            "env_var": "LOG_LEVEL",
            "value": "INFO",
            "type": "str",
            "section": "diagnostics",
            "origin": "default",
            "editable": True,
            "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [adv_field], "is_addon": False},
                "responses": [
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                    {"status": 400, "json": {"error": {"message": "bad log level"}}},
                ],
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const sel = document.querySelector('select[data-adv-field="log_level"]');
              sel.value = 'DEBUG';
              sel.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 1500));
              const t = document.querySelector('#ha-toast-region .ha-toast');
              document.body.setAttribute('data-toast',
                t ? (t.className + '::' + (t.querySelector('.ha-toast-msg')?.textContent || '')) : 'NONE');
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'data-toast="([^"]*)"', result.dom)
        assert m and "ha-toast-error" in m.group(1), (
            f"expected an error toast on failed auto-save; got {m.group(1) if m else None}"
        )
        assert m and "bad log level" in m.group(1), (
            f"error toast missing server message; got {m.group(1) if m else None}"
        )

    def test_advanced_autosave_no_restart_field_plain_saved(
        self, settings_script: str
    ) -> None:
        """A field NOT in ADVANCED_RESTART_REQUIRED saves with a plain
        "Saved." toast and does NOT raise the restart banner — pins the
        no-restart branch (dashboard_screenshot_engine_url is resolved live)."""
        adv_field = {
            "field": "dashboard_screenshot_engine_url",
            "env_var": "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL",
            "value": "",
            "type": "str",
            "section": "tools_surface",
            "origin": "default",
            "editable": True,
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [adv_field], "is_addon": False},
                "responses": [
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                    {"status": 200, "json": {"applied": {}}},
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                ],
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const inp = document.querySelector('[data-adv-field="dashboard_screenshot_engine_url"]');
              inp.value = 'http://engine.local';
              inp.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 1500));
              const t = document.querySelector('#ha-toast-region .ha-toast');
              document.body.setAttribute('data-toast',
                t ? (t.querySelector('.ha-toast-msg')?.textContent || '') : 'NONE');
              document.body.setAttribute('data-restart',
                String(document.getElementById('restartNotice').classList.contains('show')));
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'data-toast="([^"]*)"', result.dom)
        assert m and m.group(1) == "Saved.", (
            f"expected plain 'Saved.' for a non-restart field; got {m.group(1) if m else None}"
        )
        assert 'data-restart="false"' in result.dom, (
            "restart banner should NOT show for a non-restart field"
        )
        assert not result.broadcasts_of_type("restart-required"), (
            "no restart broadcast expected for a non-restart field"
        )

    def test_advanced_autosave_preserves_edit_made_during_reload(
        self, settings_script: str
    ) -> None:
        """Regression (data loss): a pending edit must survive the post-save
        reload. loadAdvancedSettings() must NOT reset _advancedDirty or revert
        the input to the server value, or an edit made to another field while
        a save was in flight is silently lost."""
        adv_field = {
            "field": "fuzzy_threshold",
            "env_var": "FUZZY_THRESHOLD",
            "value": 60,
            "type": "int",
            "section": "search",
            "origin": "default",
            "editable": True,
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [adv_field], "is_addon": False},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              // Simulate an edit pending when a reload fires (the server still
              // reports the old value 60).
              _advancedDirty['fuzzy_threshold'] = 999;
              await loadAdvancedSettings();
              const input = document.querySelector('[data-adv-field="fuzzy_threshold"]');
              document.body.setAttribute('data-test',
                `dirty=${_advancedDirty['fuzzy_threshold']} input=${input ? input.value : 'none'}`);
            """,
        )
        _assert_clean_init(result)
        assert 'data-test="dirty=999 input=999"' in result.dom, (
            "pending edit was not preserved + re-stamped across reload; "
            f"dom tail: {result.dom[-400:]}"
        )

    def test_advanced_autosave_nan_drops_pending_and_skips_save(
        self, settings_script: str
    ) -> None:
        """Clearing a number field after a valid edit drops the pending value
        (no stale save) and triggers no POST."""
        adv_field = {
            "field": "fuzzy_threshold",
            "env_var": "FUZZY_THRESHOLD",
            "value": 60,
            "type": "int",
            "section": "search",
            "origin": "default",
            "editable": True,
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [adv_field], "is_addon": False},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const input = document.querySelector('[data-adv-field="fuzzy_threshold"]');
              input.value = '5';
              input.dispatchEvent(new Event('change'));   // pending=5, debounce armed
              input.value = '';
              input.dispatchEvent(new Event('change'));    // NaN -> drop pending
              const pending = ('fuzzy_threshold' in _advancedDirty)
                ? String(_advancedDirty['fuzzy_threshold']) : 'gone';
              await new Promise(r => setTimeout(r, 1500));  // past debounce
              document.body.setAttribute('data-test', `pending=${pending}`);
            """,
        )
        _assert_clean_init(result)
        assert 'data-test="pending=gone"' in result.dom, (
            f"NaN edit should drop the pending value; dom tail: {result.dom[-300:]}"
        )
        posts = [
            f
            for f in result.fetches
            if "/api/settings/advanced" in f["url"] and f["method"] == "POST"
        ]
        assert len(posts) == 0, f"a NaN/empty number field must not POST; got {posts}"

    def test_advanced_autosave_partitions_addon_and_file_batches(
        self, settings_script: str
    ) -> None:
        """Editing an addon-origin and a file-origin field in one debounce
        window posts TWO separate batches; no batch mixes origins (the server
        500s on a mixed batch)."""
        file_field = {
            "field": "fuzzy_threshold",
            "env_var": "FUZZY_THRESHOLD",
            "value": 60,
            "type": "int",
            "section": "search",
            "origin": "default",
            "editable": True,
        }
        addon_field = {
            "field": "timeout",
            "env_var": "HOMEASSISTANT_TIMEOUT",
            "value": 30,
            "type": "int",
            "section": "operations",
            "origin": "addon",
            "editable": True,
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [file_field, addon_field], "is_addon": True},
                "responses": [
                    {
                        "status": 200,
                        "json": {"fields": [file_field, addon_field], "is_addon": True},
                    },
                    {"status": 200, "json": {"applied": {}}},
                    {"status": 200, "json": {"applied": {}}},
                    {
                        "status": 200,
                        "json": {"fields": [file_field, addon_field], "is_addon": True},
                    },
                ],
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const f1 = document.querySelector('[data-adv-field="fuzzy_threshold"]');
              const f2 = document.querySelector('[data-adv-field="timeout"]');
              f1.value = '10'; f1.dispatchEvent(new Event('change'));
              f2.value = '20'; f2.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 1500));
            """,
        )
        _assert_clean_init(result)
        posts = [
            f
            for f in result.fetches
            if "/api/settings/advanced" in f["url"] and f["method"] == "POST"
        ]
        assert len(posts) == 2, (
            f"expected two batches (file + addon); got {len(posts)}: {posts}"
        )
        bodies = [json.loads(p["body"]) for p in posts]
        for b in bodies:
            assert not ("fuzzy_threshold" in b and "timeout" in b), (
                f"a batch mixed addon + file origins (server would 500): {b}"
            )
        assert any("fuzzy_threshold" in b for b in bodies), (
            "file field missing from any batch"
        )
        assert any("timeout" in b for b in bodies), "addon field missing from any batch"

    def test_beta_master_help_text_contains_danger_warning(
        self, settings_script: str
    ) -> None:
        """`enable_beta_features` help-text must lead with the danger
        warning — these features can permanently damage HA and users
        must be told before flipping the master.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {"status": 200, "json": self._payload()},
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert "PERMANENTLY DAMAGE" in result.dom, (
            "master beta help-text missing 'PERMANENTLY DAMAGE' warning"
        )
        assert "OWN RISK" in result.dom, (
            "master beta help-text missing 'OWN RISK' framing"
        )
        assert "backup" in result.dom.lower(), (
            "master beta help-text missing backup advice"
        )


class TestBetaMasterToggleLiveRender:
    """JSDOM coverage for live re-render on master flip."""

    def _flags_payload(self, master_value: bool) -> dict:
        return {
            "flags": {
                "enable_beta_features": {
                    "value": master_value,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "ENABLE_BETA_FEATURES",
                },
                "enable_yaml_config_editing": {
                    "value": False,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "ENABLE_YAML_CONFIG_EDITING",
                },
            },
            "beta_sub_flags": ["enable_yaml_config_editing"],
        }

    def test_master_off_renders_subrow_dimmed(self, settings_script: str) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": self._flags_payload(master_value=False),
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        # The yaml-editing row must carry both beta-sub AND dimmed classes.
        assert "beta-sub dimmed" in result.dom or (
            "beta-sub" in result.dom and "dimmed" in result.dom
        ), f"expected dimmed beta-sub row; dom tail: {result.dom[-3000:]}"

    def test_master_on_renders_subrow_not_dimmed(self, settings_script: str) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": self._flags_payload(master_value=True),
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        # Row has beta-sub class but NOT dimmed.

        beta_sub_rows = re.findall(
            r'<div[^>]*class="[^"]*beta-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert beta_sub_rows, "expected at least one beta-sub row"
        for row_html in beta_sub_rows:
            assert "dimmed" not in row_html, (
                f"unexpected dimmed on master-on row: {row_html}"
            )

    def test_master_off_click_dims_subrow_live_without_clobbering_value(
        self, settings_script: str
    ) -> None:
        """F.37 — dispatch a real change event on the master toggle and
        assert the sub-row goes dimmed + disabled in the same tick,
        WITHOUT visually flipping the sub-flag's checked state. The
        live UI preserves the user's prior sub-flag selection (the
        server-side cascade-clear was removed; the persisted file
        keeps the truthy values, and the runtime master gate forces
        sub-flags off at the Settings layer without touching the
        file) — re-enabling the master later restores the sub-flag
        values automatically.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                # Master ON, sub-flag ON — flipping master off should
                # dim the sub-row but leave its checkbox checked.
                "json": {
                    "flags": {
                        "enable_beta_features": {
                            "value": True,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_BETA_FEATURES",
                        },
                        "enable_yaml_config_editing": {
                            "value": True,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_YAML_CONFIG_EDITING",
                        },
                    },
                    "beta_sub_flags": ["enable_yaml_config_editing"],
                    "is_addon": False,
                },
            },
            "/save-features": {"status": 200, "json": {"success": True}},
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              // Wait for the initial render.
              await new Promise(r => setTimeout(r, 200));
              // Find the master checkbox and flip it off.
              const beta = document.getElementById('betaBody');
              const masterInput = beta.querySelector(
                '.beta-master-row input[type="checkbox"]'
              );
              masterInput.checked = false;
              masterInput.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 50));
              // Probe via JS — JSDOM serialises attributes, not
              // properties, so input.checked never shows up in the
              // serialised dom. Stash the .checked property of the
              // sub-row's checkbox into a probe div for the test to
              // read.
              const subInput = document.querySelector(
                '.feature-row.beta-sub input[type="checkbox"]'
              );
              const probe = document.createElement('div');
              probe.id = '__sub_state_probe';
              if (!subInput) {
                // Distinguish "selector missed" from "checkbox value
                // flipped" so the failure message in the harness
                // points at the right root cause.
                probe.dataset.error = 'beta-sub input not in DOM';
              } else {
                probe.dataset.subChecked = String(subInput.checked);
                probe.dataset.subDisabled = String(subInput.disabled);
              }
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        sub_row_match = re.search(
            r'<div[^>]*class="feature-row beta-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert sub_row_match, (
            f"beta-sub row missing after master flip; dom: {result.dom[-3000:]}"
        )
        assert "dimmed" in sub_row_match.group(0), (
            f"sub-row should be dimmed after master-off: {sub_row_match.group(0)}"
        )
        # Read probe div for the .checked / .disabled property values.
        probe_match = re.search(r'<div[^>]*id="__sub_state_probe"[^>]*>', result.dom)
        assert probe_match, f"sub-state probe missing; dom tail: {result.dom[-2000:]}"
        probe_attrs = probe_match.group(0)
        assert "data-error" not in probe_attrs, (
            f"selector missed beta-sub input — DOM structure changed: {probe_attrs}"
        )
        # The checkbox VALUE must stay checked — user's prior
        # sub-flag selection is preserved.
        assert 'data-sub-checked="true"' in probe_attrs, (
            f"sub-row checkbox flipped off — should stay checked: {probe_attrs}"
        )
        # Input must be disabled so the user can't fight the master gate.
        assert 'data-sub-disabled="true"' in probe_attrs, (
            f"sub-row input should be disabled when master off: {probe_attrs}"
        )


class TestCodeModeNesting:
    """JSDOM coverage for code-mode sub-numerics nested under
    enable_code_mode in the Beta section."""

    def _payloads(self, master_on: bool, code_mode_on: bool) -> dict[str, dict]:
        """Build /api/settings/features + /api/settings/advanced
        responses for the two-gate matrix."""
        return {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": {
                            "value": master_on,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_BETA_FEATURES",
                        },
                        "enable_code_mode": {
                            "value": code_mode_on,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_CODE_MODE",
                        },
                    },
                    "beta_sub_flags": ["enable_code_mode"],
                },
            },
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "code_mode_max_duration",
                            "env_var": "CODE_MODE_MAX_DURATION",
                            "value": 30.0,
                            "type": "float",
                            "section": "beta_codemode",
                            "origin": "default",
                            "editable": True,
                            "min": 1.0,
                            "max": 300.0,
                        }
                    ]
                },
            },
        }

    def test_codemode_subrows_dimmed_when_master_off(
        self, settings_script: str
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payloads(master_on=False, code_mode_on=True),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)

        cm_rows = re.findall(
            r'<div[^>]*class="[^"]*codemode-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert cm_rows, f"expected codemode-sub row in DOM; tail: {result.dom[-2000:]}"
        # All code-mode-sub rows must be dimmed when master is off.
        for row in cm_rows:
            assert "dimmed" in row, (
                f"expected dimmed on codemode-sub row with master off: {row}"
            )

    def test_codemode_subrows_dimmed_when_code_mode_off(
        self, settings_script: str
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payloads(master_on=True, code_mode_on=False),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)

        cm_rows = re.findall(
            r'<div[^>]*class="[^"]*codemode-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert cm_rows, "expected codemode-sub row"
        for row in cm_rows:
            assert "dimmed" in row, (
                f"expected dimmed on codemode-sub row with code_mode off: {row}"
            )

    def test_codemode_subrows_enabled_when_both_gates_on(
        self, settings_script: str
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payloads(master_on=True, code_mode_on=True),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)

        cm_rows = re.findall(
            r'<div[^>]*class="[^"]*codemode-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert cm_rows, "expected codemode-sub row"
        for row in cm_rows:
            assert "dimmed" not in row, f"unexpected dimmed when both gates on: {row}"


class TestYamlPackagesSubFlagNesting:
    """JSDOM coverage for the 3 yaml-packages sub-rows nested under
    enable_yaml_config_editing. Same dim-on-parent-off pattern as the
    code-mode sub-numerics but with bool sub-toggles instead of int
    bounds."""

    def _payload(self, master_on: bool, parent_on: bool) -> dict[str, dict]:
        flag = lambda name, value, env: {  # noqa: E731
            "value": value,
            "origin": "default",
            "editable": True,
            "type": "bool",
            "env_var": env,
        }
        return {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": flag(
                            "enable_beta_features", master_on, "ENABLE_BETA_FEATURES"
                        ),
                        "enable_yaml_config_editing": flag(
                            "enable_yaml_config_editing",
                            parent_on,
                            "ENABLE_YAML_CONFIG_EDITING",
                        ),
                        "enable_yaml_packages_automation": flag(
                            "enable_yaml_packages_automation",
                            False,
                            "ENABLE_YAML_PACKAGES_AUTOMATION",
                        ),
                        "enable_yaml_packages_script": flag(
                            "enable_yaml_packages_script",
                            False,
                            "ENABLE_YAML_PACKAGES_SCRIPT",
                        ),
                        "enable_yaml_packages_scene": flag(
                            "enable_yaml_packages_scene",
                            False,
                            "ENABLE_YAML_PACKAGES_SCENE",
                        ),
                    },
                    # The backend now sends the per-key flags in
                    # beta_sub_flags (they're in BETA_FEATURE_FIELDS); the
                    # main render pass still skips them via
                    # YAML_PACKAGES_SUB_FLAGS, so they render nested rather
                    # than as top-level beta-sub rows.
                    "beta_sub_flags": [
                        "enable_yaml_config_editing",
                        "enable_yaml_packages_automation",
                        "enable_yaml_packages_script",
                        "enable_yaml_packages_scene",
                    ],
                },
            },
        }

    def test_three_sub_rows_render(self, settings_script: str) -> None:
        """All 3 yaml-packages sub-rows must render even when the
        parent is off — the user needs to see what they CAN enable
        once they flip the parent on."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(master_on=True, parent_on=False),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)
        rows = re.findall(
            r'<div[^>]*class="[^"]*yaml-packages-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert len(rows) == 3, (
            f"expected 3 yaml-packages-sub rows, got {len(rows)}; tail: "
            f"{result.dom[-2000:]}"
        )

    def test_dimmed_when_parent_off(self, settings_script: str) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(master_on=True, parent_on=False),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)
        rows = re.findall(
            r'<div[^>]*class="[^"]*yaml-packages-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert rows
        for row in rows:
            assert "dimmed" in row, (
                f"expected dimmed on yaml-packages-sub row when parent off: {row}"
            )

    def test_dimmed_when_master_off(self, settings_script: str) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(master_on=False, parent_on=True),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)
        rows = re.findall(
            r'<div[^>]*class="[^"]*yaml-packages-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert rows
        for row in rows:
            assert "dimmed" in row, (
                f"expected dimmed when master off (parent transitively off): {row}"
            )

    def test_enabled_when_both_gates_on(self, settings_script: str) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(master_on=True, parent_on=True),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)
        rows = re.findall(
            r'<div[^>]*class="[^"]*yaml-packages-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert rows
        for row in rows:
            assert "dimmed" not in row, f"unexpected dimmed when both gates on: {row}"

    def test_subrow_input_disabled_when_parent_off(self, settings_script: str) -> None:
        """The ``dimmed`` class is cosmetic — assert the actual <input>
        ``.disabled`` PROPERTY is true when the parent is off, so a dimmed
        sub-row genuinely can't be toggled. (JSDOM serialises attributes, not
        live properties, so probe the property directly.)"""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(master_on=True, parent_on=False),
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              const row = document.querySelector('.yaml-packages-sub');
              const input = row.querySelector('input[type="checkbox"]');
              const probe = document.createElement('div');
              probe.id = 'pkgProbe';
              probe.dataset.disabled = String(input.disabled);
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'id="pkgProbe"[^>]*data-disabled="([^"]*)"', result.dom)
        assert m is not None, f"probe div missing; tail: {result.dom[-1500:]}"
        assert m.group(1) == "true", (
            "sub-row <input> must be .disabled when the parent is off"
        )

    def test_subrow_input_enabled_when_both_gates_on(
        self, settings_script: str
    ) -> None:
        """With master + parent both on, the sub-row <input> is actually
        interactive (``.disabled === false``)."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(master_on=True, parent_on=True),
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              const row = document.querySelector('.yaml-packages-sub');
              const input = row.querySelector('input[type="checkbox"]');
              const probe = document.createElement('div');
              probe.id = 'pkgProbe';
              probe.dataset.disabled = String(input.disabled);
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'id="pkgProbe"[^>]*data-disabled="([^"]*)"', result.dom)
        assert m is not None, f"probe div missing; tail: {result.dom[-1500:]}"
        assert m.group(1) == "false", (
            "sub-row <input> must be interactive when both gates are on"
        )


class TestStrictMandatoryBpsSubFlagNesting:
    """JSDOM coverage for the strict-mode sub-row nested under
    enable_mandatory_bps (issue #1779). Non-beta: the only gate is the
    parent toggle (no beta master), so the dim/disable pattern keys off
    parentOn alone."""

    def _payload(self, parent_on: bool) -> dict[str, dict]:
        flag = lambda name, value, env: {  # noqa: E731
            "value": value,
            "origin": "default",
            "editable": True,
            "type": "bool",
            "env_var": env,
        }
        return {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_mandatory_bps": flag(
                            "enable_mandatory_bps",
                            parent_on,
                            "ENABLE_MANDATORY_BPS",
                        ),
                        "enable_strict_mandatory_bps": flag(
                            "enable_strict_mandatory_bps",
                            False,
                            "ENABLE_STRICT_MANDATORY_BPS",
                        ),
                    },
                    # Strict mode is NON-beta, so it is absent from
                    # beta_sub_flags; renderSubFlagRows nests it
                    # under enable_mandatory_bps instead.
                    "beta_sub_flags": [],
                },
            },
        }

    def test_sub_row_renders_even_when_parent_off(self, settings_script: str) -> None:
        """The strict sub-row renders even when the parent is off — the user
        needs to see what they CAN enable once they flip the parent on."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(parent_on=False),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)
        rows = re.findall(
            r'<div[^>]*class="[^"]*mandatory-bps-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert len(rows) == 1, (
            f"expected 1 mandatory-bps-sub row, got {len(rows)}; tail: "
            f"{result.dom[-2000:]}"
        )

    def test_dimmed_when_parent_off(self, settings_script: str) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(parent_on=False),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)
        rows = re.findall(
            r'<div[^>]*class="[^"]*mandatory-bps-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert rows
        for row in rows:
            assert "dimmed" in row, (
                f"expected dimmed on mandatory-bps-sub row when parent off: {row}"
            )

    def test_enabled_when_parent_on(self, settings_script: str) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(parent_on=True),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)
        rows = re.findall(
            r'<div[^>]*class="[^"]*mandatory-bps-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert rows
        for row in rows:
            assert "dimmed" not in row, f"unexpected dimmed when parent on: {row}"

    def test_subrow_input_disabled_when_parent_off(self, settings_script: str) -> None:
        """The ``dimmed`` class is cosmetic — assert the actual <input>
        ``.disabled`` PROPERTY is true when the parent is off, so a dimmed
        sub-row genuinely can't be toggled."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(parent_on=False),
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              const row = document.querySelector('.mandatory-bps-sub');
              const input = row.querySelector('input[type="checkbox"]');
              const probe = document.createElement('div');
              probe.id = 'bpsProbe';
              probe.dataset.disabled = String(input.disabled);
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'id="bpsProbe"[^>]*data-disabled="([^"]*)"', result.dom)
        assert m is not None, f"probe div missing; tail: {result.dom[-1500:]}"
        assert m.group(1) == "true", (
            "sub-row <input> must be .disabled when the parent is off"
        )

    def test_subrow_input_enabled_when_parent_on(self, settings_script: str) -> None:
        """With the parent on, the strict sub-row <input> is actually
        interactive (``.disabled === false``)."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(parent_on=True),
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              const row = document.querySelector('.mandatory-bps-sub');
              const input = row.querySelector('input[type="checkbox"]');
              const probe = document.createElement('div');
              probe.id = 'bpsProbe';
              probe.dataset.disabled = String(input.disabled);
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'id="bpsProbe"[^>]*data-disabled="([^"]*)"', result.dom)
        assert m is not None, f"probe div missing; tail: {result.dom[-1500:]}"
        assert m.group(1) == "false", (
            "sub-row <input> must be interactive when the parent is on"
        )

    def test_parent_flip_live_rerenders_subrow(self, settings_script: str) -> None:
        """Render with the parent ON, then flip enable_mandatory_bps OFF via a
        DOM change event: the strict sub-row must dim + its <input> disable
        synchronously (the parent's change handler mutates the live cache and
        re-renders), and flipping the parent back ON must re-enable it. Guards
        the live-toggle UX that a page reload would otherwise mask."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payload(parent_on=True),
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              const subState = () => {
                const row = document.querySelector('.mandatory-bps-sub');
                const input = row.querySelector('input[type="checkbox"]');
                return { dimmed: row.className.includes('dimmed'),
                         disabled: input.disabled };
              };
              const flipParent = (checked) => {
                const parent = document.querySelector(
                  'input[name="feature:enable_mandatory_bps"]');
                parent.checked = checked;
                parent.dispatchEvent(new Event('change'));
              };
              const initial = subState();
              flipParent(false);
              const afterOff = subState();
              flipParent(true);
              const afterOn = subState();
              const probe = document.createElement('div');
              probe.id = 'bpsLiveProbe';
              probe.dataset.initialDimmed = String(initial.dimmed);
              probe.dataset.initialDisabled = String(initial.disabled);
              probe.dataset.offDimmed = String(afterOff.dimmed);
              probe.dataset.offDisabled = String(afterOff.disabled);
              probe.dataset.onDimmed = String(afterOn.dimmed);
              probe.dataset.onDisabled = String(afterOn.disabled);
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)

        def _probe(attr: str) -> str:
            m = re.search(rf'id="bpsLiveProbe"[^>]*{attr}="([^"]*)"', result.dom)
            assert m is not None, (
                f"probe attr {attr} missing; tail: {result.dom[-1500:]}"
            )
            return m.group(1)

        # Baseline: parent ON → sub-row live and interactive.
        assert _probe("data-initial-dimmed") == "false"
        assert _probe("data-initial-disabled") == "false"
        # Parent flipped OFF → sub-row dims and disables without a reload.
        assert _probe("data-off-dimmed") == "true", (
            "strict sub-row must dim when the parent is flipped off live"
        )
        assert _probe("data-off-disabled") == "true", (
            "strict sub-row <input> must disable when the parent is flipped off live"
        )
        # Parent flipped back ON → sub-row re-enables.
        assert _probe("data-on-dimmed") == "false", (
            "strict sub-row must un-dim when the parent is flipped back on"
        )
        assert _probe("data-on-disabled") == "false", (
            "strict sub-row <input> must re-enable when the parent is flipped back on"
        )


class TestReadOnlyModeToggle:
    """Read Only Mode (#1569): the Tools-tab toggle posts to the
    feature-flag endpoint, and render() forces write-capable tool rows
    off (without rewriting saved states) while exempt mixed read/write
    tools stay interactive."""

    @staticmethod
    def _tools_payload() -> dict:
        return {
            "status": 200,
            "json": {
                "tools": [
                    {
                        "name": "ha_get_history",
                        "title": "Get History",
                        "primary_tag": "Test",
                        "annotations": {"readOnlyHint": True},
                    },
                    {
                        "name": "ha_config_set_scene",
                        "title": "Set Scene",
                        "primary_tag": "Test",
                        "annotations": {"destructiveHint": True},
                    },
                    {
                        "name": "ha_manage_pipeline",
                        "title": "Manage Pipeline",
                        "primary_tag": "Test",
                        "annotations": {"destructiveHint": True},
                    },
                ],
                "states": {},
                "env_pinned": {},
                "read_only_exempt": ["ha_manage_pipeline"],
            },
        }

    def _fetches(self, *, read_only: bool) -> dict:
        return {
            **DEFAULT_FETCHES,
            "/api/settings/tools": self._tools_payload(),
            "/api/settings/features": {
                "status": 200,
                "json": {"flags": {"read_only_mode": {"value": read_only}}},
            },
        }

    @staticmethod
    def _input_tag(dom: str, tool: str) -> str:
        m = re.search(rf'<input[^>]*name="tool:{tool}:enabled"[^>]*>', dom)
        assert m is not None, (
            f"enabled input for {tool} missing; dom tail: {dom[-1500:]}"
        )
        return m.group(0)

    @staticmethod
    def _posted_read_only_true(result: HarnessResult) -> bool:
        flag_posts = [
            f
            for f in result.fetches
            if f["method"] == "POST" and "/api/settings/features" in f["url"]
        ]
        assert flag_posts, (
            f"expected POST to /api/settings/features; got {result.fetches}"
        )
        for f in flag_posts:
            raw = f.get("body", "")
            if not raw:
                continue
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            flags = body.get("flags") if isinstance(body, dict) else None
            if isinstance(flags, dict) and flags.get("read_only_mode") is True:
                return True
        return False

    @staticmethod
    def _probe_toggle_state() -> str:
        return """
          const cb = document.getElementById('read-only-mode-toggle');
          cb.checked = true;
          cb.dispatchEvent(new Event('change'));
          await new Promise(r => setTimeout(r, 80));
          const probe = document.createElement('div');
          probe.id = '__ro_toggle_probe';
          probe.dataset.checked = String(
            document.getElementById('read-only-mode-toggle').checked);
          document.body.appendChild(probe);
        """

    @staticmethod
    def _probe_checked(dom: str) -> str:
        m = re.search(r'<div[^>]*id="__ro_toggle_probe"[^>]*>', dom)
        assert m is not None, f"toggle probe missing; dom tail: {dom[-1500:]}"
        return m.group(0)

    def test_toggle_change_posts_to_features_endpoint(
        self, settings_script: str
    ) -> None:
        """Flipping the Tools-tab toggle must POST
        ``{flags: {read_only_mode: true}}`` to /api/settings/features —
        the same endpoint and shape as every other feature flag — and,
        when the follow-up features GET confirms the new value, the
        checkbox stays checked.

        ``responses`` sequences the three same-URL hits: init GET (off),
        save POST (ok), re-read GET (on).
        """
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map={
                **DEFAULT_FETCHES,
                "/api/settings/features": {
                    "responses": [
                        {"status": 200, "json": {"flags": {}}},
                        {"status": 200, "json": {"restart_required": True}},
                        {
                            "status": 200,
                            "json": {"flags": {"read_only_mode": {"value": True}}},
                        },
                    ]
                },
            },
            invoke=self._probe_toggle_state(),
        )
        _assert_clean_init(result)
        assert self._posted_read_only_true(result), (
            f"no POST carried flags.read_only_mode=true: {result.fetches}"
        )
        probe = self._probe_checked(result.dom)
        assert 'data-checked="true"' in probe, (
            f"toggle must stay checked when the server confirms on: {probe}"
        )

    def test_toggle_reverts_to_previous_when_save_fails(
        self, settings_script: str
    ) -> None:
        """A non-200 features POST is a failed save — the checkbox must
        revert to its pre-flip (off) value rather than lie about a
        persisted change. ``responses``: init GET (off), save POST (500)."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map={
                **DEFAULT_FETCHES,
                "/api/settings/features": {
                    "responses": [
                        {"status": 200, "json": {"flags": {}}},
                        {"status": 500, "json": {"error": {"message": "boom"}}},
                    ]
                },
            },
            invoke=self._probe_toggle_state(),
        )
        _assert_clean_init(result)
        probe = self._probe_checked(result.dom)
        assert 'data-checked="false"' in probe, (
            f"toggle must revert to previous (off) on save failure: {probe}"
        )

    def test_render_forces_write_tools_off_when_on(self, settings_script: str) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._fetches(read_only=True),
            invoke="await new Promise(r => setTimeout(r, 250));",
        )
        _assert_clean_init(result)
        write_tag = self._input_tag(result.dom, "ha_config_set_scene")
        assert "disabled" in write_tag, f"write tool must be locked: {write_tag}"
        assert "checked" not in write_tag, f"write tool must render off: {write_tag}"
        read_tag = self._input_tag(result.dom, "ha_get_history")
        assert "checked" in read_tag, f"read tool must stay on: {read_tag}"
        assert "disabled" not in read_tag, f"read tool must stay live: {read_tag}"
        exempt_tag = self._input_tag(result.dom, "ha_manage_pipeline")
        assert "checked" in exempt_tag, f"exempt tool must stay on: {exempt_tag}"
        assert "disabled" not in exempt_tag, (
            f"exempt tool toggle must stay live: {exempt_tag}"
        )
        assert "Read Only Mode is on; write tools are disabled" in result.dom
        assert "write operations of this tool are blocked" in result.dom

    def test_render_leaves_tools_alone_when_off(self, settings_script: str) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._fetches(read_only=False),
            invoke="await new Promise(r => setTimeout(r, 250));",
        )
        _assert_clean_init(result)
        write_tag = self._input_tag(result.dom, "ha_config_set_scene")
        assert "checked" in write_tag, (
            f"write tool defaults on when mode off: {write_tag}"
        )
        assert "disabled" not in write_tag, (
            f"write tool stays interactive when mode off: {write_tag}"
        )
        assert "Read Only Mode is on; write tools are disabled" not in result.dom

    def test_group_master_click_does_not_touch_write_tool_when_on(
        self, settings_script: str
    ) -> None:
        """With Read Only Mode on, the group master switch must exclude
        write-capable rows: clicking it never enables/disables the forced
        -off write tool, and that tool's row input stays unchecked +
        disabled. The save POST (if any) must not flip the write tool."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._fetches(read_only=True),
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              const master = document.querySelector(
                'input[name="tool-group:Test"]');
              if (master) {
                master.checked = !master.checked;
                master.dispatchEvent(new Event('change'));
              }
              // saveConfig is debounced 800ms; advance past it.
              await new Promise(r => setTimeout(r, 1000));
            """,
        )
        _assert_clean_init(result)
        # The write-tool row input stays off + locked under the mode.
        write_tag = self._input_tag(result.dom, "ha_config_set_scene")
        assert "checked" not in write_tag, (
            f"write tool must stay off after group-master click: {write_tag}"
        )
        assert "disabled" in write_tag, (
            f"write tool must stay locked after group-master click: {write_tag}"
        )
        # No tools-save POST may carry a state for the write tool.
        tool_posts = [
            f
            for f in result.fetches
            if f["method"] == "POST" and "/api/settings/tools" in f["url"]
        ]
        for f in tool_posts:
            assert "ha_config_set_scene" not in f.get("body", ""), (
                f"group-master save flipped the forced-off write tool: {f}"
            )

    def test_double_flip_restores_pinned_row_without_tools_save(
        self, settings_script: str
    ) -> None:
        """A pinned write tool forced off while the mode is on must render
        back as checked+pinned once the mode goes off again — and the
        re-render must NOT issue a tools-save POST (saved state is never
        rewritten, only visually overridden)."""
        tools_payload = {
            "status": 200,
            "json": {
                "tools": [
                    {
                        "name": "ha_config_set_scene",
                        "title": "Set Scene",
                        "primary_tag": "Test",
                        "annotations": {"destructiveHint": True},
                    },
                ],
                "states": {"ha_config_set_scene": "pinned"},
                "env_pinned": {},
                "read_only_exempt": [],
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map={
                **DEFAULT_FETCHES,
                "/api/settings/tools": tools_payload,
                "/api/settings/features": {
                    # init GET (on), then re-read GET (off) after the flip.
                    "responses": [
                        {
                            "status": 200,
                            "json": {"flags": {"read_only_mode": {"value": True}}},
                        },
                        {
                            "status": 200,
                            "json": {"restart_required": True},
                        },
                        {
                            "status": 200,
                            "json": {"flags": {"read_only_mode": {"value": False}}},
                        },
                    ]
                },
            },
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              // Mode starts on → the pinned write row is forced off.
              const cb = document.getElementById('read-only-mode-toggle');
              cb.checked = false;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 1000));
            """,
        )
        _assert_clean_init(result)
        scene_enabled = self._input_tag(result.dom, "ha_config_set_scene")
        assert "checked" in scene_enabled, (
            f"pinned row must render checked once mode is off again: {scene_enabled}"
        )
        pinned_tag = re.search(
            r'<input[^>]*name="tool:ha_config_set_scene:pinned"[^>]*>', result.dom
        )
        assert pinned_tag is not None and "checked" in pinned_tag.group(0), (
            f"row must render pinned again once mode is off: {result.dom[-1500:]}"
        )
        tool_posts = [
            f
            for f in result.fetches
            if f["method"] == "POST" and "/api/settings/tools" in f["url"]
        ]
        assert not tool_posts, (
            f"double-flip must not POST a tools-save (state is only visually "
            f"overridden): {tool_posts}"
        )

    def test_unknown_state_notice_shown_when_features_fetch_fails(
        self, settings_script: str
    ) -> None:
        """When /api/settings/features fails (503), readOnlyState is
        unknown and the #roUnknownNotice element must gain the ``show``
        class so the user knows the view may not match the server (#1569,
        item 6)."""
        dom = MIN_DOM.replace(
            "</body>",
            '<div class="pin-notice" id="roUnknownNotice"></div></body>',
        )
        result = run_script(
            settings_script,
            initial_html=dom,
            fetch_map={
                **DEFAULT_FETCHES,
                "/api/settings/features": {"status": 503, "json": {}},
            },
            invoke="""
              await new Promise(r => setTimeout(r, 250));
              const notice = document.getElementById('roUnknownNotice');
              const probe = document.createElement('div');
              probe.id = '__ro_notice_probe';
              probe.dataset.shown = String(
                !!notice && notice.classList.contains('show'));
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        m = re.search(r'<div[^>]*id="__ro_notice_probe"[^>]*>', result.dom)
        assert m is not None, f"notice probe missing; dom tail: {result.dom[-1500:]}"
        assert 'data-shown="true"' in m.group(0), (
            f"roUnknownNotice must show when features fetch fails: {m.group(0)}"
        )


class TestTablistAndStatusBehavior:
    """Behavioural coverage for the #1596 a11y JS — the ARIA tablist
    (aria-selected + roving tabindex sync, keyboard navigation) and the
    failure-path role=alert toggling. The static-markup tests in
    test_settings_ui.py only assert the affordances exist; these drive the
    handlers and assert they actually behave.
    """

    # Two side-effect-free tabs (server + accessibility have no data-load
    # branch in activateTab), so switching between them touches no network.
    _TAB_STRIP = (
        '<div class="tabs" role="tablist" aria-label="Settings sections">'
        '<button class="tab active" data-panel="server" role="tab" id="tab-server"'
        ' aria-controls="panel-server" aria-selected="true">Server</button>'
        '<button class="tab" data-panel="accessibility" role="tab"'
        ' id="tab-accessibility" aria-controls="panel-accessibility"'
        ' aria-selected="false" tabindex="-1">Accessibility</button>'
        "</div>"
    )

    def _dom_with_tabs(self) -> str:
        return MIN_DOM.replace("</body>", self._TAB_STRIP + "\n</body>")

    def test_failure_announces_via_assertive_toast(self, settings_script: str) -> None:
        """A terminal failure is announced assertively through the toast
        (role=alert / aria-live=assertive), NOT the #status region: updateStatus
        routes saved/error outcomes solely to showToast so screen readers hear
        them exactly once (no #status + toast double-announce). #status stays
        role=status / aria-live=polite for the transient progress text
        ("Saving…"/"Loading…") that never toasts."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=DEFAULT_FETCHES,
            invoke="""
              updateStatus('Save failed!', false, true);
              const t = document.querySelector('#ha-toast-region .ha-toast');
              const tr = t ? t.getAttribute('role') : '';
              const tl = t ? t.getAttribute('aria-live') : '';
              updateStatus('Loading...', false, false);
              const sr = document.getElementById('status').getAttribute('role');
              const sl = document.getElementById('status').getAttribute('aria-live');
              document.body.setAttribute('data-test',
                `fail=${tr}/${tl} progress=${sr}/${sl}`);
            """,
        )
        _assert_clean_init(result)
        assert (
            'data-test="fail=alert/assertive progress=status/polite"' in result.dom
        ), (
            "terminal failure should announce via the toast (assertive) and leave "
            f"#status polite for progress; dom tail: {result.dom[-800:]}"
        )

    def test_tab_click_syncs_aria_selected_and_roving_tabindex(
        self, settings_script: str
    ) -> None:
        """Clicking a tab must mark exactly that tab aria-selected=true with
        tabIndex 0, and demote the previously selected tab to
        aria-selected=false / tabIndex -1 (WAI-ARIA APG roving tabindex)."""
        result = run_script(
            settings_script,
            initial_html=self._dom_with_tabs(),
            fetch_map=DEFAULT_FETCHES,
            invoke="""
              document.getElementById('tab-accessibility')
                .dispatchEvent(new MouseEvent('click', {bubbles: true}));
              const sel = document.querySelectorAll('.tab[aria-selected="true"]');
              document.body.setAttribute('data-test',
                'selcount=' + sel.length +
                ' active=' + (sel[0] && sel[0].dataset.panel) +
                ' newidx=' + document.getElementById('tab-accessibility').tabIndex +
                ' oldidx=' + document.getElementById('tab-server').tabIndex);
            """,
        )
        _assert_clean_init(result)
        assert (
            'data-test="selcount=1 active=accessibility newidx=0 oldidx=-1"'
            in result.dom
        ), f"activateTab aria/roving-tabindex sync wrong; dom tail: {result.dom[-800:]}"

    def test_tablist_keyboard_navigation(self, settings_script: str) -> None:
        """Left/Right move + activate the adjacent tab (wrapping), Home/End
        jump to the ends — the keydown handler bound on the tablist."""
        result = run_script(
            settings_script,
            initial_html=self._dom_with_tabs(),
            fetch_map=DEFAULT_FETCHES,
            invoke="""
              const tablist = document.querySelector('.tabs[role="tablist"]');
              const active = () =>
                document.querySelector('.tab[aria-selected="true"]').dataset.panel;
              const press = (key) => {
                tablist.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true}));
                return active();
              };
              document.getElementById('tab-server').focus();
              const right = press('ArrowRight');     // server -> accessibility
              document.getElementById('tab-server').focus();
              const wrap = press('ArrowLeft');        // server -> accessibility (wrap)
              const end = press('End');               // -> accessibility (last)
              const home = press('Home');             // -> server (first)
              document.body.setAttribute('data-test',
                `right=${right} wrap=${wrap} end=${end} home=${home}`);
            """,
        )
        _assert_clean_init(result)
        assert (
            'data-test="right=accessibility wrap=accessibility'
            ' end=accessibility home=server"' in result.dom
        ), f"tablist keyboard nav wrong; dom tail: {result.dom[-800:]}"


class TestToastFeedback:
    """Save/load feedback surfaces as an HA-style bottom toast (showToast),
    not the old persistent grey status pill. Terminal outcomes (saved / error)
    toast; transient progress states ("Saving…", "Loading…") do not; only one
    toast shows at a time (replace-on-new, so rapid toggles don't stack); and
    errors carry role=alert plus a dismiss button while successes don't."""

    def test_saved_outcome_shows_toast_without_dismiss(
        self, settings_script: str
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=DEFAULT_FETCHES,
            invoke="""
              updateStatus('Saved.', true, false);
              const t = document.querySelector('#ha-toast-region .ha-toast');
              document.body.setAttribute('data-test',
                `present=${!!t} role=${t ? t.getAttribute('role') : ''} `
                + `live=${t ? t.getAttribute('aria-live') : ''} `
                + `msg=${t ? t.querySelector('.ha-toast-msg').textContent : ''} `
                + `dismiss=${!!(t && t.querySelector('.ha-toast-dismiss'))}`);
            """,
        )
        _assert_clean_init(result)
        assert "present=true" in result.dom, result.dom[-600:]
        assert "role=status" in result.dom
        assert "live=polite" in result.dom
        assert "msg=Saved." in result.dom
        assert "dismiss=false" in result.dom

    def test_progress_state_suppresses_toast(self, settings_script: str) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=DEFAULT_FETCHES,
            invoke="""
              updateStatus('Saving...', false, false);
              const n = document.querySelectorAll('#ha-toast-region .ha-toast').length;
              document.body.setAttribute('data-test', `count=${n}`);
            """,
        )
        _assert_clean_init(result)
        assert 'data-test="count=0"' in result.dom, result.dom[-600:]

    def test_error_outcome_toasts_with_alert_role_and_dismiss(
        self, settings_script: str
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=DEFAULT_FETCHES,
            invoke="""
              updateStatus('Save failed!', false, true);
              const t = document.querySelector('#ha-toast-region .ha-toast');
              document.body.setAttribute('data-test',
                `err=${t ? t.classList.contains('ha-toast-error') : false} `
                + `role=${t ? t.getAttribute('role') : ''} `
                + `live=${t ? t.getAttribute('aria-live') : ''} `
                + `dismiss=${!!(t && t.querySelector('.ha-toast-dismiss'))}`);
            """,
        )
        _assert_clean_init(result)
        assert "err=true" in result.dom, result.dom[-600:]
        assert "role=alert" in result.dom
        assert "live=assertive" in result.dom
        assert "dismiss=true" in result.dom

    def test_rapid_toasts_replace_and_do_not_stack(self, settings_script: str) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=DEFAULT_FETCHES,
            invoke="""
              showToast('first');
              showToast('second');
              showToast('third');
              const toasts = document.querySelectorAll('#ha-toast-region .ha-toast');
              const last = toasts.length
                ? toasts[toasts.length - 1].querySelector('.ha-toast-msg').textContent
                : '';
              document.body.setAttribute('data-test', `count=${toasts.length} last=${last}`);
            """,
        )
        _assert_clean_init(result)
        assert 'data-test="count=1 last=third"' in result.dom, result.dom[-600:]

    def test_reused_toast_survives_pending_leave_removal(
        self, settings_script: str
    ) -> None:
        """A toast reused (replace-on-new) while a prior toast is mid-leave must
        not be yanked from the DOM by the earlier 200ms removal timer. Settle
        past the 200ms leave window but before the 4s auto-dismiss: the new
        toast must still be present, proving the pending removal was cancelled."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            # Populated tools so init renders silently (an empty list would fire a
            # "No tools found" error toast during settle and clobber our toast).
            fetch_map={
                **DEFAULT_FETCHES,
                "/api/settings/tools": {
                    "status": 200,
                    "json": {
                        "tools": [
                            {
                                "name": "ha_get_state",
                                "title": "Get State",
                                "primary_tag": "Test",
                                "annotations": {"readOnlyHint": True},
                            },
                        ],
                        "states": {"ha_get_state": "enabled"},
                        "env_pinned": {},
                        "read_only_exempt": [],
                    },
                },
            },
            # 300ms > the 200ms leave-removal, < the 4000ms auto-dismiss.
            settle_ms=300,
            invoke="""
              showToast('first');
              const t = document.querySelector('#ha-toast-region .ha-toast');
              _removeToast(t);     // start leave: schedules DOM removal in 200ms
              showToast('second'); // reuse element; must cancel that removal
            """,
        )
        _assert_clean_init(result)
        # Without the fix the stale 200ms timer removes the reused toast; with it
        # the toast survives with the new message.
        assert '<div class="ha-toast' in result.dom, (
            f"reused toast was removed by stale timer: {result.dom[-600:]}"
        )
        assert ">second<" in result.dom, result.dom[-600:]


def _probe(result: HarnessResult, attr: str) -> str | None:
    """Read a ``data-<attr>`` value stamped onto an element in result.dom.

    The JS behaviour tests stash assertion data into data-* attributes
    (live, before the fake clock advances past toast auto-dismiss). This
    pulls the value back out without depending on attribute order.
    """
    m = re.search(rf'data-{attr}="([^"]*)"', result.dom)
    return m.group(1) if m else None


class TestModalFocusTrap:
    """The snapshot modal follows the WAI-ARIA APG dialog pattern:
    focus moves into the dialog on open, Tab is trapped inside it, Escape
    closes it, and focus returns to the opener on close. Regression guard
    for the focus-management handlers added in showModal/closeModal.
    """

    def test_focus_moves_traps_and_restores(self, settings_script: str) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=DEFAULT_FETCHES,
            invoke="""
              await new Promise(r => setTimeout(r, 50));
              // Focus a page element so closeModal has an opener to restore to.
              const opener = document.getElementById('search');
              opener.focus();
              const openerId = document.activeElement ? document.activeElement.id : '';
              // Two focusable controls in the body so the trap has a range.
              showModal('T', '<button id="b1">one</button><button id="b2">two</button>');
              const backdrop = document.getElementById('modalBackdrop');
              const afterOpen = document.activeElement ? document.activeElement.id : '';
              const shownOpen = backdrop.classList.contains('show');
              // Tab at the last focusable wraps to the first (#modalClose).
              document.getElementById('b2').focus();
              backdrop.dispatchEvent(
                new window.KeyboardEvent('keydown', {key: 'Tab', bubbles: true}));
              const afterTab = document.activeElement ? document.activeElement.id : '';
              // Shift+Tab at the first focusable wraps to the last (#b2).
              document.getElementById('modalClose').focus();
              backdrop.dispatchEvent(
                new window.KeyboardEvent(
                  'keydown', {key: 'Tab', shiftKey: true, bubbles: true}));
              const afterShiftTab = document.activeElement ? document.activeElement.id : '';
              // Escape closes the dialog and restores focus to the opener.
              backdrop.dispatchEvent(
                new window.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
              const shownEsc = backdrop.classList.contains('show');
              const afterClose = document.activeElement ? document.activeElement.id : '';
              const probe = document.createElement('div');
              probe.id = '__modal_probe';
              probe.dataset.opener = openerId;
              probe.dataset.afterOpen = afterOpen;
              probe.dataset.shownOpen = String(shownOpen);
              probe.dataset.afterTab = afterTab;
              probe.dataset.afterShiftTab = afterShiftTab;
              probe.dataset.shownEsc = String(shownEsc);
              probe.dataset.afterClose = afterClose;
              document.body.appendChild(probe);
            """,
        )
        _assert_clean_init(result)
        assert _probe(result, "opener") == "search", "opener was not focused first"
        assert _probe(result, "shown-open") == "true", "modal did not open"
        assert _probe(result, "after-open") == "modalClose", (
            "showModal did not move focus to the close button"
        )
        assert _probe(result, "after-tab") == "modalClose", (
            "Tab at the last focusable did not wrap to the first"
        )
        assert _probe(result, "after-shift-tab") == "b2", (
            "Shift+Tab at the first focusable did not wrap to the last"
        )
        assert _probe(result, "shown-esc") == "false", "Escape did not close the dialog"
        assert _probe(result, "after-close") == "search", (
            "closeModal did not restore focus to the opener"
        )


# Advanced field shared by the reload-guard tests below.
_ADV_GUARD_FIELD = {
    "field": "fuzzy_threshold",
    "env_var": "FUZZY_THRESHOLD",
    "value": 60,
    "type": "int",
    "section": "search",
    "origin": "default",
    "editable": True,
}


def _adv_method_counts(result: HarnessResult) -> tuple[int, int]:
    """Return ``(get_count, post_count)`` for /api/settings/advanced."""
    gets = sum(
        1
        for f in result.fetches
        if "/api/settings/advanced" in f["url"] and f["method"] == "GET"
    )
    posts = sum(
        1
        for f in result.fetches
        if "/api/settings/advanced" in f["url"] and f["method"] == "POST"
    )
    return gets, posts


class TestAdvancedAutoSaveReloadGuard:
    """saveAdvancedSettings() rebuilds the panel (innerHTML='') after a save
    only when it is SAFE: skipped while another edit is still pending or the
    user is typing in an advanced field, otherwise the reload GET fires. The
    skip prevents a focus-stealing / typing-discarding rebuild mid-edit.
    """

    def test_reload_skipped_when_another_field_still_dirty(
        self, settings_script: str
    ) -> None:
        """An edit that lands while the POST is in flight stays in
        _advancedDirty after the saved batch is cleared, so the post-save
        reload must NOT fire (it would discard the pending edit)."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [_ADV_GUARD_FIELD], "is_addon": False},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));  // init load (GET #1)
              _advancedDirty['fuzzy_threshold'] = 5;
              const p = saveAdvancedSettings();   // restartFields=['fuzzy_threshold']
              // Arrives during the in-flight POST -> not in the saved batch.
              _advancedDirty['log_level'] = 'DEBUG';
              await p;
              document.body.setAttribute(
                'data-remaining', Object.keys(_advancedDirty).sort().join(','));
            """,
        )
        _assert_clean_init(result)
        assert _probe(result, "remaining") == "log_level", (
            "the in-flight edit was not preserved in _advancedDirty"
        )
        gets, posts = _adv_method_counts(result)
        assert posts == 1, f"expected exactly one save POST, got {posts}"
        assert gets == 1, (
            f"reload GET should be skipped while an edit is pending; "
            f"got {gets} GETs (init load is the only expected one)"
        )

    def test_reload_skipped_when_focus_in_advanced_field(
        self, settings_script: str
    ) -> None:
        """With focus inside a [data-adv-field] the reload must NOT fire — a
        rebuild would yank focus and discard in-progress typing."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [_ADV_GUARD_FIELD], "is_addon": False},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));  // init load (GET #1)
              const input = document.querySelector('[data-adv-field="fuzzy_threshold"]');
              input.value = '5';
              input.dispatchEvent(new Event('change'));  // commit + arm debounce
              input.focus();                             // focus inside an adv field
              await new Promise(r => setTimeout(r, 1500));  // debounce fires the save
              document.body.setAttribute(
                'data-active',
                document.activeElement
                  ? (document.activeElement.getAttribute('data-adv-field') || 'none')
                  : 'none');
            """,
        )
        _assert_clean_init(result)
        assert _probe(result, "active") == "fuzzy_threshold", (
            "test precondition: focus should remain in the advanced field"
        )
        gets, posts = _adv_method_counts(result)
        assert posts == 1, f"expected exactly one save POST, got {posts}"
        assert gets == 1, (
            f"reload GET should be skipped while a field is focused; got {gets}"
        )

    def test_reload_fires_when_clean_and_unfocused(self, settings_script: str) -> None:
        """Control: with _advancedDirty empty after the save and focus NOT in
        an advanced field, the post-save reload GET DOES fire."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [_ADV_GUARD_FIELD], "is_addon": False},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));  // init load (GET #1)
              const input = document.querySelector('[data-adv-field="fuzzy_threshold"]');
              input.value = '5';
              input.dispatchEvent(new Event('change'));  // commit + arm debounce
              // Ensure focus is not inside any advanced field.
              if (document.activeElement && document.activeElement.blur) {
                document.activeElement.blur();
              }
              await new Promise(r => setTimeout(r, 1500));  // debounce fires the save
            """,
        )
        _assert_clean_init(result)
        gets, posts = _adv_method_counts(result)
        assert posts == 1, f"expected exactly one save POST, got {posts}"
        assert gets == 2, f"expected init load + one post-save reload GET, got {gets}"


class TestLoadPolicyStateKeepsPriorGated:
    """loadPolicyState() keeps the previously-loaded gatedTools when the
    /api/policy/config reload fails, instead of clobbering it to empty —
    so a transient blip can't make the Tools tab falsely claim nothing is
    gated.
    """

    def test_gated_row_survives_failed_policy_reload(
        self, settings_script: str
    ) -> None:
        tool = {
            "name": "ha_get_state",
            "title": "Get State",
            "category": "read",
            "description": "Read a state.",
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [tool],
                    "states": {},
                    "env_pinned": {},
                    "read_only_exempt": [],
                },
            },
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_tool_security_policies": {
                            "value": True,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                        }
                    },
                    "beta_sub_flags": [],
                    "is_addon": False,
                },
            },
            "/api/policy/config": {
                "responses": [
                    {"status": 200, "json": {"rules": [{"tool_name": "ha_get_state"}]}},
                    {"status": 500, "json": {"error": {"message": "boom"}}},
                ]
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));  // first load seeds gatedTools
              const before = document.querySelector('input[name="tool:ha_get_state:gated"]');
              const checkedBefore = !!(before && before.checked);
              await loadPolicyState();   // second load: policy/config 500 -> keep prior
              render();
              const after = document.querySelector('input[name="tool:ha_get_state:gated"]');
              document.body.setAttribute('data-before', String(checkedBefore));
              document.body.setAttribute('data-after', String(!!(after && after.checked)));
              document.body.setAttribute(
                'data-has', String(policyState.gatedTools.has('ha_get_state')));
            """,
        )
        _assert_clean_init(result)
        assert _probe(result, "before") == "true", (
            "precondition: the tool should render gated after the first load"
        )
        assert _probe(result, "has") == "true", (
            "gatedTools was cleared after the failed policy reload"
        )
        assert _probe(result, "after") == "true", (
            "the gated checkbox lost its checked state after the failed reload"
        )


class TestCapabilityBadgeUnknownFallback:
    """A tool whose category is missing/blank or an unrecognised value must
    still render a visible badge (``badge unknown`` with '?' or the escaped
    value) — a destructive tool showing no tier badge would understate risk.
    """

    def test_blank_and_bogus_categories_render_unknown_badge(
        self, settings_script: str
    ) -> None:
        tools = [
            {
                "name": "tool_empty_cat",
                "title": "Empty Cat",
                "category": "",
                "description": "x",
            },
            {
                "name": "tool_bogus_cat",
                "title": "Bogus Cat",
                "category": "weird",
                "description": "y",
            },
        ]
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": tools,
                    "states": {},
                    "env_pinned": {},
                    "read_only_exempt": [],
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const badges = Array.from(document.querySelectorAll('span.badge.unknown'));
              document.body.setAttribute('data-count', String(badges.length));
              document.body.setAttribute(
                'data-texts', badges.map(b => b.textContent).join('|'));
            """,
        )
        _assert_clean_init(result)
        assert _probe(result, "count") == "2", (
            f"expected two unknown-category badges; dom tail: {result.dom[-600:]}"
        )
        texts = _probe(result, "texts") or ""
        assert set(texts.split("|")) == {"?", "weird"}, (
            f"unknown badges should show '?' (blank) and the raw value; got {texts}"
        )


class TestBackupActionErrorToast:
    """A network drop on a destructive backup action surfaces a visible
    error toast instead of silently no-opping (the bare rejection would
    only reach the visually-hidden #status region).
    """

    @pytest.mark.parametrize(
        "act,verb,name,throw_pattern",
        [
            ("restore", "Restore", "snap_a", "/restore"),
            ("delete", "Delete", "snap_b", "/backups/snap_b"),
        ],
    )
    def test_network_error_shows_error_toast(
        self,
        settings_script: str,
        act: str,
        verb: str,
        name: str,
        throw_pattern: str,
    ) -> None:
        fetches = {**DEFAULT_FETCHES, throw_pattern: {"throw": "network down"}}
        invoke = (
            (
                "await new Promise(r => setTimeout(r, 100));\n"
                "await window.backupAction('__ACT__', '__NAME__');\n"
                "const t = document.querySelector('#ha-toast-region .ha-toast');\n"
                "document.body.setAttribute('data-toast',\n"
                "  t ? (t.className + '::' + "
                "(t.querySelector('.ha-toast-msg')?.textContent || '')) : 'NONE');\n"
            )
            .replace("__ACT__", act)
            .replace("__NAME__", name)
        )
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            settle_ms=300,
            invoke=invoke,
        )
        _assert_clean_init(result)
        toast = _probe(result, "toast") or ""
        assert "ha-toast-error" in toast, (
            f"expected an error toast for a failed {act}; got {toast!r}"
        )
        assert verb in toast and name in toast and "failed" in toast, (
            f"error toast missing action/name context; got {toast!r}"
        )


class TestLlmApiToggle:
    """The per-tool "LLM API" toggle (#1745): renders the server-computed
    effective value, a flip lands in the POSTed llm_api overrides, and the
    save messaging branches on restart_required (exposure-only saves apply
    live — no restart banner)."""

    _TOOL: ClassVar[dict] = {
        "name": "ha_get_state",
        "title": "Get State",
        "category": "read",
        "description": "x",
        "tags": ["Entity"],
    }

    def _tools_json(self, llm_effective: bool) -> dict:
        return {
            "tools": [self._TOOL],
            "states": {},
            "env_pinned": {},
            "read_only_exempt": [],
            "llm_api": {"ha_get_state": llm_effective},
            "llm_api_overrides": {},
            # Embedded (custom-component) server: the LLM API toggle renders.
            "llm_api_available": True,
        }

    @pytest.mark.parametrize(
        "shape",
        ["explicit_false", "absent"],
        ids=["explicit_false", "absent_default"],
    )
    def test_toggle_hidden_when_llm_api_unavailable(
        self, settings_script: str, shape: str
    ) -> None:
        """On a non-embedded server (add-on / Docker / standalone) the LLM API
        toggle column must not render — it would be a no-op there. Other toggles
        still render.

        Covers both the shape the server actually sends —
        ``llm_api_available: False`` (``is_embedded()`` on a non-embedded
        server) — and, defensively, an older payload that omits the key
        entirely (the JS ``!!data.llm_api_available`` default hides it too).
        """
        payload = self._tools_json(True)
        if shape == "explicit_false":
            payload["llm_api_available"] = False
        else:
            del payload["llm_api_available"]
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {"status": 200, "json": payload},
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            settle_ms=300,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              document.body.setAttribute('data-llm-present',
                document.querySelector('input[data-field="llm"]') ? 'yes' : 'no');
              document.body.setAttribute('data-enabled-present',
                document.querySelector('input[data-field="enabled"]') ? 'yes' : 'no');
            """,
        )
        _assert_clean_init(result)
        assert _probe(result, "llm-present") == "no", (
            "LLM API toggle must be hidden when the server is not embedded"
        )
        assert _probe(result, "enabled-present") == "yes", (
            "other tool toggles must still render"
        )

    def test_toggle_renders_effective_value(self, settings_script: str) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": self._tools_json(False),
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            settle_ms=300,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const llm = document.querySelector('input[data-field="llm"]');
              document.body.setAttribute('data-llm-checked',
                llm ? String(llm.checked) : 'MISSING');
            """,
        )
        _assert_clean_init(result)
        # Server-computed effective False renders unchecked.
        assert _probe(result, "llm-checked") == "false"

    def test_flip_posts_override_and_live_apply_message(
        self, settings_script: str
    ) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "responses": [
                    {"status": 200, "json": self._tools_json(True)},
                    {
                        "status": 200,
                        "json": {
                            "success": True,
                            "applied": {},
                            "llm_api_applied": {"ha_get_state": False},
                            "mode": "file",
                            "restart_required": False,
                        },
                    },
                ]
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            settle_ms=300,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              document.querySelector('input[data-field="llm"]').click();
              await window.saveConfig();
              const t = document.querySelector('#ha-toast-region .ha-toast');
              document.body.setAttribute('data-toast',
                t ? (t.querySelector('.ha-toast-msg')?.textContent || '') : 'NONE');
            """,
        )
        _assert_clean_init(result)
        posts = [
            f
            for f in result.fetches
            if f["method"] == "POST" and "/api/settings/tools" in f["url"]
        ]
        assert posts, "the toggle flip never saved"
        body = json.loads(posts[-1]["body"])
        # The flip (effective True -> off) lands in the overrides map.
        assert body["llm_api"] == {"ha_get_state": False}
        # Exposure-only saves apply live: no restart messaging.
        toast = _probe(result, "toast") or ""
        assert "next agent message" in toast, toast
        assert "Restart required" not in toast

    def test_states_change_still_shows_restart_message(
        self, settings_script: str
    ) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "responses": [
                    {"status": 200, "json": self._tools_json(True)},
                    {
                        "status": 200,
                        "json": {
                            "success": True,
                            "applied": {"ha_get_state": "disabled"},
                            "llm_api_applied": {},
                            "mode": "file",
                            "restart_required": True,
                        },
                    },
                ]
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            settle_ms=300,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              document.querySelector('input[data-field="enabled"]').click();
              await window.saveConfig();
              const t = document.querySelector('#ha-toast-region .ha-toast');
              document.body.setAttribute('data-toast',
                t ? (t.querySelector('.ha-toast-msg')?.textContent || '') : 'NONE');
            """,
        )
        _assert_clean_init(result)
        toast = _probe(result, "toast") or ""
        assert "Restart required" in toast, toast


class TestSaveConfigStructuredError:
    """A failed tools save surfaces the server's structured error message
    (``Save failed: <message>``) rather than a generic failure string.
    """

    def test_structured_error_message_surfaced(self, settings_script: str) -> None:
        tool = {
            "name": "ha_get_state",
            "title": "Get State",
            "category": "read",
            "description": "x",
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "responses": [
                    {
                        "status": 200,
                        "json": {
                            "tools": [tool],
                            "states": {},
                            "env_pinned": {},
                            "read_only_exempt": [],
                        },
                    },
                    {"status": 400, "json": {"error": {"message": "disk full"}}},
                ]
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            settle_ms=300,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              await window.saveConfig();
              const t = document.querySelector('#ha-toast-region .ha-toast');
              document.body.setAttribute('data-toast',
                t ? (t.querySelector('.ha-toast-msg')?.textContent || '') : 'NONE');
            """,
        )
        _assert_clean_init(result)
        assert _probe(result, "toast") == "Save failed: disk full", (
            f"expected the structured error message; got {_probe(result, 'toast')!r}"
        )


class TestGoogleFontsRemoved:
    """The #1695 redesign dropped the Google Fonts CDN <link>/preconnect in
    favour of the system font stack — keep it out (privacy + offline use).
    """

    def test_settings_html_has_no_google_fonts_or_preconnect(self) -> None:
        from ha_mcp.settings_ui import _SETTINGS_HTML

        assert "fonts.googleapis.com" not in _SETTINGS_HTML
        assert "fonts.gstatic.com" not in _SETTINGS_HTML
        assert "preconnect" not in _SETTINGS_HTML


class TestAriaLabelledbyOnRenderedInputs:
    """Feature-flag, advanced, and backup inputs are associated with their
    visible label via aria-labelledby pointing at the label element's id
    (replacing the duplicated aria-label text).
    """

    def test_feature_advanced_backup_inputs_reference_visible_label(
        self, settings_script: str
    ) -> None:
        feature_flags = {
            "enable_tool_search": {
                "value": False,
                "origin": "default",
                "editable": True,
                "type": "bool",
            }
        }
        adv_field = {
            "field": "fuzzy_threshold",
            "env_var": "FUZZY_THRESHOLD",
            "value": 60,
            "type": "int",
            "section": "search",
            "origin": "default",
            "editable": True,
        }
        backup_field = {
            "field": "enable_auto_backup",
            "value": True,
            "origin": "default",
            "editable": True,
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": feature_flags,
                    "beta_sub_flags": [],
                    "is_addon": False,
                },
            },
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [adv_field], "is_addon": False},
            },
            "/api/settings/backup-config": {
                "status": 200,
                "json": {"fields": [backup_field], "is_addon": False},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              await loadBackupConfig();  // not loaded on init
              const fInput = document.querySelector('input[name="feature:enable_tool_search"]');
              const fLabel = document.getElementById('label-feature-enable_tool_search');
              const aInput = document.querySelector('[data-adv-field="fuzzy_threshold"]');
              const aLabel = document.getElementById('label-adv-fuzzy_threshold');
              const bInput = document.querySelector('input[data-field="enable_auto_backup"]');
              const bLabel = document.getElementById('label-backup-enable_auto_backup');
              document.body.setAttribute('data-feat',
                `${fInput ? fInput.getAttribute('aria-labelledby') : 'noinput'}`
                + `|${fLabel ? fLabel.className : 'nolabel'}`);
              document.body.setAttribute('data-adv',
                `${aInput ? aInput.getAttribute('aria-labelledby') : 'noinput'}`
                + `|${aLabel ? aLabel.className : 'nolabel'}`);
              document.body.setAttribute('data-backup',
                `${bInput ? bInput.getAttribute('aria-labelledby') : 'noinput'}`
                + `|${bLabel ? bLabel.className : 'nolabel'}`);
            """,
        )
        _assert_clean_init(result)
        assert _probe(result, "feat") == "label-feature-enable_tool_search|feature-name"
        assert _probe(result, "adv") == "label-adv-fuzzy_threshold|adv-name"
        assert (
            _probe(result, "backup")
            == "label-backup-enable_auto_backup|backup-field-label"
        )


# ---------------------------------------------------------------------------
# Entity Visibility tab (#1728): load error announcement, save status role,
# and the 409 optimistic-lock flow that must keep the user's unsaved edits.
# ---------------------------------------------------------------------------

# The visibility inputs the load/save handlers read + write, injected into
# MIN_DOM so the handlers find their fields without standing up the full panel.
_VISIBILITY_DOM = MIN_DOM.replace(
    "\n</body>",
    """
  <div id="visibility-load-error" role="alert" aria-live="assertive" style="display:none"></div>
  <span id="visibility-save-status" class="status" role="status" aria-live="polite"></span>
  <input id="visibility-enabled" type="checkbox" />
  <input id="visibility-enforce" type="checkbox" />
  <input id="visibility-restrict-report-issue" type="checkbox" />
  <input id="visibility-cat-diagnostic" type="checkbox" />
  <input id="visibility-cat-config" type="checkbox" />
  <input id="visibility-exclude-hidden" type="checkbox" />
  <input id="visibility-areas" type="text" />
  <input id="visibility-labels" type="text" />
  <input id="visibility-allow-areas" type="text" />
  <input id="visibility-allow-labels" type="text" />
  <textarea id="visibility-deny"></textarea>
  <textarea id="visibility-allow-entities"></textarea>
  <input id="visibility-respect-assist" type="checkbox" />
  <button id="visibility-save-btn"></button>
</body>""",
)


class TestVisibilitySettingsTab:
    """The Entity Visibility tab's load/save/409 behaviour."""

    def test_load_failure_populates_the_alert_region(
        self, settings_script: str
    ) -> None:
        """A failed config GET must announce in the load-error region (an
        assertive alert), not fail silently."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/visibility/config": {"status": 500, "json": {"error": "boom"}},
        }
        result = run_script(
            settings_script,
            initial_html=_VISIBILITY_DOM,
            fetch_map=fetches,
            invoke="await window.visibilityLoadConfig();",
        )
        _assert_clean_init(result)
        assert 'id="visibility-load-error"' in result.dom
        assert "Failed to load visibility config" in result.dom
        # The error region is an assertive alert so a screen reader announces it.
        assert 'role="alert"' in result.dom

    def test_report_issue_restriction_loads_and_saves(
        self, settings_script: str
    ) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/visibility/config": {
                "status": 200,
                "json": {"version": 4, "restrict_report_issue": True},
            },
        }
        result = run_script(
            settings_script,
            initial_html=_VISIBILITY_DOM,
            fetch_map=fetches,
            invoke="""
              await window.visibilityLoadConfig();
              const toggle = document.getElementById('visibility-restrict-report-issue');
              document.body.dataset.loaded = String(toggle.checked);
              toggle.checked = false;
              await window.visibilitySaveConfig();
            """,
        )
        _assert_clean_init(result)
        assert 'data-loaded="true"' in result.dom
        puts = [
            f
            for f in result.fetches
            if "/api/visibility/config" in f["url"] and f["method"] == "PUT"
        ]
        assert len(puts) == 1
        body = json.loads(puts[0]["body"])
        assert body["restrict_report_issue"] is False

    def test_save_success_reports_saved_as_polite_status(
        self, settings_script: str
    ) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/visibility/config": {"status": 200, "json": {"version": 5}},
        }
        result = run_script(
            settings_script,
            initial_html=_VISIBILITY_DOM,
            fetch_map=fetches,
            invoke="""
              await window.visibilitySaveConfig();
              const st = document.getElementById('visibility-save-status');
              document.body.dataset.role = st.getAttribute('role') || '';
              document.body.dataset.text = st.textContent || '';
            """,
        )
        _assert_clean_init(result)
        puts = [
            f
            for f in result.fetches
            if "/api/visibility/config" in f["url"] and f["method"] == "PUT"
        ]
        assert len(puts) == 1
        assert 'data-text="Saved."' in result.dom
        assert 'data-role="status"' in result.dom  # success stays polite, not alert

    def test_save_failure_announces_via_alert_role(self, settings_script: str) -> None:
        """A failed save must flip the status span to an alert so it is
        announced — the prior code left it a polite status."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/visibility/config": {"status": 500, "json": {"error": "kaboom"}},
        }
        result = run_script(
            settings_script,
            initial_html=_VISIBILITY_DOM,
            fetch_map=fetches,
            invoke="""
              await window.visibilitySaveConfig();
              const st = document.getElementById('visibility-save-status');
              document.body.dataset.role = st.getAttribute('role') || '';
              document.body.dataset.text = st.textContent || '';
            """,
        )
        _assert_clean_init(result)
        assert 'data-role="alert"' in result.dom
        assert "kaboom" in result.dom

    def test_save_409_keeps_edits_and_does_not_reload(
        self, settings_script: str
    ) -> None:
        """A 409 must NOT auto-reload the config (which would overwrite the
        user's unsaved edits). It surfaces the conflict as an alert and leaves
        the form untouched so the user can reload deliberately."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/visibility/config": {"status": 409, "json": {"error": "conflict"}},
        }
        result = run_script(
            settings_script,
            initial_html=_VISIBILITY_DOM,
            fetch_map=fetches,
            invoke="""
              document.getElementById('visibility-areas').value = 'garage';
              await window.visibilitySaveConfig();
              const areas = document.getElementById('visibility-areas');
              const st = document.getElementById('visibility-save-status');
              document.body.dataset.areas = areas.value;
              document.body.dataset.role = st.getAttribute('role') || '';
              document.body.dataset.text = st.textContent || '';
            """,
        )
        _assert_clean_init(result)
        vis_calls = [f for f in result.fetches if "/api/visibility/config" in f["url"]]
        # Exactly one call: the PUT. No follow-up GET reload — that is the bug
        # (it clobbered the edits the message told the user to "re-apply").
        assert len(vis_calls) == 1, vis_calls
        assert vis_calls[0]["method"] == "PUT"
        assert 'data-areas="garage"' in result.dom  # edit preserved
        assert 'data-role="alert"' in result.dom
        assert "another tab or session" in result.dom


class TestEmbeddedRestartButton:
    """Embedded deployments get a relabeled restart button (issue #1778)."""

    def test_embedded_mode_shows_relabeled_restart_button(
        self, settings_script: str
    ) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/info": {
                "status": 200,
                "json": {
                    "instance_id": "baseline-id",
                    "deployment_mode": "embedded",
                    "is_addon": False,
                    "is_sidecar": False,
                    "version": "7.11.0",
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 300));
              const btn = document.getElementById('restartBtn');
              document.body.setAttribute('data-btn-hidden',
                String(btn.style.display === 'none'));
              document.body.setAttribute('data-btn-label', btn.textContent);
              document.body.setAttribute('data-notice-head',
                document.getElementById('restartNoticeText')
                  .textContent.trim().slice(0, 80));
            """,
        )
        _assert_clean_init(result)
        assert 'data-btn-hidden="false"' in result.dom, (
            "restart button must be visible in embedded mode"
        )
        assert "Restart HA-MCP Server" in result.dom, (
            "embedded mode must relabel the restart button"
        )
        m = re.search(r'data-notice-head="([^"]*)"', result.dom)
        assert m and "Restart HA-MCP Server" in m.group(1), (
            f"embedded restart-notice copy missing; got {m.group(1) if m else None}"
        )


class TestExtraYamlWriteKeysNesting:
    """JSDOM coverage for the extra-YAML-write-keys text row nested under
    enable_yaml_config_editing (#1887).

    It comes from the advanced cache (section ``beta_yamlkeys``) but renders
    among the YAML per-key toggles, so it shares their indent class and both
    of their gates (beta master AND the parent toggle).
    """

    def _payloads(self, master_on: bool, yaml_on: bool) -> dict[str, dict]:
        return {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": {
                            "value": master_on,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_BETA_FEATURES",
                        },
                        "enable_yaml_config_editing": {
                            "value": yaml_on,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_YAML_CONFIG_EDITING",
                        },
                    },
                    "beta_sub_flags": ["enable_yaml_config_editing"],
                },
            },
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "extra_yaml_write_keys",
                            "env_var": "HA_MCP_EXTRA_YAML_KEYS",
                            "value": "alert2",
                            "type": "str",
                            "section": "beta_yamlkeys",
                            "origin": "default",
                            "editable": True,
                        }
                    ]
                },
            },
        }

    def _rows(self, dom: str) -> list[str]:
        return re.findall(
            r'<div[^>]*class="[^"]*yaml-packages-sub[^"]*"[^>]*>',
            dom,
        )

    def test_row_rendered_as_text_input_when_both_gates_on(
        self, settings_script: str
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payloads(master_on=True, yaml_on=True),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)

        assert self._rows(result.dom), (
            f"expected yaml-packages-sub row in DOM; tail: {result.dom[-2000:]}"
        )
        # A free-text box, not a toggle – the whole point of the setting.
        assert 'name="adv:extra_yaml_write_keys"' in result.dom
        for row in self._rows(result.dom):
            assert "dimmed" not in row, f"unexpected dimmed with both gates on: {row}"

    @pytest.mark.parametrize(
        "master_on,yaml_on", [(False, True), (True, False), (False, False)]
    )
    def test_row_dimmed_when_either_gate_off(
        self, settings_script: str, master_on: bool, yaml_on: bool
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payloads(master_on=master_on, yaml_on=yaml_on),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)

        rows = self._rows(result.dom)
        assert rows, "expected yaml-packages-sub row"
        for row in rows:
            assert "dimmed" in row, f"expected dimmed row: {row}"


class TestFeatureGatedStubRow:
    """Feature-gated stub rows (#2148): the "how to enable this" hint is
    worded per ``disabled_by_beta``, and the security-gate switch stays
    operable so a rule can be authored BEFORE the tool is registered."""

    @staticmethod
    def _fetches(*, policies_enabled: bool) -> dict:
        return {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [
                        {
                            "name": "ha_manage_security_policy",
                            "title": "Manage Security Policy",
                            "primary_tag": "System",
                            "annotations": {"destructiveHint": True},
                            "disabled_by": "enable_security_policy_tool",
                            "disabled_by_beta": False,
                        },
                        {
                            "name": "ha_write_file",
                            "title": "Write File",
                            "primary_tag": "Files",
                            "annotations": {"destructiveHint": True},
                            "disabled_by": "enable_filesystem_tools",
                            "disabled_by_beta": True,
                        },
                    ],
                    "states": {},
                    "env_pinned": {},
                    "read_only_exempt": [],
                },
            },
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_tool_security_policies": {"value": policies_enabled}
                    }
                },
            },
            "/api/policy/config": {
                "status": 200,
                "json": {
                    "wait_seconds": 60,
                    "approval_ttl_minutes": 5,
                    "version": 1,
                    "rules": [],
                },
            },
        }

    @staticmethod
    def _gate_input(dom: str, tool: str) -> str:
        m = re.search(rf'<input[^>]*name="tool:{tool}:gated"[^>]*>', dom)
        assert m is not None, f"gated input for {tool} missing; dom tail: {dom[-2000:]}"
        return m.group(0)

    def test_non_beta_stub_renders_non_beta_hint(self, settings_script: str) -> None:
        """A non-beta gated row must NOT claim to be beta or point at the dev
        add-on config — its toggle is on the Tool Security Policies tab."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._fetches(policies_enabled=True),
            invoke="await new Promise(r => setTimeout(r, 250));",
        )
        _assert_clean_init(result)
        row = re.search(
            r'<div class="tool"[^>]*data-name="ha_manage_security_policy".*?'
            r'name="tool:ha_manage_security_policy:gated"',
            result.dom,
            re.S,
        )
        assert row is not None, f"stub row missing; dom tail: {result.dom[-2000:]}"
        assert "Tool Security Policies tab" in row.group(0), (
            f"non-beta gated row must name where its toggle lives: {row.group(0)}"
        )
        assert "docs/beta.md" not in row.group(0), (
            f"non-beta gated row must not render the beta hint: {row.group(0)}"
        )

    def test_beta_stub_still_renders_beta_hint(self, settings_script: str) -> None:
        """Regression guard on the other branch: beta-gated rows keep the
        beta wording (dev add-on config / docs/beta.md)."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._fetches(policies_enabled=True),
            invoke="await new Promise(r => setTimeout(r, 250));",
        )
        _assert_clean_init(result)
        row = re.search(
            r'<div class="tool"[^>]*data-name="ha_write_file".*?'
            r'name="tool:ha_write_file:gated"',
            result.dom,
            re.S,
        )
        assert row is not None, f"beta stub row missing; dom tail: {result.dom[-2000:]}"
        assert "docs/beta.md" in row.group(0), (
            f"beta gated row must keep the beta hint: {row.group(0)}"
        )

    def test_gate_switch_operable_on_stub_when_policies_enabled(
        self, settings_script: str
    ) -> None:
        """The chicken-and-egg fix: with policies on, a gated-off tool's
        security-gate switch must be live so the rule can be authored before
        the tool is enabled. Its enable/pin switches stay locked."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._fetches(policies_enabled=True),
            invoke="await new Promise(r => setTimeout(r, 250));",
        )
        _assert_clean_init(result)
        gate = self._gate_input(result.dom, "ha_manage_security_policy")
        assert "disabled" not in gate, (
            f"gate switch must be operable on a stub row while policies are on: {gate}"
        )
        enabled_input = re.search(
            r'<input[^>]*name="tool:ha_manage_security_policy:enabled"[^>]*>',
            result.dom,
        )
        assert enabled_input is not None and "disabled" in enabled_input.group(0), (
            "the enable switch on a gated stub row must stay locked: "
            f"{enabled_input and enabled_input.group(0)}"
        )

    def test_gate_switch_locked_on_stub_when_policies_disabled(
        self, settings_script: str
    ) -> None:
        """Without the master switch there is nothing to gate with, so the
        stub's gate switch stays disabled (same as every other row)."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._fetches(policies_enabled=False),
            invoke="await new Promise(r => setTimeout(r, 250));",
        )
        _assert_clean_init(result)
        gate = self._gate_input(result.dom, "ha_manage_security_policy")
        assert "disabled" in gate, (
            f"gate switch must stay locked while policies are off: {gate}"
        )
