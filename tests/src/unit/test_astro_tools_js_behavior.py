"""Behavioural tests for ``site/src/pages/tools.astro``'s script.

tools.astro is TypeScript (no ``define:vars``, no ``is:inline``), so the
harness runs it through esbuild's type-stripping pass before evaluation.
The page is a tool catalog with search, multi-toggle filters by category
and file, size-bucket filtering, and a design-mode that exposes textarea
diffs for prompt regeneration.

These tests pin the search/filter pipeline. Design mode is a separate
subsystem (textarea diffs, prompt copy) — a small smoke is included to
catch the toggle wiring; the deeper diff logic warrants its own tests
when someone touches it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._js_harness import extract_script_body, run_script

TOOLS_ASTRO = (
    Path(__file__).resolve().parents[3] / "site" / "src" / "pages" / "tools.astro"
)


@pytest.fixture(scope="module")
def tools_script() -> str:
    return extract_script_body(TOOLS_ASTRO.read_text(encoding="utf-8"))


def _build_tools_dom(cards: list[dict[str, str]] | None = None) -> str:
    """Build the minimal DOM ``tools.astro`` expects on first paint.

    ``cards`` lets a test seed a specific list of tool cards; each entry
    becomes one ``.tool-card`` div with the dataset attributes the
    filter pipeline reads.
    """
    cards = cards or []
    parts = [
        "<!DOCTYPE html><html><body>",
        '<input id="search" type="text" />',
        '<div id="tools-container">',
        '<div class="tool-group">',
    ]
    for c in cards:
        attrs = " ".join(f'data-{k}="{v}"' for k, v in c.items())
        parts.append(f'<div class="tool-card" {attrs}></div>')
    parts.extend(
        [
            "</div>",
            "</div>",
            '<span id="results-count"></span>',
            '<button id="group-category"></button>',
            '<button id="group-file"></button>',
            '<button id="group-none"></button>',
            '<button id="sort-alpha"></button>',
            '<button id="sort-size"></button>',
            '<button id="expand-all"></button>',
            '<button id="design-mode-toggle"></button>',
            '<div id="design-panel" class="hidden"></div>',
            '<textarea id="design-prompt"></textarea>',
            '<button id="design-copy-btn"></button>',
            '<span id="design-change-count"></span>',
            '<button id="design-instructions-toggle"></button>',
            '<div id="design-instructions-body" class="hidden"></div>',
            "</body></html>",
        ]
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Search / filter pipeline
# ---------------------------------------------------------------------------


class TestSearchAndFilter:
    """The search input and the multi-toggle chips share one
    ``applyFilters()`` pipeline. Tests here pin its behaviour against
    the rendered ``.tool-card`` dataset attributes.
    """

    def test_search_input_hides_non_matching_cards(self, tools_script: str) -> None:
        cards = [
            {
                "tool-name": "ha_get_state",
                "tool-title": "Get state",
                "tool-desc": "fetch entity state",
                "tool-category": "Entities",
                "tool-file": "tools_entities.py",
                "tool-type": "read",
                "tool-size": "500",
            },
            {
                "tool-name": "ha_call_service",
                "tool-title": "Call service",
                "tool-desc": "invoke an HA service",
                "tool-category": "Services",
                "tool-file": "tools_services.py",
                "tool-type": "write",
                "tool-size": "800",
            },
            {
                "tool-name": "ha_search_entities",
                "tool-title": "Search entities",
                "tool-desc": "fuzzy search across entities",
                "tool-category": "Entities",
                "tool-file": "tools_search.py",
                "tool-type": "read",
                "tool-size": "1500",
            },
        ]
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom(cards),
            invoke="""
              const inp = document.getElementById('search');
              inp.value = 'service';
              inp.dispatchEvent(new Event('input'));
            """,
        )
        assert not result.errors, f"errors: {result.errors}"
        # Only the 'ha_call_service' card has 'service' in its name/title/desc;
        # the other two should pick up the 'hidden' class.
        get_state_idx = result.dom.find('data-tool-name="ha_get_state"')
        call_service_idx = result.dom.find('data-tool-name="ha_call_service"')
        search_entities_idx = result.dom.find('data-tool-name="ha_search_entities"')

        # Pull the class attribute for each card.
        def class_at(idx: int) -> str:
            slice_ = result.dom[max(0, idx - 200) : idx + 200]
            return slice_

        assert "hidden" in class_at(get_state_idx), (
            "ha_get_state should be hidden when query='service'"
        )
        # ha_search_entities has 'fuzzy search across entities' — no 'service'.
        assert "hidden" in class_at(search_entities_idx), (
            "ha_search_entities should be hidden when query='service'"
        )
        # ha_call_service should stay visible — look at the actual card
        # div, not adjacent siblings.
        before = result.dom[:call_service_idx]
        card_start = before.rfind("<div ")
        card_html = result.dom[card_start : result.dom.find(">", call_service_idx) + 1]
        assert "hidden" not in card_html, (
            f"ha_call_service should be visible for query='service'; got {card_html}"
        )

    def test_results_count_reflects_visible_cards(self, tools_script: str) -> None:
        cards = [
            {
                "tool-name": "a",
                "tool-title": "a",
                "tool-desc": "a",
                "tool-category": "X",
                "tool-file": "x.py",
                "tool-type": "read",
                "tool-size": "100",
            },
            {
                "tool-name": "b",
                "tool-title": "b",
                "tool-desc": "b",
                "tool-category": "Y",
                "tool-file": "y.py",
                "tool-type": "read",
                "tool-size": "100",
            },
        ]
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom(cards),
            invoke="""
              const inp = document.getElementById('search');
              inp.value = 'a';
              inp.dispatchEvent(new Event('input'));
            """,
        )
        assert not result.errors, f"errors: {result.errors}"
        # The results-count is populated by the pipeline. The exact text
        # depends on whether the page uses "1 result" / "1 / 2", etc. We
        # just assert *something* numeric appears so the count wiring
        # didn't silently break.
        results = result.dom[
            result.dom.find('id="results-count"') : result.dom.find(
                'id="results-count"'
            )
            + 200
        ]
        assert any(ch.isdigit() for ch in results), (
            f"results-count should pick up a number after filtering; snippet={results}"
        )


# ---------------------------------------------------------------------------
# Design mode toggle (smoke)
# ---------------------------------------------------------------------------


def test_design_mode_toggle_flips_state(tools_script: str) -> None:
    """Clicking the design-mode button reveals the panel and design-only chips.

    Deeper assertions on the diff machinery belong with a PR that
    touches it — this smoke catches the toggle wiring itself.
    """
    result = run_script(
        tools_script,
        language="ts",
        initial_html=_build_tools_dom([]),
        invoke="""
          document.getElementById('design-mode-toggle').click();
        """,
    )
    assert not result.errors, f"errors: {result.errors}"
    # After click, the button text should flip to 'Exit Design'.
    assert "Exit Design" in result.dom, (
        f"design-mode toggle should swap button text; dom={result.dom[:600]}"
    )
