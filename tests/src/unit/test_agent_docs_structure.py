"""Structural checks for the repository's progressively disclosed guidance."""

from __future__ import annotations

import html
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).parents[3]
ROOT_AGENTS = ROOT / "AGENTS.md"
LINK_RE = re.compile(r"(?<!!)\[[^]]+\]\(\s*(?:<([^>]+)>|([^\s)]+))")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
EXPLICIT_ID_RE = re.compile(r"\s*\{#([^}]+)\}\s*$")


def _tracked_markdown() -> list[Path]:
    """Return tracked Markdown except generated release histories."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        ROOT / path
        for raw in result.stdout.split(b"\0")
        if raw
        for path in [Path(os.fsdecode(raw))]
        if path.name.upper() != "CHANGELOG.MD"
    ]


def _links(path: Path) -> list[str]:
    """Extract inline Markdown link destinations, excluding images."""
    text = path.read_text(encoding="utf-8")
    return [match.group(1) or match.group(2) for match in LINK_RE.finditer(text)]


def _local_target(source: Path, raw: str) -> tuple[Path, str] | None:
    """Resolve a repository-local Markdown destination and fragment."""
    parsed = urlsplit(html.unescape(raw))
    if parsed.scheme or parsed.netloc:
        return None
    relative = unquote(parsed.path)
    target = source if not relative else source.parent / relative
    target = target.resolve(strict=False)
    try:
        target.relative_to(ROOT.resolve())
    except ValueError:
        return None
    return target, unquote(parsed.fragment)


def _github_slug(text: str) -> str:
    """Approximate GitHub's stable heading slug for local-anchor validation."""
    text = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return re.sub(r"\s", "-", text.strip())


def _anchors(path: Path) -> set[str]:
    """Collect generated heading anchors and explicit ``{#id}`` anchors."""
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if not match:
            continue
        heading = match.group(2)
        explicit = EXPLICIT_ID_RE.search(heading)
        if explicit:
            anchors.add(explicit.group(1))
            heading = heading[: explicit.start()]
        base = _github_slug(heading)
        count = occurrences.get(base, 0)
        occurrences[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def test_tracked_markdown_local_links_resolve() -> None:
    """Every local file and heading link in tracked guidance must exist."""
    failures: list[str] = []
    for source in _tracked_markdown():
        for raw in _links(source):
            resolved = _local_target(source, raw)
            if resolved is None:
                continue
            target, fragment = resolved
            display = source.relative_to(ROOT)
            if not target.exists():
                failures.append(f"{display}: {raw!r} points to a missing path")
                continue
            if fragment and target.suffix.lower() == ".md":
                if fragment not in _anchors(target):
                    failures.append(
                        f"{display}: {raw!r} points to a missing heading in "
                        f"{target.relative_to(ROOT)}"
                    )
    assert not failures, "\n".join(failures)


def test_root_instruction_graph_and_claude_alias() -> None:
    """Root guidance must expose every chapter and retain its Claude alias."""
    claude = ROOT / "CLAUDE.md"
    assert claude.is_symlink(), "CLAUDE.md must remain a symlink to AGENTS.md"
    assert os.readlink(claude) == "AGENTS.md"

    linked = {
        target
        for raw in _links(ROOT_AGENTS)
        if (resolved := _local_target(ROOT_AGENTS, raw)) is not None
        for target, _fragment in [resolved]
    }
    chapters = set((ROOT / "docs" / "agents").glob("*.md"))
    missing = sorted(path.relative_to(ROOT).as_posix() for path in chapters - linked)
    assert not missing, f"AGENTS.md does not directly link agent chapters: {missing}"
