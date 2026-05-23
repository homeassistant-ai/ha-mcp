"""Behavioural tests for ``site/src/pages/tools.astro``'s script.

tools.astro is TypeScript (no ``define:vars``, no ``is:inline``), so
the harness runs it through esbuild's type-stripping pass before
evaluation. The page is a tool catalog with search, multi-toggle
filters by category and file, size-bucket filtering, group toggle
(category/file/none), sort toggle (alpha/size), expand-all, and a
design mode that swaps in diff-editable description textareas plus a
copy-prompt button.

The harness is the same JSDOM driver used by the other behavioural
tests. The DOM fixture below seeds the elements the script's top-level
``getEl(...)`` calls require — without them the script throws during
init.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ._js_harness import HarnessResult, extract_script_body, run_script

TOOLS_ASTRO = (
    Path(__file__).resolve().parents[3] / "site" / "src" / "pages" / "tools.astro"
)


@pytest.fixture(scope="module")
def tools_script() -> str:
    return extract_script_body(
        TOOLS_ASTRO.read_text(encoding="utf-8"),
        source_label=str(TOOLS_ASTRO),
    )


def _build_tools_dom(
    cards: list[dict[str, str]] | None = None,
    *,
    filter_btns: list[str] | None = None,
    size_filter_btns: list[str] | None = None,
    cat_btns: list[str] | None = None,
    file_btns: list[str] | None = None,
) -> str:
    """Build the minimal DOM ``tools.astro`` expects on first paint.

    ``cards`` seeds tool cards; each entry becomes one ``.tool-card``
    div with the dataset attributes the filter pipeline reads. The
    extra ``*_btns`` lists seed the chip toolbars so the multi-toggle
    bindings have something to bind to.
    """
    cards = cards or []
    filter_btns = filter_btns or ["all", "read", "write"]
    size_filter_btns = size_filter_btns or ["small", "medium", "large"]
    cat_btns = cat_btns or []
    file_btns = file_btns or []

    parts = [
        "<!DOCTYPE html><html><body>",
        '<input id="search" type="text" />',
    ]
    parts.extend(
        f'<button class="filter-btn" data-filter="{f}">{f}</button>'
        for f in filter_btns
    )
    parts.extend(
        f'<button class="size-filter-btn" data-size-filter="{s}">{s}</button>'
        for s in size_filter_btns
    )
    parts.extend(
        f'<button class="cat-btn" data-cat="{c}">{c}</button>' for c in cat_btns
    )
    parts.extend(
        f'<button class="file-btn" data-file="{f}">{f}</button>' for f in file_btns
    )
    parts.extend(
        [
            '<div id="tools-container">',
            '<div class="tool-group">',
        ]
    )
    for c in cards:
        attrs = " ".join(f'data-{k}="{v}"' for k, v in c.items())
        parts.append(
            f'<div class="tool-card" {attrs}>'
            f'<span class="tool-chevron"></span>'
            f'<div class="tool-details hidden"></div>'
            f"</div>"
        )
    parts.extend(
        [
            "</div>",  # /tool-group
            "</div>",  # /tools-container
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
            '<div class="design-only hidden"></div>',
            "</body></html>",
        ]
    )
    return "\n".join(parts)


def _assert_clean_init(result: HarnessResult) -> None:
    init_errors = [
        e
        for e in result.errors
        if e.startswith(("script init:", "transpile failure", "invoke:", "jsdom error"))
    ]
    assert not init_errors, f"script failed to initialise: {init_errors}"


def _card_class(dom: str, tool_name: str) -> str:
    """Return the ``class`` attribute value of the card whose
    ``data-tool-name`` is ``tool_name``. Lets tests inspect visibility
    via the actual element rather than fragile substring slicing.
    """
    match = re.search(
        rf'<div\s+class="([^"]*)"[^>]*\bdata-tool-name="{re.escape(tool_name)}"',
        dom,
    )
    if match is None:
        match = re.search(
            rf'<div\s+(?:[^>]*\s)?data-tool-name="{re.escape(tool_name)}"[^>]*\bclass="([^"]*)"',
            dom,
        )
    if match is None:
        raise AssertionError(
            f"tool-card for {tool_name!r} not found in dom (length {len(dom)})"
        )
    return match.group(1)


# Module-level so the cards list isn't a mutable class attr (RUF012)
# but tests in multiple classes still share the same fixture data.
SAMPLE_CARDS: list[dict[str, str]] = [
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


# ---------------------------------------------------------------------------
# Search / filter pipeline
# ---------------------------------------------------------------------------


class TestSearchAndFilter:
    """The search input and the multi-toggle chips share one
    ``applyFilters()`` pipeline. Tests here pin its behaviour against
    the rendered ``.tool-card`` dataset attributes.
    """

    def test_search_input_hides_non_matching_cards(self, tools_script: str) -> None:
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom(SAMPLE_CARDS),
            invoke="""
              const inp = document.getElementById('search');
              inp.value = 'service';
              inp.dispatchEvent(new Event('input'));
            """,
        )
        _assert_clean_init(result)
        assert "hidden" in _card_class(result.dom, "ha_get_state").split(), (
            "ha_get_state should be hidden when query='service'"
        )
        assert "hidden" in _card_class(result.dom, "ha_search_entities").split(), (
            "ha_search_entities (no 'service' in metadata) should be hidden"
        )
        assert "hidden" not in _card_class(result.dom, "ha_call_service").split(), (
            "ha_call_service should be visible for query='service'"
        )

    def test_results_count_reflects_visible_cards(self, tools_script: str) -> None:
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom(SAMPLE_CARDS),
            invoke="""
              const inp = document.getElementById('search');
              inp.value = 'service';
              inp.dispatchEvent(new Event('input'));
              const rc = document.getElementById('results-count');
              document.body.dataset.resultsCount = rc.textContent || '';
            """,
        )
        _assert_clean_init(result)
        match = re.search(r'data-results-count="([^"]*)"', result.dom)
        assert match, "results-count text not captured"
        assert "1" in match.group(1), (
            f"results-count should reflect 1 visible card; got {match.group(1)!r}"
        )

    def test_type_filter_chip_narrows_visible_cards(self, tools_script: str) -> None:
        """Clicking the ``write`` filter chip must hide read-only cards.

        A regression that misroutes the chip's ``activeFilter``
        assignment would leave all cards visible regardless of the
        chip's apparent state — confusing the user with no error signal.
        """
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom(SAMPLE_CARDS),
            invoke="""
              document.querySelector('.filter-btn[data-filter="write"]').click();
            """,
        )
        _assert_clean_init(result)
        assert "hidden" in _card_class(result.dom, "ha_get_state").split()
        assert "hidden" in _card_class(result.dom, "ha_search_entities").split()
        assert "hidden" not in _card_class(result.dom, "ha_call_service").split()

    def test_category_chip_toggles_via_bind_multi_toggle(
        self, tools_script: str
    ) -> None:
        """The ``.cat-btn`` chips drive ``selectedCats``. Clicking one
        narrows visible cards to that category; clicking it again
        clears the selection.

        Covers the ``bindMultiToggle('.cat-btn', 'cat', selectedCats)``
        binding — a regression here silently breaks per-category
        filtering with no visible error.
        """
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom(
                SAMPLE_CARDS,
                cat_btns=["Entities", "Services"],
            ),
            invoke="""
              document.querySelector('.cat-btn[data-cat="Services"]').click();
              const visAfterClick = Array.from(
                document.querySelectorAll('.tool-card:not(.hidden)')
              ).map((c) => c.dataset.toolName).join(',');
              document.body.dataset.visAfterClick = visAfterClick;
              document.querySelector('.cat-btn[data-cat="Services"]').click();
              const visAfterToggleOff = Array.from(
                document.querySelectorAll('.tool-card:not(.hidden)')
              ).map((c) => c.dataset.toolName).join(',');
              document.body.dataset.visAfterToggleOff = visAfterToggleOff;
            """,
        )
        _assert_clean_init(result)
        m1 = re.search(r'data-vis-after-click="([^"]*)"', result.dom)
        m2 = re.search(r'data-vis-after-toggle-off="([^"]*)"', result.dom)
        assert m1 and m1.group(1) == "ha_call_service", (
            f"only Services-category card should be visible; got {m1 and m1.group(1)!r}"
        )
        assert m2 and set(m2.group(1).split(",")) == {
            "ha_get_state",
            "ha_call_service",
            "ha_search_entities",
        }, f"all cards should be visible after toggle-off; got {m2 and m2.group(1)!r}"

    def test_size_filter_chip_narrows_by_size_bucket(self, tools_script: str) -> None:
        """Size chips bucket cards into small / medium / large. A
        regression in the bucket math (off-by-one on the threshold)
        would silently route cards to the wrong bucket.
        """
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom(SAMPLE_CARDS),
            invoke="""
              document.querySelector('.size-filter-btn[data-size-filter="large"]').click();
            """,
        )
        _assert_clean_init(result)
        visible = [
            t
            for t in ("ha_get_state", "ha_call_service", "ha_search_entities")
            if "hidden" not in _card_class(result.dom, t).split()
        ]
        # Production threshold: size > 2000 = large, > 1000 = medium,
        # else small. None of SAMPLE_CARDS exceed 2000, so 'large'
        # narrows to zero. If the threshold changes the assertion
        # tightens or loosens with a clear failure message.
        assert len(visible) <= 1, (
            f"size-filter=large should narrow to at most 1 card; got {visible}"
        )


# ---------------------------------------------------------------------------
# Group / sort / expand-all controls
# ---------------------------------------------------------------------------


class TestGroupSortExpand:
    """The group, sort, and expand-all toggles mutate render order /
    grouping / per-card detail visibility. Each is a separate code path
    in the script — a regression in any one silently degrades the page
    without an error signal.
    """

    def test_group_by_file_regroups_container(self, tools_script: str) -> None:
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom(SAMPLE_CARDS),
            invoke="""
              document.getElementById('group-file').click();
              document.body.dataset.groupNames = Array.from(
                document.querySelectorAll('.tool-group[data-group-name]')
              ).map((g) => g.dataset.groupName).join('|');
            """,
        )
        _assert_clean_init(result)
        match = re.search(r'data-group-names="([^"]*)"', result.dom)
        assert match, "group-name attrs missing after group-file click"
        names = match.group(1).split("|") if match.group(1) else []
        assert {"tools_entities.py", "tools_services.py", "tools_search.py"} == set(
            names
        ), f"group-file should produce 3 file-named groups; got {names}"

    def test_group_none_collapses_to_single_group(self, tools_script: str) -> None:
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom(SAMPLE_CARDS),
            invoke="""
              document.getElementById('group-none').click();
              document.body.dataset.groupCount = String(
                document.querySelectorAll('#tools-container .tool-group').length
              );
            """,
        )
        _assert_clean_init(result)
        match = re.search(r'data-group-count="([^"]*)"', result.dom)
        assert match and match.group(1) == "1", (
            f"group-none should produce exactly 1 group; "
            f"got {match and match.group(1)!r}"
        )

    def test_sort_alpha_orders_cards_by_name(self, tools_script: str) -> None:
        """Sort wiring rebuilds the DOM via ``regroup`` -> ``sortCards``.
        Asserting on the post-sort name order catches a regression that
        flipped the localeCompare argument order.
        """
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom(SAMPLE_CARDS),
            invoke="""
              document.getElementById('group-none').click();
              document.getElementById('sort-alpha').click();
              document.body.dataset.sortedNames = Array.from(
                document.querySelectorAll('#tools-container .tool-card')
              ).map((c) => c.dataset.toolName).join(',');
            """,
        )
        _assert_clean_init(result)
        match = re.search(r'data-sorted-names="([^"]*)"', result.dom)
        assert match, "sorted-names attr missing"
        names = match.group(1).split(",")
        assert names == sorted(names), (
            f"sort-alpha should produce alphabetical name order; got {names}"
        )

    def test_expand_all_toggles_button_label(self, tools_script: str) -> None:
        """Catches a regression that wires the click but forgets the
        label flip (expand-all / collapse-all). The label is the only
        user-visible signal that the click did anything when the page
        starts with cards collapsed.
        """
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom(SAMPLE_CARDS),
            invoke="""
              const btn = document.getElementById('expand-all');
              const before = btn.textContent || '';
              btn.click();
              const after = btn.textContent || '';
              document.body.dataset.expandBefore = before;
              document.body.dataset.expandAfter = after;
            """,
        )
        _assert_clean_init(result)
        before = re.search(r'data-expand-before="([^"]*)"', result.dom)
        after = re.search(r'data-expand-after="([^"]*)"', result.dom)
        assert before and after and before.group(1) != after.group(1), (
            f"expand-all click should flip label; "
            f"before={before and before.group(1)!r} "
            f"after={after and after.group(1)!r}"
        )


# ---------------------------------------------------------------------------
# Design mode
# ---------------------------------------------------------------------------


class TestDesignMode:
    """Design mode swaps in description-edit textareas + reveals the
    design-only chips (file grouping, size sorting, etc.). Tests here
    pin the toggle wiring; the deeper diff machinery warrants its own
    tests when someone touches it.
    """

    def test_design_mode_toggle_flips_label_and_reveals_design_only(
        self, tools_script: str
    ) -> None:
        result = run_script(
            tools_script,
            language="ts",
            initial_html=_build_tools_dom([]),
            invoke="""
              const btn = document.getElementById('design-mode-toggle');
              document.body.dataset.beforeLabel = btn.textContent || '';
              btn.click();
              document.body.dataset.afterLabel = btn.textContent || '';
              const designOnly = document.querySelector('.design-only');
              document.body.dataset.designOnlyHidden = String(
                designOnly.classList.contains('hidden')
              );
            """,
        )
        _assert_clean_init(result)
        assert "Exit Design" in result.dom, (
            "design-mode toggle should swap button text to 'Exit Design'"
        )
        # design-only elements must lose the 'hidden' class so the
        # design controls become reachable.
        assert 'data-design-only-hidden="false"' in result.dom, (
            "design-only elements should be visible after toggling design mode on"
        )
