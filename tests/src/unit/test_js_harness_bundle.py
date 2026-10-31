"""Unit tests for ``_js_harness.bundle_inline_script`` / ``module_exports``.

Pins what the setup-wizard tests rely on: an Astro page's two-line
``<script>`` entry is bundled with the modules it imports, build-time
constants are substituted, and a module's exported data can be read back
as JSON. Without these, a wizard module that fails to resolve would
surface as "feature didn't fire" in every wizard test at once.
"""

from __future__ import annotations

from pathlib import Path

from ._js_harness import bundle_inline_script, module_exports, run_script


def test_bundle_resolves_imports_and_build_constants(tmp_path: Path) -> None:
    """The entry's relative import resolves from the page directory and
    ``import.meta.env.BASE_URL`` takes the defined literal, so the bundle
    runs as a classic script in JSDOM."""
    (tmp_path / "greeting.ts").write_text(
        "export const greeting: string = `${import.meta.env.BASE_URL}/hello`;\n",
        encoding="utf-8",
    )
    entry = (
        "import { greeting } from './greeting';\n"
        "document.body.dataset.greeting = greeting;\n"
    )
    bundle = bundle_inline_script(
        entry, tmp_path, define={"import.meta.env.BASE_URL": '"/site"'}
    )

    result = run_script(bundle)

    assert result.errors == []
    assert 'data-greeting="/site/hello"' in result.dom


def test_module_exports_returns_named_values(tmp_path: Path) -> None:
    """Exported data comes back as JSON, so tests can build their DOM from
    the production arrays instead of a copy."""
    module = tmp_path / "data.ts"
    module.write_text(
        "export const items = ['a', 'b'];\nexport const label = 'Items';\n",
        encoding="utf-8",
    )

    assert module_exports(module, ["items", "label"]) == {
        "items": ["a", "b"],
        "label": "Items",
    }


def test_module_exports_with_no_names_skips_the_bundle(tmp_path: Path) -> None:
    """Nothing to read means nothing to bundle; the bundler would otherwise
    print code, not JSON."""
    assert module_exports(tmp_path / "missing.ts", []) == {}
