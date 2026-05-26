"""Load skill reference files from the bundled skills-vendor directory.

Shared helper for the consolidated ``ha_get_skill_guide`` tool and the
write-tool ``include_skill`` parameter. Mirrors the symlink + path-traversal
guards in ``server.py::_handle_skill_guide_call`` so any caller that needs to
read a ``(skill, file)`` pair gets the same safety contract.

Functions:

* :func:`get_skills_dir` — returns the on-disk skills root when the vendor
  submodule is present, else ``None``.
* :func:`resolve_skill_files` — reads N files from one skill and returns
  ``{relative_path: body}``. Silently skips missing files, symlinks, and
  path-traversal attempts so a partial map is returned rather than raising.

The silent-skip contract is deliberate. Callers (write tools) attach the
returned dict to a ``skill_content`` response field. A missing reference
should never fail the surrounding write operation — the agent still gets
any warnings plus whatever files did resolve. The strict-error path stays
with ``_handle_skill_guide_call``, which must raise ``ToolError`` for the
explicit user-requested file lookup.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


def _skills_dir_at(root: Path) -> Path | None:
    """Return ``root`` if it contains at least one skill directory, else None."""
    if not root.is_dir():
        return None
    for entry in root.iterdir():
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            return root
    return None


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
    """Read N reference files from one skill, returning ``{rel_path: body}``.

    Silently skips missing files, symlinks, and any path that escapes the
    skill directory. Returns an empty dict when ``skills_dir`` is ``None``
    (vendor submodule missing), when the named skill doesn't exist, or when
    no requested files resolve cleanly.

    Args:
        skills_dir: The skills root (from :func:`get_skills_dir`), or ``None``.
        skill: The skill name (subdirectory name under ``skills_dir``).
        files: Iterable of file paths relative to the skill directory
            (e.g. ``"SKILL.md"`` or ``"references/automation-patterns.md"``).

    Returns:
        Dict mapping each successfully-read relative path to its file body.
        Paths that fail any safety check are silently omitted.
    """
    if skills_dir is None:
        return {}
    skill_dir = skills_dir / skill
    if not skill_dir.is_dir():
        return {}
    skill_root = skill_dir.resolve()

    out: dict[str, str] = {}
    for rel in files:
        if not rel:
            continue
        candidate = skill_dir / rel
        # Pre-resolve symlink check: ``resolve()`` returns the canonical
        # non-symlink path, so a post-resolve ``is_symlink()`` check would
        # always be False. Matches the guard in
        # ``server.py::_handle_skill_guide_call``.
        if candidate.is_symlink():
            logger.debug("Refusing symlink in skill: %s/%s", skill, rel)
            continue
        try:
            target = candidate.resolve()
        except OSError as e:
            logger.debug("Could not resolve %s/%s: %s", skill, rel, e)
            continue
        if not target.is_relative_to(skill_root) or not target.is_file():
            logger.debug("Refusing %s/%s — outside skill dir or not a file", skill, rel)
            continue
        try:
            out[rel] = target.read_text(encoding="utf-8")
        except OSError as e:
            logger.debug("Could not read %s/%s: %s", skill, rel, e)
            continue
    return out
