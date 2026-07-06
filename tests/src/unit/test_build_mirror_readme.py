"""Unit tests for scripts/build_mirror_readme.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "build_mirror_readme.py"
_spec = importlib.util.spec_from_file_location("build_mirror_readme", _SCRIPT)
assert _spec and _spec.loader
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

BLOB = "https://github.com/homeassistant-ai/ha-mcp/blob/master/"
RAW = "https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/"


def test_relative_markdown_link_becomes_blob_url() -> None:
    out = build.rewrite_relative_links("See [Contributing](CONTRIBUTING.md) first.")
    assert f"[Contributing]({BLOB}CONTRIBUTING.md)" in out


def test_relative_markdown_link_preserves_anchor_fragment() -> None:
    out = build.rewrite_relative_links(
        "[opt](homeassistant-addon/DOCS.md#enable_tool_search)"
    )
    assert f"({BLOB}homeassistant-addon/DOCS.md#enable_tool_search)" in out


def test_relative_markdown_image_becomes_raw_url() -> None:
    out = build.rewrite_relative_links("![logo](docs/img/logo.png)")
    assert f"![logo]({RAW}docs/img/logo.png)" in out


def test_relative_html_img_src_becomes_raw_url() -> None:
    out = build.rewrite_relative_links('<img src="docs/img/demo.webp" alt="demo"/>')
    assert f'src="{RAW}docs/img/demo.webp"' in out


def test_relative_html_href_becomes_blob_url() -> None:
    out = build.rewrite_relative_links('<a href="LICENSE.md">License</a>')
    assert f'href="{BLOB}LICENSE.md"' in out


def test_absolute_url_untouched() -> None:
    src = '[site](https://example.com/x) and <img src="https://cdn.test/x.png"/>'
    assert build.rewrite_relative_links(src) == src


def test_pure_anchor_and_mailto_untouched() -> None:
    src = "[jump](#features) and [mail](mailto:a@b.com)"
    assert build.rewrite_relative_links(src) == src


def test_compose_order_prefix_separator_content() -> None:
    result = build.compose_mirror_readme("PREFIX BLURB", "# Main\n[x](docs/y.md)")
    assert result.startswith("PREFIX BLURB\n\n---\n\n# Main")
    prefix_idx = result.index("PREFIX BLURB")
    separator_idx = result.index("\n\n---\n\n")
    content_idx = result.index("# Main")
    assert prefix_idx < separator_idx < content_idx
    assert f"[x]({BLOB}docs/y.md)" in result
    assert result.endswith("\n")
