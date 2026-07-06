"""Compose the HACS mirror README from the prefix blurb and the main README.

The mirror repository (homeassistant-ai/ha-mcp-integration) renders its own
README on the HACS page. This script keeps the mirror's permanent prefix blurb
followed by the main repository README, rewriting repo-relative links and
images to absolute main-repo URLs so nothing 404s on the mirror page.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# The main repository the mirror is distributed from. Links resolve to the
# rendered blob page; images/media must resolve to raw content to display.
_BLOB_BASE = "https://github.com/homeassistant-ai/ha-mcp/blob/master/"
_RAW_BASE = "https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/"

# Horizontal-rule separator between the permanent prefix and the main README.
# Blank lines around the rule keep it an <hr>, not a setext heading underline.
_SEPARATOR = "\n\n---\n\n"

# Markdown link or image: a leading "!" marks an image.
#   [text](target)   or   ![alt](target "optional title")
_MD_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(\s*([^)\s]+)((?:\s+\"[^\"]*\")?)\s*\)")

# HTML src/href attribute with a single- or double-quoted value.
_HTML_ATTR_RE = re.compile(r"\b(src|href)\s*=\s*(\"[^\"]*\"|'[^']*')", re.IGNORECASE)


def _is_relative(target: str) -> bool:
    """Return True when target is a repo-relative path we should rewrite.

    Absolute URLs (any scheme), protocol-relative URLs, in-page anchors, and
    mailto/tel links are left untouched.
    """
    value = target.strip()
    if not value:
        return False
    if value.startswith("#"):  # in-page anchor
        return False
    if value.startswith("//"):  # protocol-relative (absolute)
        return False
    # Relative unless it carries a URI scheme (http/https/mailto/tel/...).
    return re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", value) is None


def _absolutize(target: str, *, is_image: bool) -> str:
    """Prefix a relative target with the raw base (images) or blob base (links)."""
    if not _is_relative(target):
        return target
    path = target[2:] if target.startswith("./") else target
    base = _RAW_BASE if is_image else _BLOB_BASE
    return base + path


def _rewrite_md(match: re.Match[str]) -> str:
    bang, text, target, title = match.groups()
    new_target = _absolutize(target, is_image=bool(bang))
    return f"{bang}[{text}]({new_target}{title})"


def _rewrite_html(match: re.Match[str]) -> str:
    attr, quoted = match.groups()
    quote = quoted[0]
    value = quoted[1:-1]
    # src is media (images/video) -> raw; href is a link -> blob.
    new_value = _absolutize(value, is_image=attr.lower() == "src")
    return f"{attr}={quote}{new_value}{quote}"


def rewrite_relative_links(markdown: str) -> str:
    """Rewrite repo-relative Markdown and HTML targets to absolute main-repo URLs."""
    rewritten = _MD_LINK_RE.sub(_rewrite_md, markdown)
    rewritten = _HTML_ATTR_RE.sub(_rewrite_html, rewritten)
    return rewritten


def compose_mirror_readme(prefix: str, readme: str) -> str:
    """Compose the mirror README: verbatim prefix, separator, rewritten main README."""
    body = rewrite_relative_links(readme)
    return prefix.rstrip("\n") + _SEPARATOR + body.strip("\n") + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        type=Path,
        required=True,
        help="Path to the permanent mirror README prefix blurb",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        required=True,
        help="Path to the main repository README.md",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Destination path for the composed mirror README",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    prefix = args.prefix.read_text(encoding="utf-8")
    readme = args.readme.read_text(encoding="utf-8")
    args.out.write_text(compose_mirror_readme(prefix, readme), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
