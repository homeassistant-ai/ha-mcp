"""Behavioural test for ``site/src/layouts/Layout.astro``'s copy-button script.

Every Astro page inherits this layout. The inline script walks every
``<pre>`` element and wraps it with a copy-to-clipboard button so users
of the docs / setup / tools pages can copy commands in one click. A
regression that breaks the button placement (a typo in
``parentElement.classList.contains``, an off-by-one in the wrapper
insertion, or a broken event handler binding) silently degrades every
page in the site without a CI failure.

The script also exposes ``window.initCopyButtons`` so pages with
dynamically rendered ``<pre>`` blocks can re-run it after their own
render — covered here too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._js_harness import extract_script_body, run_script


@pytest.fixture(scope="module")
def layout_script() -> str:
    layout = (
        Path(__file__).resolve().parents[3]
        / "site"
        / "src"
        / "layouts"
        / "Layout.astro"
    )
    body = extract_script_body(layout.read_text(encoding="utf-8"))
    return body


@pytest.fixture
def page_with_pres() -> str:
    """A page with two ``<pre>`` blocks the script should wrap on init."""
    return """
    <!DOCTYPE html><html><body>
      <pre><code>$ echo hi</code></pre>
      <pre>$ uvx ha-mcp</pre>
      <div class="other">not a pre</div>
    </body></html>
    """


def test_init_wraps_every_pre_with_copy_button(
    layout_script: str, page_with_pres: str
) -> None:
    result = run_script(
        layout_script,
        initial_html=page_with_pres,
        # The script binds initCopyButtons to DOMContentLoaded — JSDOM
        # may have already fired that event by the time we get here, so
        # call the exposed function directly to be deterministic.
        invoke="window.initCopyButtons();",
    )
    assert not result.errors, f"errors: {result.errors}"
    # Two <pre>s → two wrappers → two copy buttons.
    assert result.dom.count("pre-wrapper") >= 2, (
        f"expected 2 .pre-wrapper divs around the two <pre> blocks, "
        f"got dom={result.dom[:800]}"
    )
    assert result.dom.count('class="copy-btn"') == 2, (
        f"expected 2 .copy-btn buttons, got dom={result.dom[:800]}"
    )


def test_reinit_does_not_double_wrap(layout_script: str, page_with_pres: str) -> None:
    """``initCopyButtons`` must be idempotent across re-invocations.

    The guard is the ``pre.parentElement?.classList.contains('pre-wrapper')``
    check at the top of the loop. Without it, a page that renders new
    ``<pre>`` blocks and re-calls ``initCopyButtons()`` would stack
    wrapper divs and double-bind handlers — every click would copy twice
    and the DOM would balloon on each refresh.
    """
    result = run_script(
        layout_script,
        initial_html=page_with_pres,
        invoke="window.initCopyButtons(); window.initCopyButtons(); window.initCopyButtons();",
    )
    assert not result.errors, f"errors: {result.errors}"
    assert result.dom.count('class="copy-btn"') == 2, (
        f"three init calls must still produce exactly 2 buttons; "
        f"got count={result.dom.count('class="copy-btn"')}"
    )
