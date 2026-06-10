"""Parity guard for the duplicated anti-FOUC accessibility-pref resolver.

The #1572 accessibility feature ships the same pre-paint resolver on two
independent surfaces — the Python-served settings UI
(``ha_mcp.settings_ui._SETTINGS_HTML``) and the Astro docs layout
(``site/src/layouts/Layout.astro``). Both read the same ``ha-mcp-*``
``localStorage`` keys and set the same ``data-theme`` / ``data-contrast``
/ ``data-shade`` / root-``font-size`` attributes on ``<html>`` before CSS
evaluates, so a user who set prefs on one surface sees them honored on the
other with no flash.

They CANNOT share a source file: the settings page is a self-contained
HTML string (no external assets, may be served while the docs site is a
static GitHub Pages build), and an external ``<script src>`` would load
asynchronously and defeat the anti-FOUC guarantee. So the duplication is
the pragmatic floor — but "keep these two in sync by hand" is a comment,
not an enforced invariant. This test turns that prose into a structural
guarantee: edit one resolver without mirroring the other and CI goes red.

The comparison is logic-only — JS line comments, quote style, and
whitespace are normalized away, so the two copies are free to differ in
commentary and formatting while their behavior must stay identical.
"""

from __future__ import annotations

import re
from pathlib import Path

_ANTI_FOUC_RE = re.compile(
    r"""<script[^>]*\bdata-purpose\s*=\s*["']anti-fouc["'][^>]*>(.*?)</script>""",
    re.DOTALL,
)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_WHITESPACE_RE = re.compile(r"\s+")


def _extract_anti_fouc(source: str, *, label: str) -> str:
    """Return the body of the ``data-purpose="anti-fouc"`` inline script."""
    match = _ANTI_FOUC_RE.search(source)
    assert match is not None, f"no anti-fouc script found in {label}"
    return match.group(1)


def _normalize(js: str) -> str:
    """Reduce a JS snippet to its logic: drop ``//`` comments, unify quote
    style, and strip all whitespace. Two snippets that differ only in
    commentary or formatting normalize to the same string."""
    js = _LINE_COMMENT_RE.sub("", js)
    js = js.replace('"', "'")
    return _WHITESPACE_RE.sub("", js)


def test_anti_fouc_resolvers_are_logically_identical() -> None:
    from ha_mcp.settings_ui import _SETTINGS_HTML

    layout_path = (
        Path(__file__).resolve().parents[3]
        / "site"
        / "src"
        / "layouts"
        / "Layout.astro"
    )

    settings_body = _extract_anti_fouc(_SETTINGS_HTML, label="settings_ui")
    layout_body = _extract_anti_fouc(
        layout_path.read_text(encoding="utf-8"), label="Layout.astro"
    )

    assert _normalize(settings_body) == _normalize(layout_body), (
        "The anti-FOUC accessibility-pref resolvers in "
        "ha_mcp/settings_ui.py and site/src/layouts/Layout.astro have "
        "diverged. They must stay logically identical (same localStorage "
        "keys, same <html> attributes) or one surface paints with the "
        "wrong prefs before CSS loads. Mirror your change into both."
    )
