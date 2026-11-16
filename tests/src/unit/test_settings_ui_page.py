"""The settings page as it is served: markup, inline script and stylesheet.

The script and stylesheet ship as files beside the page and are injected
inline; these tests compare the served page with the shipped files.
"""

from __future__ import annotations

import json
import re
from importlib.resources import files

import pytest

from ha_mcp.settings_ui import MANDATORY_TOOLS, render_settings_html
from ha_mcp.transforms import DEFAULT_PINNED_TOOLS

from ._settings_ui_app import settings_world as settings_world

_SHIPPED = files("ha_mcp.settings_ui")
_PANELS = (
    "tools",
    "server",
    "backups",
    "tool-security-policies",
    "entity-visibility",
    "accessibility",
)
_PER_REQUEST_TOKENS = (
    "__HA_MCP_THEME_PREFS__",
    "__HA_MCP_I18N__",
    "__HA_MCP_LANG__",
    "__HA_MCP_DIR__",
)


@pytest.fixture
def page() -> str:
    return render_settings_html()


def _shipped_script() -> str:
    parts = sorted(
        (p for p in (_SHIPPED / "settings_js").iterdir() if p.name.endswith(".js")),
        key=lambda p: p.name,
    )
    return "".join(part.read_text(encoding="utf-8") for part in parts)


def _shipped_stylesheet() -> str:
    return (_SHIPPED / "settings.css").read_text(encoding="utf-8")


def _tag_at(page: str, element_id: str) -> str:
    start = page.find(f'id="{element_id}"')
    assert start != -1, f"{element_id} is missing"
    return page[start : start + 140]


def test_the_browser_is_not_sent_for_a_favicon(page: str) -> None:
    """The page carries an empty icon so the browser never asks for /favicon.ico."""
    assert '<link rel="icon" href="data:,">' in page


class TestAccessibility:
    """#1596: skip link, main landmark, ARIA tablist and live status regions."""

    def test_a_skip_link_leads_to_the_one_main_landmark(self, page: str) -> None:
        assert re.search(r"""<a\s+[^>]*href=["']#main-content["']""", page)
        assert (
            len(re.findall(r"""<main\s+[^>]*id=["']main-content["'][^>]*>""", page))
            == 1
        )
        assert page.count("</main>") == 1

    def test_every_panel_has_a_tab_and_one_tab_is_selected(self, page: str) -> None:
        assert 'role="tablist"' in page
        assert page.count('role="tab"') == len(_PANELS)
        assert page.count('role="tabpanel"') == len(_PANELS)
        assert page.count('aria-selected="true"') == 1
        for name in _PANELS:
            assert f'id="tab-{name}"' in page
            assert f'aria-controls="panel-{name}"' in page
            assert f'aria-labelledby="tab-{name}"' in page

    def test_save_progress_is_announced_politely(self, page: str) -> None:
        for span_id in ("status", "backupConfigStatus", "policy-global-save-status"):
            tag = _tag_at(page, span_id)
            assert 'role="status"' in tag
            assert 'aria-live="polite"' in tag

    def test_unknown_server_settings_interrupt_a_screen_reader(self, page: str) -> None:
        """A failed features read disables the switches the notice describes."""
        for notice_id in ("roUnknownNotice", "policyUnknownNotice"):
            tag = _tag_at(page, notice_id)
            assert 'role="alert"' in tag
            assert 'aria-live="assertive"' in tag

    def test_the_script_keeps_the_tab_state_and_arrow_keys(
        self, settings_script: str
    ) -> None:
        for name in ("aria-selected", "tabIndex", "ArrowRight", "ArrowLeft"):
            assert name in settings_script
        assert "setStatusAlert" in settings_script


class TestInlinedFiles:
    def test_the_page_runs_the_shipped_script_with_the_tool_lists_filled_in(
        self, settings_script: str
    ) -> None:
        expected = (
            _shipped_script()
            .replace(
                "__HA_MCP_DEFAULT_PINNED__", json.dumps(sorted(DEFAULT_PINNED_TOOLS))
            )
            .replace("__HA_MCP_MANDATORY__", json.dumps(sorted(MANDATORY_TOOLS)))
        )

        assert settings_script == expected

    def test_the_shipped_script_cannot_end_its_script_tag_early(self) -> None:
        assert "</script>" not in _shipped_script()

    def test_the_page_carries_the_shipped_stylesheet_once(self, page: str) -> None:
        assert page.count(f"<style>{_shipped_stylesheet()}</style>") == 1

    def test_the_shipped_stylesheet_cannot_end_its_style_tag_early(self) -> None:
        assert "</style>" not in _shipped_stylesheet()

    def test_every_placeholder_is_filled_on_every_request(self, page: str) -> None:
        template = (_SHIPPED / "settings.html").read_text(encoding="utf-8")

        for token in _PER_REQUEST_TOKENS:
            assert template.count(token) == 1
        assert "__HA_MCP_" not in page


class TestPolicyScopeNotice:
    """#2515: a rule gates one tool, not a capability another tool also reaches."""

    def test_the_notice_sits_in_the_rules_section(self, page: str) -> None:
        rules_section = page.split('<section id="policy-rules">', 1)[1]

        assert 'data-i18n-html="policies.rules.scope_notice"' in rules_section

    def test_the_notice_names_both_tools(self) -> None:
        english = json.loads((_SHIPPED / "locales" / "en.json").read_text("utf-8"))

        notice = english["messages"]["policies.rules.scope_notice"]

        assert "ha_call_event" in notice
        assert "ha_call_service" in notice
