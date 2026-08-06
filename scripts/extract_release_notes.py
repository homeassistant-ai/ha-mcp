"""Extract one version's CHANGELOG section as a GitHub release body.

The release workflows used to do this with an inline `awk` one-liner and pipe
the result straight into `gh release create --notes-file`. That has no upper
bound, but the GitHub API caps a release body at 125,000 characters and rejects
anything longer with `HTTP 422: body is too long` -- which kills the release
*after* semantic-release has already committed the version bump and pushed the
tag, leaving a half-applied release to clean up by hand.

A section grows past the cap whenever python-semantic-release regenerates the
changelog over a long tag gap (it then collapses every commit since the last
reachable stable tag into a single section). So this script does the same
extraction, then truncates on a line boundary and appends a pointer to the full
changelog rather than letting the API reject the whole release.

Prints nothing on success; writes the notes to --out. An absent version yields
an empty file, which is the signal for the caller's own fallback text.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# GitHub's documented maximum for a release body. Characters, not bytes --
# the changelog carries emoji in headings, so a byte-based cut would be wrong.
GITHUB_RELEASE_BODY_LIMIT = 125_000

# Section headings look like "## v8.0.0 (2026-08-02)". The negative lookahead
# keeps "8.0.0" from also matching "## v8.0.01", which a bare prefix match
# (what the old awk did) accepts.
_NEXT_SECTION_RE = re.compile(r"^## v\d")

_FENCE_RE = re.compile(r"^\s*```")


def _heading_re(version: str) -> re.Pattern[str]:
    """Match the heading for exactly `version`, not a longer version sharing its prefix."""
    return re.compile(rf"^## v{re.escape(version.lstrip('v'))}(?![\w.])")


def extract_section(changelog: str, version: str) -> str:
    """Return the changelog body for `version`, or "" when the version is absent.

    The body is everything after the version's heading up to the next version
    heading (or end of file), with surrounding blank lines stripped.
    """
    heading = _heading_re(version)
    lines = changelog.splitlines(keepends=True)

    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return ""

    body = lines[start + 1 :]
    end = next((i for i, line in enumerate(body) if _NEXT_SECTION_RE.match(line)), None)
    if end is not None:
        body = body[:end]
    return "".join(body).strip()


def _truncation_notice(repo: str, version: str, limit: int) -> str:
    url = f"https://github.com/{repo}/blob/v{version.lstrip('v')}/CHANGELOG.md"
    return (
        "\n\n---\n\n"
        f"_Release notes truncated to fit GitHub's {limit:,}-character release-body "
        f"limit. Full changelog: {url}_\n"
    )


def _close_dangling_fence(text: str) -> str:
    """Append a closing fence if truncation cut inside a fenced code block."""
    if sum(1 for line in text.splitlines() if _FENCE_RE.match(line)) % 2:
        return text + "\n```"
    return text


def cap_to_limit(body: str, repo: str, version: str, limit: int) -> str:
    """Return `body` unchanged, or truncated on a line boundary plus a notice.

    Truncating on a line boundary keeps the markdown readable; a body whose
    very first line already blows the budget is cut mid-line rather than
    reduced to a bare notice.
    """
    if len(body) <= limit:
        return body

    notice = _truncation_notice(repo, version, limit)
    budget = limit - len(notice)
    if budget <= 0:  # pragma: no cover - only reachable with an absurd --limit
        return body[:limit]

    kept: list[str] = []
    used = 0
    for line in body.splitlines(keepends=True):
        if used + len(line) > budget:
            break
        kept.append(line)
        used += len(line)

    head = "".join(kept).rstrip() if kept else body[:budget].rstrip()
    return _close_dangling_fence(head) + notice


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Release version, e.g. 8.0.0")
    parser.add_argument(
        "--out", type=Path, required=True, help="Destination for the notes"
    )
    parser.add_argument(
        "--changelog", type=Path, default=Path("CHANGELOG.md"), help="Changelog to read"
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "homeassistant-ai/ha-mcp"),
        help="owner/name used in the truncation notice link (defaults to $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=GITHUB_RELEASE_BODY_LIMIT,
        help="Maximum release-body length in characters",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        changelog = args.changelog.read_text(encoding="utf-8")
    except OSError as e:
        print(f"extract_release_notes: {e}", file=sys.stderr)
        return 1

    body = extract_section(changelog, args.version)
    if body:
        # Cap the trailing newline too, so the written file never exceeds --limit.
        body = cap_to_limit(body + "\n", args.repo, args.version, args.limit)
    args.out.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
