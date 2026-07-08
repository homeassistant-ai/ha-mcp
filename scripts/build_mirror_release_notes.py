"""Generate release notes for a homeassistant-ai/ha-mcp-integration mirror tag.

The mirror's release-on-tag workflow turns each pushed tag into a GitHub
release, but the deploy key that pushes the tag cannot call the GitHub API
to set a rich release body -- so the body has to travel inside the annotated
tag message itself. This script produces that message: a summary of what
changed in the HACS custom component (custom_components/ha_mcp_tools/) since
the previous stable server release, so HACS shows real release notes instead
of a generic stub.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Main repo's stable release tags look like v7.10.0; dev tags append
# ".devN" (e.g. v7.10.0.dev778) and the floating "stable" tag isn't a
# vX.Y.Z tag at all -- both must be excluded from stable-tag selection.
_STABLE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

# Commit subjects that touch custom_components/ha_mcp_tools/ only
# incidentally, via merge/publish mechanics -- not real component changes,
# so they'd just be noise in release notes.
_NOISE_SUBJECT_PREFIXES = ("chore(addon): publish dev addon version",)

_CHANGELOG_URL = "https://github.com/homeassistant-ai/ha-mcp/releases"

_COMPONENT_PATH = "custom_components/ha_mcp_tools/"


def _stable_version_key(tag: str) -> tuple[int, int, int]:
    """Numeric sort key for a stable tag, e.g. 'v7.10.0' -> (7, 10, 0)."""
    match = _STABLE_TAG_RE.match(tag)
    assert match, f"not a stable tag: {tag}"
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def select_stable_tags(tags: list[str]) -> tuple[str | None, str]:
    """Pick the two newest stable server release tags as (prev, curr).

    Filters to tags matching vX.Y.Z (excludes dev tags like
    'v7.10.0.dev778' and the floating 'stable' tag) and sorts numerically --
    a lexical sort would put 'v7.10.0' before 'v7.9.0'. Returns (None, curr)
    when only one stable tag exists (the first-ever stable release).
    """
    stable = sorted(
        (tag for tag in tags if _STABLE_TAG_RE.match(tag)), key=_stable_version_key
    )
    if not stable:
        raise ValueError("no stable tags (vX.Y.Z) found")
    if len(stable) == 1:
        return None, stable[0]
    return stable[-2], stable[-1]


def _filter_noise_subjects(subjects: list[str]) -> list[str]:
    """Drop commit subjects that are automation noise, not component changes."""
    return [s for s in subjects if not s.startswith(_NOISE_SUBJECT_PREFIXES)]


def format_release_notes(
    component_version: str,
    prev_tag: str | None,
    curr_tag: str,
    subjects: list[str],
) -> str:
    """Render the mirror tag's annotated-tag message as markdown.

    `subjects` are commit subject lines touching custom_components/ha_mcp_tools/
    in the range (prev_tag, curr_tag] of the main repo. `curr_tag` names the
    server release this notes body was generated from; `component_version` is
    the mirror's own tag version (the two numbering schemes are unrelated).
    """
    subjects = _filter_noise_subjects(subjects)
    lines = [f"## ha-mcp-tools {component_version}", ""]

    if prev_tag is None:
        lines.append("Initial component release.")
    else:
        lines.append(f"Synced from ha-mcp {curr_tag}.")
    lines.append("")

    if subjects:
        since = prev_tag if prev_tag is not None else "this component's introduction"
        lines.append(f"Component changes since {since}:")
        lines.extend(f"- {subject}" for subject in subjects)
    else:
        lines.append(
            "Component snapshot resync -- no changes to "
            f"{_COMPONENT_PATH} in this server release."
        )

    lines.append("")
    lines.append(f"Full server changelog: {_CHANGELOG_URL}")
    return "\n".join(lines) + "\n"


def _run_git(args: list[str], repo_dir: Path) -> str:
    """Run a git command in repo_dir, returning stripped stdout.

    Raises RuntimeError with git's stderr so callers surface an actionable
    message instead of a bare CalledProcessError traceback.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def discover_stable_tags(repo_dir: Path) -> tuple[str | None, str]:
    """Find the two newest stable server release tags in repo_dir."""
    output = _run_git(["tag", "-l", "v*"], repo_dir)
    tags = output.splitlines() if output else []
    return select_stable_tags(tags)


def collect_component_subjects(
    prev_tag: str | None, curr_tag: str, repo_dir: Path
) -> list[str]:
    """Commit subjects touching custom_components/ha_mcp_tools/ in (prev_tag, curr_tag]."""
    range_arg = curr_tag if prev_tag is None else f"{prev_tag}..{curr_tag}"
    output = _run_git(
        ["log", "--format=%s", range_arg, "--", _COMPONENT_PATH], repo_dir
    )
    return output.splitlines() if output else []


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component-version",
        required=True,
        help="Version from custom_components/ha_mcp_tools/manifest.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Destination path for the generated release notes",
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path.cwd(),
        help="Main repository checkout (defaults to cwd)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        prev_tag, curr_tag = discover_stable_tags(args.repo_dir)
        subjects = collect_component_subjects(prev_tag, curr_tag, args.repo_dir)
    except (RuntimeError, ValueError) as e:
        print(f"build_mirror_release_notes: {e}", file=sys.stderr)
        return 1

    notes = format_release_notes(args.component_version, prev_tag, curr_tag, subjects)
    args.out.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
