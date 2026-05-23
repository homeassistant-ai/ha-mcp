"""Python wrapper for the JSDOM harness at ``tests/js/harness.mjs``.

Used by ``test_settings_ui_js_behavior.py`` and ``test_astro_js_behavior.py``
to drive real ``<script>`` bodies through a Node + JSDOM sandbox and assert
on observable side effects (fetches issued, broadcasts emitted, DOM mutated,
``location.reload`` invoked).

This complements — does not replace — the ``node --check`` parse guard in
``TestRenderedHTMLJsSyntax``. The parse guard remains the cheap canary that
catches Python-consumed escape sequences and other syntax errors; this
harness layers behavioural assertions on top.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# Path to the harness — repo-relative, resolved once at import time.
HARNESS_PATH = Path(__file__).resolve().parents[2] / "js" / "harness.mjs"
EXTRACT_ASTRO_VARS_PATH = HARNESS_PATH.parent / "extract_astro_vars.mjs"
JS_DEPS_DIR = HARNESS_PATH.parent


def _node_available() -> bool:
    return shutil.which("node") is not None


def _jsdom_installed() -> bool:
    return (JS_DEPS_DIR / "node_modules" / "jsdom" / "package.json").is_file()


def skip_if_unsupported() -> None:
    """Skip the calling test when node or jsdom is missing.

    CI installs both (see ``.github/workflows/test.yml``). Local devs who
    haven't run ``npm install`` in ``tests/js/`` get a skip instead of a
    confusing failure.
    """
    if not _node_available():
        pytest.skip("node not installed — install Node.js to run JS behaviour tests")
    if not _jsdom_installed():
        pytest.skip(
            "jsdom not installed — run `npm install` in tests/js/ to enable "
            "JS behaviour tests",
        )


@dataclass
class HarnessResult:
    """Side effects observed while the script ran.

    Mirrors the JSON contract documented in ``tests/js/harness.mjs``.
    Methods are convenience matchers — tests can also inspect the raw
    lists directly.
    """

    fetches: list[dict[str, Any]] = field(default_factory=list)
    broadcasts: list[dict[str, Any]] = field(default_factory=list)
    reloads: int = 0
    alerts: list[str] = field(default_factory=list)
    confirms: list[str] = field(default_factory=list)
    console: list[dict[str, Any]] = field(default_factory=list)
    status: str | None = None
    dom: str = ""
    errors: list[str] = field(default_factory=list)

    def fetches_to(self, pattern: str) -> list[dict[str, Any]]:
        """Return every fetch whose URL contains ``pattern`` (substring match)."""
        return [f for f in self.fetches if pattern in f["url"]]

    def broadcasts_of_type(self, type_: str) -> list[dict[str, Any]]:
        """Return every broadcast whose ``data.type`` equals ``type_``."""
        out = []
        for b in self.broadcasts:
            data = b.get("data") or {}
            if isinstance(data, dict) and data.get("type") == type_:
                out.append(b)
        return out


def run_script(
    script: str,
    *,
    prelude: str = "",
    invoke: str = "",
    fetch_map: dict[str, dict[str, Any]] | None = None,
    broadcast_events: list[dict[str, Any]] | None = None,
    initial_html: str | None = None,
    settle_ms: int = 120000,
    timeout_s: float = 15.0,
    language: str = "js",
) -> HarnessResult:
    """Run ``script`` in JSDOM and return observed side effects.

    Parameters mirror the JSON contract in ``tests/js/harness.mjs``.

    ``fetch_map`` keys are URL substrings; values are
    ``{"status": int, "body"?: str, "json"?: any, "throw"?: str}``.
    Missing routes default to 404.

    ``invoke`` runs after the script body — use it to call a function the
    script exposes on ``window`` (e.g. ``"await window.restartAddon();"``)
    or to dispatch DOM events.

    ``settle_ms`` is virtual time, not wall time — set generously (default
    covers the 60 s addon-restart probe window with margin).
    """
    skip_if_unsupported()

    request = {
        "script": script,
        "prelude": prelude,
        "invoke": invoke,
        "fetchMap": fetch_map or {},
        "broadcastEvents": broadcast_events or [],
        "initialHtml": initial_html or "<!DOCTYPE html><html><body></body></html>",
        "settleMs": settle_ms,
        "language": language,
    }

    proc = subprocess.run(
        ["node", str(HARNESS_PATH)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"JS harness exited {proc.returncode}\n"
            f"stderr: {proc.stderr}\n"
            f"stdout: {proc.stdout[:2000]}",
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"JS harness returned non-JSON stdout: {e}\n"
            f"stdout: {proc.stdout[:2000]}\n"
            f"stderr: {proc.stderr}",
        ) from e
    return HarnessResult(**payload)


def extract_astro_frontmatter_vars(
    astro_path: Path, names: list[str]
) -> dict[str, Any]:
    """Return ``{name: value}`` for each Astro frontmatter const requested.

    Spawns ``tests/js/extract_astro_vars.mjs``, which evaluates the
    frontmatter (TypeScript stripped via esbuild) with ``import`` lines
    removed and ``base = ""`` stubbed for ``import.meta.env.BASE_URL``.
    Use to feed the wizard's data arrays into the JS behaviour harness
    so tests see the real production set, not a hand-maintained mock.
    """
    skip_if_unsupported()
    proc = subprocess.run(
        ["node", str(EXTRACT_ASTRO_VARS_PATH)],
        input=json.dumps({"path": str(astro_path), "names": names}),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"extract_astro_vars failed for {astro_path.name}: "
            f"stderr={proc.stderr}\nstdout={proc.stdout[:1000]}",
        )
    return json.loads(proc.stdout)


def astro_vars_prelude(vars_: dict[str, Any]) -> str:
    """Render a JS prelude that assigns each var as a const.

    Used as the ``prelude`` argument to :func:`run_script` so the rest
    of the Astro page script sees the same names Astro would have
    injected via ``<script define:vars={...}>``.
    """
    parts = []
    for name, value in vars_.items():
        parts.append(f"const {name} = {json.dumps(value)};")
    return "\n".join(parts)


def extract_script_body(source: str, *, marker: str = "<script>") -> str:
    """Return the first non-external inline ``<script>`` body in ``source``.

    Handles both the bare ``<script>`` form used by ``_SETTINGS_HTML`` and
    attributed forms like Astro's ``<script define:vars={...}>``. The body
    is everything between the opening tag's ``>`` and the next
    ``</script>``. External scripts (``<script src=...></script>``) are
    skipped — they have no inline body to extract.
    """
    for match in re.finditer(r"<script\b([^>]*)>", source):
        attrs = match.group(1)
        if re.search(r"\bsrc\s*=", attrs):
            continue
        start = match.end()
        end = source.find("</script>", start)
        if end == -1:
            raise ValueError("unterminated <script> in source")
        return source[start:end]
    raise ValueError(f"no inline <script> tag in source (marker hint: {marker!r})")


# ---------------------------------------------------------------------------
# Surface discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptSurface:
    """A single rendered ``<script>`` body discovered in the codebase.

    Auto-discovery — see :func:`discover_script_surfaces` — keeps the
    parse-time guard automatically covering any new UI surface a future
    PR adds, without anyone having to remember to register the file.
    """

    surface_id: str
    """Short stable id used as the pytest parameter id (e.g. ``settings_ui``)."""

    source_path: Path
    """Absolute path to the file the script body was extracted from."""

    script: str
    """The extracted ``<script>`` body, after Python/Astro evaluation when needed."""

    language: str
    """``"js"`` or ``"ts"`` — drives whether the harness transpiles before eval."""


def discover_script_surfaces() -> list[ScriptSurface]:
    """Walk the repo for every rendered ``<script>`` surface that ships.

    Returns one entry per surface, in stable order. Future ``<script>``
    surfaces added under ``src/ha_mcp/**/*.py`` (HTML in Python triple-
    quoted strings) or ``site/src/**/*.{astro,html}`` are picked up
    automatically by the auto-discovery test, so the parse-time guard
    extends without code changes.

    Astro pages with TypeScript inside ``<script>`` (no ``is:inline``,
    no ``define:vars``) are tagged ``language="ts"`` so the harness
    transpiles them via esbuild before parsing.
    """
    repo_root = Path(__file__).resolve().parents[3]
    surfaces: list[ScriptSurface] = []

    # Python-embedded UI: each entry is a callable that returns the
    # rendered HTML — usually a module-level constant accessed via a
    # lambda, or a render function called with placeholder args. The
    # callable form keeps consent_form's ``create_consent_html(...)``
    # discoverable without inventing a synthetic constant.
    def _render_consent() -> str:
        from ha_mcp.auth.consent_form import create_consent_html

        return create_consent_html(
            client_id="test-client",
            redirect_uri="https://test.local/cb",
            state="state-x",
            txn_id="txn-y",
        )

    def _render_settings() -> str:
        from ha_mcp.settings_ui import _SETTINGS_HTML

        return _SETTINGS_HTML

    py_entries: list[tuple[str, str, Any]] = [
        # (surface_id, dotted module ref for source_path, html-renderer)
        ("settings_ui", "ha_mcp.settings_ui", _render_settings),
        ("consent_form", "ha_mcp.auth.consent_form", _render_consent),
    ]
    for surface_id, module_name, render in py_entries:
        try:
            module = __import__(module_name, fromlist=["__file__"])
        except ImportError as exc:
            raise RuntimeError(
                f"discover_script_surfaces: cannot import {module_name}: {exc}",
            ) from exc
        body = extract_script_body(render(), marker=f"<script in {module_name}>")
        surfaces.append(
            ScriptSurface(
                surface_id=surface_id,
                source_path=Path(module.__file__),
                script=body,
                language="js",
            ),
        )

    # Astro pages and layouts. The site lives outside the package; walk
    # the static source. Skip external <script src=...> tags.
    site_dir = repo_root / "site" / "src"
    if site_dir.is_dir():
        for path in sorted(site_dir.rglob("*.astro")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"<script\b([^>]*)>", text):
                attrs = match.group(1)
                if re.search(r"\bsrc\s*=", attrs):
                    continue
                end = text.find("</script>", match.end())
                if end == -1:
                    continue
                body = text[match.end() : end]
                # `define:vars` interpolation is JSON, not TS — Astro
                # builds the page by prepending `const k = JSON.parse(...);`
                # for each var. For parse coverage we strip the directive
                # away (the body itself is plain JS).
                #
                # `<script>` without `define:vars`, `is:inline`, or `lang`
                # defaults to TypeScript in Astro since v3.
                if "define:vars" in attrs or "is:inline" in attrs:
                    language = "js"
                else:
                    language = "ts"
                rel = path.relative_to(site_dir).with_suffix("")
                surface_id = f"astro_{str(rel).replace('/', '_').replace(chr(92), '_')}"
                surfaces.append(
                    ScriptSurface(
                        surface_id=surface_id,
                        source_path=path,
                        script=body,
                        language=language,
                    ),
                )

    return surfaces
