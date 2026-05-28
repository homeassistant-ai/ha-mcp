"""Python wrapper for the JSDOM harness at ``tests/js/harness.mjs``.

Used by the ``test_*_js_behavior.py`` modules under ``tests/src/unit/``
to drive real ``<script>`` bodies through a Node + JSDOM sandbox and
assert on observable side effects (fetches issued, broadcasts emitted,
DOM mutated, ``location.reload`` invoked).

Layered on top of — not a replacement for — the auto-discovery parse
guard in ``test_rendered_scripts_parse.py``. The parse guard is the
cheap canary that catches Python-consumed escape sequences and other
syntax errors; this harness adds behavioural assertions.
"""

from __future__ import annotations

import json
import os
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


def _node_binary() -> str:
    """Return the node binary to invoke. Default is PATH-resolved
    ``node``; ``NODE_BINARY`` env var overrides for sandboxed runners
    that pin a specific path.
    """
    return os.environ.get("NODE_BINARY", "node")


def _esbuild_binary() -> Path:
    """Return the esbuild binary path. Default is the project-local
    install from ``tests/js/package-lock.json``; ``ESBUILD_BINARY`` env
    var overrides for environments that supply esbuild elsewhere.
    """
    override = os.environ.get("ESBUILD_BINARY")
    if override:
        return Path(override)
    return JS_DEPS_DIR / "node_modules" / ".bin" / "esbuild"


def _node_available() -> bool:
    return shutil.which(_node_binary()) is not None


def _jsdom_installed() -> bool:
    return (JS_DEPS_DIR / "node_modules" / "jsdom" / "package.json").is_file()


def skip_if_unsupported() -> None:
    """Skip the calling test when node or jsdom is missing.

    CI installs both in the ``unit-tests`` job
    (``.github/workflows/pr.yml``). Local devs who haven't run
    ``npm install`` in ``tests/js/`` get a skip instead of a confusing
    failure.
    """
    if not _node_available():
        pytest.skip(
            "node not installed — install Node.js to run JS behaviour tests",
            allow_module_level=True,
        )
    if not _jsdom_installed():
        pytest.skip(
            "jsdom not installed — run `npm install` in tests/js/ to enable "
            "JS behaviour tests",
            allow_module_level=True,
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
    broadcast_channel_unavailable: bool = False,
) -> HarnessResult:
    """Run ``script`` in JSDOM and return observed side effects.

    Parameters mirror the JSON contract in ``tests/js/harness.mjs``.

    ``fetch_map`` keys are URL substrings; values are
    ``{"status": int, "body"?: str, "json"?: any, "throw"?: str,
    "responses"?: [...]}``. ``responses`` sequences per-call overrides;
    the last entry sticks after exhaustion. Missing routes default to
    404.

    ``invoke`` runs after the script body — use it to call a function
    the script exposes on ``window`` (e.g. ``"await window.restartAddon();"``)
    or to dispatch DOM events.

    ``settle_ms`` is virtual time, not wall time — set generously
    (default covers the 60 s addon-restart probe window with margin).

    ``broadcast_channel_unavailable`` deletes ``window.BroadcastChannel``
    before the script runs, so the production
    ``typeof BroadcastChannel === 'function'`` null-guard branch is
    exercised. Without this, JSDOM always provides BroadcastChannel and
    the guard never fires in tests.
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
        "broadcastChannelUnavailable": broadcast_channel_unavailable,
    }

    proc = subprocess.run(
        [_node_binary(), str(HARNESS_PATH)],
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
        [_node_binary(), str(EXTRACT_ASTRO_VARS_PATH)],
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


def _strip_astro_frontmatter(source: str, *, source_label: str = "<source>") -> str:
    """Drop the leading ``--- ... ---`` block if present.

    Astro frontmatter is TypeScript that runs at build-time, not JS that
    runs in the browser, so any ``<script>`` substring inside a comment
    or string there is not a real script tag. Stripping it before
    searching prevents matches against frontmatter comments.

    Raises ``ValueError`` when frontmatter opens (``---\\n``) but never
    closes — silently returning the source would let the caller find
    bogus ``<script>`` matches inside the frontmatter prose with no
    indication that the frontmatter itself is malformed.
    """
    if not source.startswith("---\n"):
        return source
    close = source.find("\n---\n", 4)
    if close == -1:
        raise ValueError(
            f"{source_label}: Astro frontmatter opens (---) but never closes",
        )
    return source[close + len("\n---\n") :]


def extract_script_body(source: str, *, source_label: str = "<source>") -> str:
    """Return the first non-external inline ``<script>`` body in ``source``.

    Handles both the bare ``<script>`` form used by ``_SETTINGS_HTML``
    and attributed forms like Astro's ``<script define:vars={...}>``.
    The body is everything between the opening tag's ``>`` and the next
    ``</script>``. External scripts (``<script src=...></script>``) are
    skipped — they have no inline body to extract. Astro frontmatter is
    skipped before searching so ``<script>`` mentions in frontmatter
    comments don't match.

    ``source_label`` (e.g. a file path) is included in the raised
    ``ValueError`` so callers know which surface was malformed.
    """
    search_source = _strip_astro_frontmatter(source, source_label=source_label)
    for match in re.finditer(r"<script\b([^>]*)>", search_source):
        attrs = match.group(1)
        if re.search(r"\bsrc\s*=", attrs):
            continue
        start = match.end()
        end = search_source.find("</script>", start)
        if end == -1:
            raise ValueError(f"{source_label}: unterminated <script> tag")
        return search_source[start:end]
    raise ValueError(f"{source_label}: no inline <script> tag found")


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
    source_path: Path
    script: str
    language: str
    """``"js"`` or ``"ts"`` — drives whether the harness transpiles
    before eval (Astro defaults inline ``<script>`` to TypeScript)."""


def _script_attr_language(attrs: str) -> str:
    """Map an Astro ``<script ...>`` attribute string to ``"js"`` / ``"ts"``.

    Astro defaults inline ``<script>`` to TypeScript since v3. The
    ``define:vars`` and ``is:inline`` directives both flip it to JS
    (define:vars injects JSON.parse calls into a JS body; is:inline
    emits the body verbatim). An explicit ``lang="js"`` likewise opts
    out of TS; ``lang="ts"`` is the explicit form matching the default.
    """
    if "define:vars" in attrs or "is:inline" in attrs:
        return "js"
    m = re.search(r"""\blang\s*=\s*['"]([a-z]+)['"]""", attrs)
    if m:
        return "js" if m.group(1).lower() == "js" else "ts"
    return "ts"


