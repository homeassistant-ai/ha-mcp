"""Behaviour of the tools page's long-description fold.

The rule lives in `site/src/pages/tools.astro`'s frontmatter rather than
inline in the markup so it can be exercised here: the JSDOM harness reaches
`<script>` bodies, not the SSR template, and an unpinned threshold is how a
rendering choice drifts silently.
"""

import re
from pathlib import Path

import pytest

from ._js_harness import run_script, skip_if_unsupported

REPO_ROOT = Path(__file__).parent.parent.parent.parent
TOOLS_ASTRO = REPO_ROOT / "site" / "src" / "pages" / "tools.astro"


def _frontmatter() -> str:
    """The page's frontmatter, with Astro's import lines dropped.

    The imports pull in components the fold rule does not touch, and the
    harness has no module resolution.
    """
    match = re.match(r"---\n(.*?)\n---\n", TOOLS_ASTRO.read_text(encoding="utf-8"), re.S)
    assert match, "tools.astro has no frontmatter block"
    body = match.group(1)
    return "\n".join(
        line for line in body.splitlines() if not line.startswith("import ")
    )


def _fold(description: str) -> dict:
    """Run `foldDescription` over one description and return its result."""
    skip_if_unsupported()
    result = run_script(
        _frontmatter(),
        language="ts",
        invoke=(
            "globalThis.__out = JSON.stringify("
            f"foldDescription({description!r}));"
            "document.title = globalThis.__out;"
        ),
    )
    match = re.search(r"<title>(.*?)</title>", result.dom, re.S)
    assert match, f"no result captured; dom was {result.dom[:400]}"
    import json

    return json.loads(match.group(1))


class TestDescriptionFold:
    def test_a_short_description_is_not_folded(self):
        assert _fold("Viewport width in px.") == {
            "folded": False,
            "lead": "Viewport width in px.",
            "rest": "",
        }

    def test_a_description_at_the_threshold_is_not_folded(self):
        text = "x" * 600

        assert _fold(text)["folded"] is False

    def test_a_longer_description_folds(self):
        text = "y" * 601
        result = _fold(text)

        assert result["folded"] is True
        assert len(result["lead"]) == 140

    def test_the_panel_does_not_repeat_the_lead(self):
        """The summary stays visible while open, so the two must not overlap."""
        text = "".join(f"word{n} " for n in range(200))
        result = _fold(text)

        assert result["lead"] + result["rest"] == text.rstrip() or (
            result["lead"] + result["rest"] == text
        )
        assert not result["rest"].startswith(result["lead"])

    def test_an_absent_description_folds_to_nothing(self):
        assert _fold("") == {"folded": False, "lead": "", "rest": ""}


class TestFoldRuleIsPinnedToTheTemplate:
    """The numbers the page renders with are the numbers tested here."""

    @pytest.mark.parametrize(
        ("name", "value"),
        [("DESCRIPTION_FOLD_THRESHOLD", 600), ("DESCRIPTION_LEAD_LENGTH", 140)],
    )
    def test_declared_constant(self, name: str, value: int):
        assert re.search(rf"export const {name} = {value};", _frontmatter())
