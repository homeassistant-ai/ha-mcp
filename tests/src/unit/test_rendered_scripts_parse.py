"""Parse-time guard for every rendered ``<script>`` surface in the repo.

Auto-discovers each surface via :func:`_js_harness.discover_script_surfaces`
and verifies the rendered body parses as JavaScript (or, for Astro
pages that use plain ``<script>`` without ``define:vars`` /
``is:inline``, as TypeScript via esbuild's type-stripping transform).

Parse coverage extends automatically as new UI surfaces ship — drop a
new ``.astro`` file under ``site/src/`` or register a renderer in
``_js_harness.py::_PY_RENDERERS`` and the next test run picks it up.

A parse failure here is catastrophic — a Python-consumed ``\\n`` inside
a single-quoted JS string, an unbalanced brace from a hand-edit, an
Astro template-literal that didn't close cleanly — all abort the entire
script before any handler runs, leaving the page stuck on its initial
state with no in-page diagnostic (the page-level
``window.addEventListener('error', ...)`` cannot catch parse-time
errors). The cheap ``node --check`` / esbuild parse here is the canary
that catches it before users do.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from ._js_harness import (
    ScriptSurface,
    _esbuild_binary,
    _node_binary,
    discover_script_surfaces,
)

# Discover once at module import so each surface lands as its own
# parametrize id in test output (e.g. ``[settings_ui]``,
# ``[astro_pages_setup]``).
_SURFACES: list[ScriptSurface] = discover_script_surfaces()

# CI sets ``CI=true`` (GitHub Actions does this by default). When set,
# any missing-dependency skip path in this file flips to fail so a
# workflow drift that drops the install step doesn't quietly lose
# parse coverage. Locally the skips stay informative.
_IN_CI = os.environ.get("CI", "").lower() in ("1", "true", "yes")


def _missing_dep(message: str) -> None:
    if _IN_CI:
        pytest.fail(f"CI: {message}")
    pytest.skip(message)


def _check_node_available() -> None:
    if shutil.which(_node_binary()) is None:
        _missing_dep("node not installed — install Node.js to run parse guard")


@pytest.mark.parametrize(
    "surface",
    _SURFACES,
    ids=[s.surface_id for s in _SURFACES],
)
def test_rendered_script_parses(surface: ScriptSurface, tmp_path) -> None:
    """The rendered ``<script>`` body must parse.

    JS surfaces are parsed by ``node --check``; TS surfaces (Astro's
    default ``<script>``) are parsed by esbuild's transform step (which
    bails on syntax errors). Both run as subprocess calls — slow on a
    per-surface basis (~50-150 ms) but cheap in aggregate for the
    handful of surfaces this discovers.
    """
    _check_node_available()

    if surface.language == "ts":
        # esbuild's CLI accepts piped input; --loader=ts strips types
        # and raises non-zero on syntax errors. Defaults to the local
        # install from package-lock; ``ESBUILD_BINARY`` env var
        # overrides for environments that supply it elsewhere.
        esbuild = _esbuild_binary()
        if not esbuild.is_file():
            _missing_dep(
                "esbuild not installed — run `npm install` in tests/js/ "
                "to enable TypeScript parse coverage",
            )
        result = subprocess.run(
            [str(esbuild), "--loader=ts", "--target=es2020", "--log-level=error"],
            input=surface.script,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"esbuild rejected {surface.surface_id} "
                f"({surface.source_path.name}):\n"
                f"stderr:\n{result.stderr}",
            )
        return

    # Plain JS — node --check is the same guard the legacy
    # TestRenderedHTMLJsSyntax used; we just point it at every surface
    # instead of just _SETTINGS_HTML.
    js_file = tmp_path / f"{surface.surface_id}.js"
    js_file.write_text(surface.script, encoding="utf-8")
    result = subprocess.run(
        [_node_binary(), "--check", str(js_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node --check rejected {surface.surface_id} "
            f"({surface.source_path.name}):\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}",
        )


def test_discovery_finds_expected_surfaces() -> None:
    """Pin the set of surfaces the auto-discovery walker should find.

    Updates here are intentional — if a new UI surface lands, this list
    grows. If a surface is removed (e.g. consent form swapped for a
    different provider), this list shrinks. The point is to surface the
    coverage delta in code review, not to silently change what we
    parse-check.
    """
    found = {s.surface_id for s in _SURFACES}
    expected_minimum = {
        "settings_ui",
        "consent_form",
        "astro_layouts_Layout",
        "astro_pages_setup",
        "astro_pages_tools",
    }
    missing = expected_minimum - found
    assert not missing, (
        f"discovery should find at least {expected_minimum}, missing {missing}; "
        f"found {sorted(found)}"
    )