def discover_script_surfaces() -> list[ScriptSurface]:
    """Walk the repo for every rendered ``<script>`` surface that ships.

    Returns one entry per surface, in stable order. Future ``<script>``
    surfaces added under ``src/ha_mcp/**/*.py`` (registered in
    ``_PY_RENDERERS`` below) or any ``site/src/**/*.astro`` page are
    picked up automatically, so the parse-time guard extends without
    code changes to the test.
    """
    repo_root = Path(__file__).resolve().parents[3]
    surfaces: list[ScriptSurface] = []

    for surface_id, module_name, render in _PY_RENDERERS:
        try:
            module = __import__(module_name, fromlist=["__file__"])
        except ImportError as exc:
            raise RuntimeError(
                f"discover_script_surfaces: cannot import {module_name}: {exc}",
            ) from exc
        body = extract_script_body(render(), source_label=module_name)
        surfaces.append(
            ScriptSurface(
                surface_id=surface_id,
                source_path=Path(module.__file__),
                script=body,
                language="js",
            ),
        )

    # Astro pages and layouts. The site lives outside the package; walk
    # the static source. Raise rather than silently skip when site/src/
    # is missing — discovery returning a partial result would let a
    # whole-surface regression masquerade as success in the parse test.
    site_dir = repo_root / "site" / "src"
    if not site_dir.is_dir():
        raise RuntimeError(
            f"discover_script_surfaces: expected {site_dir} to exist; "
            "site source missing means no Astro surfaces would be covered",
        )
    for path in sorted(site_dir.rglob("*.astro")):
        text = _strip_astro_frontmatter(
            path.read_text(encoding="utf-8"),
            source_label=str(path.relative_to(repo_root)),
        )
        for match in re.finditer(r"<script\b([^>]*)>", text):
            attrs = match.group(1)
            if re.search(r"\bsrc\s*=", attrs):
                continue
            end = text.find("</script>", match.end())
            if end == -1:
                continue
            body = text[match.end() : end]
            rel = path.relative_to(site_dir).with_suffix("")
            surface_id = f"astro_{rel.as_posix().replace('/', '_')}"
            surfaces.append(
                ScriptSurface(
                    surface_id=surface_id,
                    source_path=path,
                    script=body,
                    language=_script_attr_language(attrs),
                ),
            )

    return surfaces


# Python-embedded UI surfaces. Each entry is
# ``(surface_id, importable_module, render_callable)``; the callable
# returns the rendered HTML (a module-level constant or a render
# function invoked with placeholder args). Add a new entry to register
# a new Python-rendered UI for parse coverage.
def _render_settings_html() -> str:
    from ha_mcp.settings_ui import _SETTINGS_HTML

    return _SETTINGS_HTML


def _render_consent_html() -> str:
    from ha_mcp.auth.consent_form import create_consent_html

    return create_consent_html(
        client_id="test-client",
        redirect_uri="https://test.local/cb",
        state="state-x",
        txn_id="txn-y",
    )


_PY_RENDERERS: list[tuple[str, str, Any]] = [
    ("settings_ui", "ha_mcp.settings_ui", _render_settings_html),
    ("consent_form", "ha_mcp.auth.consent_form", _render_consent_html),
]
