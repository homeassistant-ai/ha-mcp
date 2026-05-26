"""Load skill reference files from the bundled skills-vendor directory.

Shared helper for the consolidated ``ha_get_skill_guide`` tool and the
write-tool ``attach_skill_payload`` parameter. Mirrors the symlink + path-traversal
guards in ``server.py::_handle_skill_guide_call`` so any caller that needs to
read a ``(skill, file)`` pair gets the same safety contract.

Functions:

* :func:`get_skills_dir` — returns the on-disk skills root when the vendor
  submodule is present, else ``None``.
* :func:`resolve_skill_files` — reads N file refs from one skill and returns
  ``{ref: body_or_section}``. Each ref is either a bare file path
  (``"references/X.md"``) or a path with an anchor
  (``"references/X.md#native-conditions"``); anchored refs yield just the
  matching markdown section so reactive embeds don't ship 20 KB when only
  one 2 KB section is relevant. Silently skips missing files, missing
  anchors, symlinks, and path-traversal attempts.
* :func:`extract_section` — markdown heading-based slicer used by
  ``resolve_skill_files`` and exposed for callers that already have a file
  body in hand.

The silent-skip contract is deliberate. Callers (write tools) attach the
returned dict to a ``skill_content`` response field. A missing reference
should never fail the surrounding write operation — the agent still gets
any warnings plus whatever files did resolve. The strict-error path stays
with ``_handle_skill_guide_call``, which must raise ``ToolError`` for the
explicit user-requested file lookup.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

# GitHub-flavored markdown heading slugifier — matches the anchor format
# used by the skill:// URIs the best-practice checker emits
# (e.g. "## if/then vs choose" → "ifthen-vs-choose").
_HEADING_LINE_RE = re.compile(r"^(#+)\s+(.+?)\s*$")
_SLUG_STRIP = re.compile(r"[^\w\s-]")
_SLUG_SPACES = re.compile(r"\s+")


def _slugify(heading: str) -> str:
    """Slugify a markdown heading the way GitHub does for anchor links."""
    s = heading.strip().lower()
    s = _SLUG_STRIP.sub("", s)
    s = _SLUG_SPACES.sub("-", s)
    return s


def extract_section(body: str, anchor: str) -> str | None:
    """Return the markdown section whose heading slugifies to ``anchor``.

    Walks the body line-by-line for ``#``-prefixed headings; on the first
    match, captures from that heading until (but not including) the next
    heading at the same level OR a higher level (lower ``#`` count). The
    section ends at EOF when no closing heading is found.

    Returns ``None`` when no heading matches ``anchor``.

    A bare anchor that resolves to the file's only/top heading (e.g.
    ``#template-guidelines`` matching ``# Template Guidelines``) returns
    essentially the whole file — that's fine, the caller asked for the
    section that happens to span everything.
    """
    if not anchor:
        return body  # No anchor → whole file (convenience for callers).

    # Slugify the caller-provided anchor too — otherwise asymmetric
    # comparison silently misses on trailing whitespace, double-hash
    # typos (``"file.md##x"`` → anchor=``"#x"``), or mixed-case anchors.
    # Current ``_emit`` sites all pre-slugify, but normalising here makes
    # the contract robust to future typos.
    target = _slugify(anchor)
    if not target:
        return None  # Anchor that slugifies to empty (e.g. all punctuation).

    lines = body.splitlines(keepends=True)
    section_start: int | None = None
    section_level: int | None = None
    in_fence = False
    for i, line in enumerate(lines):
        # Skip fenced code blocks — YAML/bash examples use ``# comment``
        # which would otherwise be misread as a markdown H1 heading and
        # silently close a section after its first code example.
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_LINE_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        heading = m.group(2)
        if section_start is None:
            if _slugify(heading) == target:
                section_start = i
                section_level = level
        elif section_level is not None and level <= section_level:
            # Next heading at same/higher level closes the section.
            return "".join(lines[section_start:i])
    if section_start is not None:
        return "".join(lines[section_start:])
    return None


def _skills_dir_at(root: Path) -> Path | None:
    """Return ``root`` if the directory exists, else None.

    Matches the historical behaviour of ``server._get_skills_dir`` — a
    bare existence check is enough because the vendored submodule either
    checks out cleanly with all skills present, or doesn't check out at
    all. There's no intermediate "empty submodule" state to defend
    against here; the path-traversal guards in
    :func:`resolve_skill_files` are the real safety layer.
    """
    return root if root.exists() else None


def get_skills_dir() -> Path | None:
    """Return the bundled skills root path, or ``None`` if not present.

    Resolves to ``src/ha_mcp/resources/skills-vendor/skills/`` relative to
    this module. Returns ``None`` when the git submodule isn't initialised
    (e.g. fresh clone without ``git submodule update --init``).
    """
    skills_dir = Path(__file__).parent.parent / "resources" / "skills-vendor" / "skills"
    return _skills_dir_at(skills_dir)


def resolve_skill_files(
    skills_dir: Path | None,
    skill: str,
    files: Iterable[str],
) -> dict[str, str]:
    """Read N reference files (or sections) from one skill.

    Each entry in ``files`` is either a bare file path or a path with a
    ``#anchor`` suffix:

    * ``"references/automation-patterns.md"`` → full file body.
    * ``"references/automation-patterns.md#native-conditions"`` → only the
      markdown section whose heading slugifies to ``native-conditions``.

    The same file requested both bare AND anchored produces two entries
    keyed by the original request strings. Each on-disk file is read at
    most once regardless of how many sections are requested from it.

    Silently skips missing files, missing anchors, symlinks, and any path
    that escapes the skill directory. Returns an empty dict when
    ``skills_dir`` is ``None`` (vendor submodule missing), when the named
    skill doesn't exist, or when nothing resolves.

    Args:
        skills_dir: The skills root (from :func:`get_skills_dir`), or ``None``.
        skill: The skill name (subdirectory name under ``skills_dir``).
        files: Iterable of ``"path"`` or ``"path#anchor"`` entries relative
            to the skill directory.

    Returns:
        Dict mapping each successfully-resolved request string to its body
        or extracted section. Failures (missing file, missing anchor,
        traversal/symlink reject) are silently omitted.
    """
    if skills_dir is None:
        return {}
    skill_dir = skills_dir / skill
    if not skill_dir.is_dir():
        return {}
    skill_root = skill_dir.resolve()

    # Group requests by file path so each file is read only once even when
    # multiple sections are requested from it.
    by_path: dict[str, list[tuple[str, str]]] = {}
    for rel in files:
        if not rel:
            continue
        path, _, anchor = rel.partition("#")
        by_path.setdefault(path, []).append((rel, anchor))

    out: dict[str, str] = {}
    for path, requests in by_path.items():
        body = _read_file_safely(skill_dir, skill_root, skill, path)
        if body is None:
            continue
        for original_ref, anchor in requests:
            if anchor:
                section = extract_section(body, anchor)
                if section is not None:
                    out[original_ref] = section
                else:
                    # Missing anchor usually means a checker emission site
                    # references a heading that doesn't exist in the .md
                    # (caller typo or vendor submodule reorganised the
                    # heading). Either is a real bug — surface at WARNING,
                    # not silently drop.
                    logger.warning(
                        "Skill anchor %r did not match any heading in %s/%s — "
                        "either a checker typo or the vendor submodule changed.",
                        anchor,
                        skill,
                        path,
                    )
            else:
                out[original_ref] = body
    return out


def _read_file_safely(
    skill_dir: Path, skill_root: Path, skill: str, rel_path: str
) -> str | None:
    """Read ``skill_dir / rel_path`` with symlink + traversal guards.

    Returns ``None`` for any failure mode — caller accumulates a partial
    map rather than propagating the error.

    Log levels are tuned for production visibility: write tools' silent-
    degrade contract means the operator only ever learns about a missing
    skill via the logs, so security events and missing files surface at
    WARNING.
    """
    candidate = skill_dir / rel_path
    # Pre-resolve symlink check: ``resolve()`` returns the canonical
    # non-symlink path, so a post-resolve ``is_symlink()`` check would
    # always be False. Matches the guard in
    # ``server.py::_handle_skill_guide_call``.
    if candidate.is_symlink():
        logger.warning("Refusing symlink in skill (security): %s/%s", skill, rel_path)
        return None
    try:
        target = candidate.resolve()
    except OSError as e:
        logger.warning("Could not resolve %s/%s: %s", skill, rel_path, e)
        return None
    if not target.is_relative_to(skill_root):
        logger.warning(
            "Refusing %s/%s — escapes skill dir (path-traversal guard)",
            skill,
            rel_path,
        )
        return None
    if not target.is_file():
        # Missing file or non-regular file (directory, device, etc.).
        # Either is caller bug or submodule-version drift — operator
        # visibility matters for both.
        logger.warning(
            "Skill file %s/%s does not exist or is not a regular file",
            skill,
            rel_path,
        )
        return None
    try:
        return target.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Could not read %s/%s: %s", skill, rel_path, e)
        return None
