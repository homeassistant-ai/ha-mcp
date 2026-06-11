"""Light-scheme coverage guard for the docs site (#1572).

The docs site is authored with hard-coded dark-palette Tailwind utilities
(``bg-slate-800``, ``text-slate-300``, ...). Light mode works by routing the
used palette shades through CSS custom properties in
``site/tailwind.config.mjs`` and remapping their values under
``:root[data-theme="light"]`` in ``site/src/styles/global.css``.

That mechanism only covers what is declared. Historically every light-mode
bug on this feature was a *coverage* gap: a utility class (or an
``@apply``-baked custom class) whose family/shade nobody had remapped, which
then rendered light-on-light or dark-on-dark. This test turns coverage into
a CI invariant: every color utility used anywhere under ``site/src`` must be

* **themed** — its ``(family, shade)`` is routed through a ``--tw-*`` token
  in ``tailwind.config.mjs`` (and therefore remapped for light), or
* **light-safe** — listed in ``LIGHT_SAFE`` below with a reason why the
  stock value holds on both schemes, or
* **special-cased** — the hard-coded ``white`` utilities, which have
  explicit selector overrides in ``global.css`` that this test verifies
  literally.

Adding a new color utility that fits none of these fails the test with
instructions, so the decision is forced at PR time instead of surfacing as
a contrast bug screenshot later.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

_SITE = Path(__file__).resolve().parents[3] / "site"
_CONFIG = _SITE / "tailwind.config.mjs"
_GLOBAL_CSS = _SITE / "src" / "styles" / "global.css"

# Tailwind named color families (v3) that carry numeric shades.
_FAMILIES = (
    "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|"
    "emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose"
)

# A palette utility: optional variant prefixes (hover:, focus:, md:, ...),
# a color-bearing utility prefix, family, shade, optional opacity modifier.
_PALETTE_RE = re.compile(
    rf"(?:[a-z-]+:)*"
    rf"(?:bg|text|border|from|to|via|ring|divide|decoration|outline|fill|stroke|placeholder)"
    rf"-({_FAMILIES})-(\d{{2,3}})(?:/[\w.\[\]]+)?\b"
)

# Hard-coded white utilities (no shade): text-white, border-white/[0.06], ...
# The trailing lookahead (instead of \b, which fails after "]") prevents two
# false matches: a partial match of "bg-white" inside "bg-white/[0.03]", and
# CSS-escaped selectors in global.css itself (".border-white\/\[0\.06\]").
_WHITE_RE = re.compile(
    r"(?:[a-z-]+:)*(?:bg|text|border)-white(?:/(?:\[[0-9.]+\]|\d{1,3}))?(?![\w/\[\\])"
)

# Shades whose stock value reads acceptably on BOTH schemes, with the reason
# they don't need a light remap. Keyed (family, shade).
LIGHT_SAFE: dict[tuple[str, int], str] = {
    ("slate", 600): "dark chip fill (.step-number) / mid border; works on both",
    ("blue", 500): "saturated solid (hover fill, ring); white text re-pinned",
    ("blue", 600): "saturated button fill; white text re-pinned in global.css",
    ("blue", 700): "border tone over the blue-100 light tint",
    ("amber", 500): "low-alpha tint/border holds on light",
    ("amber", 600): "border tone over light amber tints",
    ("amber", 700): "border tone over light amber tints",
    ("green", 500): "saturated solid/border; readable on both",
    ("green", 700): "saturated badge fill (quick-start-badge), white text baked",
    ("green", 800): "low-alpha border tone",
    ("red", 500): "low-alpha tint/border holds on light",
    ("red", 700): "border tone over the red-100 light tint",
    ("purple", 500): "low-alpha tint/border holds on light",
    ("purple", 600): "border tone",
    ("purple", 700): "border tone",
    ("violet", 500): "saturated accent (fills/borders/ring) used at low alpha",
    ("cyan", 600): "border tone over the cyan-100 light tint",
    ("yellow", 700): "border tone over light yellow tints",
}

# Foreground exceptions re-pinned by hand in global.css because the themed
# direction of their shade is "container", not "foreground".
PINNED_SELECTORS = (':root[data-theme="light"] .text-slate-700',)

# The hard-coded white utilities must each have a literal override (or be a
# documented keep-white compound) in global.css.
_WHITE_OVERRIDE_SELECTORS = {
    # trailing comma keeps this from substring-matching ".text-white\/70"
    "text-white": r".text-white,",
    "text-white/70": r".text-white\/70",
    "hover:text-white": r".hover\:text-white:hover",
    "bg-white/[0.03]": r".bg-white\/\[0\.03\]",
    "bg-white/[0.05]": r".bg-white\/\[0\.05\]",
    "hover:bg-white/[0.04]": r".hover\:bg-white\/\[0\.04\]:hover",
    "hover:bg-white/[0.08]": r".hover\:bg-white\/\[0\.08\]:hover",
    "border-white/[0.04]": r".border-white\/\[0\.04\]",
    "border-white/[0.06]": r".border-white\/\[0\.06\]",
    "border-white/[0.08]": r".border-white\/\[0\.08\]",
}


@functools.cache
def _source_texts() -> tuple[tuple[Path, str], ...]:
    """All site sources with their content, read once for the module."""
    files = sorted(
        p
        for suffix in ("*.astro", "*.css", "*.html", "*.js", "*.ts")
        for p in _SITE.glob(f"src/**/{suffix}")
    )
    assert files, f"no site sources found under {_SITE}/src"
    return tuple((p, p.read_text(encoding="utf-8")) for p in files)


def _themed_shades() -> set[tuple[str, int]]:
    """Parse the themed(...) declarations out of tailwind.config.mjs."""
    config = _CONFIG.read_text(encoding="utf-8")
    themed: set[tuple[str, int]] = set()
    for match in re.finditer(r"themed\(\s*['\"](\w+)['\"]((?:,\s*\d+)+)\)", config):
        family = match.group(1)
        for shade in re.findall(r"\d+", match.group(2)):
            themed.add((family, int(shade)))
    assert themed, "no themed(...) palette entries found in tailwind.config.mjs"
    return themed


def test_every_palette_utility_is_light_covered() -> None:
    themed = _themed_shades()
    offenders: list[str] = []
    for path, text in _source_texts():
        for match in _PALETTE_RE.finditer(text):
            family, shade = match.group(1), int(match.group(2))
            if (family, shade) in themed or (family, shade) in LIGHT_SAFE:
                continue
            offenders.append(f"{path.relative_to(_SITE)}: {match.group(0)}")
    assert not offenders, (
        "Color utilities without light-scheme coverage found:\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\nEither route the shade through themed(...) in"
        " site/tailwind.config.mjs and add its --tw-* light value in"
        " global.css, or add it to LIGHT_SAFE here with a reason."
    )


def test_light_safe_set_has_no_dead_entries() -> None:
    """Every LIGHT_SAFE entry must still be used somewhere — stale entries
    would silently re-open coverage holes if the shade comes back with a
    different role than the recorded reason."""
    used: set[tuple[str, int]] = set()
    for _path, text in _source_texts():
        for match in _PALETTE_RE.finditer(text):
            used.add((match.group(1), int(match.group(2))))
    dead = set(LIGHT_SAFE) - used
    assert not dead, f"LIGHT_SAFE entries no longer used in site/src: {sorted(dead)}"


def test_white_utilities_have_explicit_overrides() -> None:
    css = _GLOBAL_CSS.read_text(encoding="utf-8")
    found: set[str] = set()
    for _path, text in _source_texts():
        found.update(m.group(0) for m in _WHITE_RE.finditer(text))
    missing_overrides: list[str] = []
    unknown: list[str] = []
    for utility in sorted(found):
        selector = _WHITE_OVERRIDE_SELECTORS.get(utility)
        if selector is None:
            unknown.append(utility)
        elif selector not in css:
            missing_overrides.append(f"{utility} -> {selector}")
    assert not unknown, (
        f"white utilities without a registered light-mode override: {unknown}; "
        "add the global.css override and register it in "
        "_WHITE_OVERRIDE_SELECTORS."
    )
    assert not missing_overrides, (
        "registered white overrides missing from global.css:\n  "
        + "\n  ".join(missing_overrides)
    )


def test_pinned_foreground_exceptions_present() -> None:
    css = _GLOBAL_CSS.read_text(encoding="utf-8")
    missing = [sel for sel in PINNED_SELECTORS if sel not in css]
    assert not missing, (
        f"pinned light-mode exceptions missing from global.css: {missing}"
    )


# Custom classes that bake `text-white` via @apply. Baked declarations are
# immune to the `.text-white` class override in global.css (the compiled
# class carries the color itself), so each must be classified by hand:
# KEEP_WHITE = the class also bakes a saturated fill that stays saturated in
# light mode; OVERRIDE = the class sits on a container that turns light, so
# global.css must darken it explicitly.
_BAKED_WHITE_KEEP = {
    "step-number",  # bg-slate-600 / bg-blue-600 chip, stays dark on light
    "optional-badge",  # bg-slate-600 chip, stays dark on light
    "section-number",  # bg-blue-600 chip
    "quick-start-badge",  # bg-green-700 chip
}
_BAKED_WHITE_OVERRIDE = {"section-header", "instruction-title", "arch-label"}

_APPLY_WHITE_RE = re.compile(
    r"\.([\w-]+)\s*\{[^}]*@apply[^;}]*\btext-white\b", re.DOTALL
)


def test_apply_baked_white_text_is_classified() -> None:
    found: set[str] = set()
    for _path, text in _source_texts():
        found.update(_APPLY_WHITE_RE.findall(text))
    assert found, "expected at least one @apply'd text-white class in site/src"
    unclassified = found - _BAKED_WHITE_KEEP - _BAKED_WHITE_OVERRIDE
    assert not unclassified, (
        f"@apply'd text-white classes without a light-mode decision: "
        f"{sorted(unclassified)}; add each to _BAKED_WHITE_KEEP (stays on a "
        "saturated fill) or _BAKED_WHITE_OVERRIDE (and darken it in "
        "global.css)."
    )
    css = _GLOBAL_CSS.read_text(encoding="utf-8")
    # Anchored match: a plain substring (or \b, which treats "-" as a
    # boundary) would accept ".section-header-title" as covering
    # ".section-header".
    not_overridden = [
        c
        for c in _BAKED_WHITE_OVERRIDE & found
        if not re.search(rf"\.{re.escape(c)}(?![\w-])", css)
    ]
    assert not not_overridden, (
        f"baked-white classes registered as OVERRIDE but missing a light-mode "
        f"rule in global.css: {not_overridden}"
    )
