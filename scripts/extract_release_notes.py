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

# Truncation can land inside a block the changelog template opened: a fenced
# code block, or the `<details><summary>Internal Changes</summary>` element
# semantic-release wraps internal commits in. Both are closed after the cut --
# an unclosed `<details>` in particular would swallow the truncation notice
# into a collapsed section, so a reader sees no sign the notes are incomplete.
# Their lengths are reserved before lines are selected, so closing them can
# never push the body back over the limit.
_FENCE_CLOSE = "\n```"
_DETAILS_CLOSE = "\n</details>"


def _heading_re(version: str) -> re.Pattern[str]:
    """Match the heading for exactly `version`, not a longer version sharing its prefix.

    The lookahead rejects `.` and word characters (so `8.0.0` misses
    `## v8.0.01`) and also `-` / `+`, so a stable version never matches a
    prerelease or build-metadata heading like `## v8.0.0-rc.1`.
    """
    return re.compile(rf"^## v{re.escape(version.lstrip('v'))}(?![\w.+-])")


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


class _OpenBlocks:
    """Tracks which markdown blocks are still open as lines are consumed."""

    def __init__(self) -> None:
        self.in_fence = False
        self.details_depth = 0

    def feed(self, line: str) -> None:
        if _FENCE_RE.match(line):
            self.in_fence = not self.in_fence
            return
        if self.in_fence:  # `<details>` inside a code block is text, not markup
            return
        self.details_depth += line.count("<details>") - line.count("</details>")
        self.details_depth = max(self.details_depth, 0)

    @property
    def closers(self) -> str:
        """The suffix that closes everything still open, innermost first."""
        fence = _FENCE_CLOSE if self.in_fence else ""
        return fence + _DETAILS_CLOSE * self.details_depth


def cap_to_limit(body: str, repo: str, version: str, limit: int) -> str:
    """Return `body` unchanged, or truncated on a line boundary plus a notice.

    Truncating on a line boundary keeps the markdown readable. A line is only
    taken when the *closed* form still fits -- the closers for whatever it
    leaves open are charged against the budget before the line is accepted, so
    the result never exceeds `limit`.
    """
    if len(body) <= limit:
        return body

    notice = _truncation_notice(repo, version, limit)
    budget = limit - len(notice)
    if budget <= 0:  # pragma: no cover - only reachable with an absurd --limit
        return body[:limit]

    open_blocks = _OpenBlocks()
    kept: list[str] = []
    used = 0
    for line in body.splitlines(keepends=True):
        candidate = _OpenBlocks()
        candidate.in_fence, candidate.details_depth = (
            open_blocks.in_fence,
            open_blocks.details_depth,
        )
        candidate.feed(line)
        if used + len(line) + len(candidate.closers) > budget:
            break
        kept.append(line)
        used += len(line)
        open_blocks = candidate

    head = "".join(kept).rstrip() if kept else body[:budget].rstrip()
    closers = open_blocks.closers if kept else ""
    # Defensive clamp: rstrip only shortens, so this should already hold.
    overflow = len(head) + len(closers) + len(notice) - limit
    if overflow > 0:  # pragma: no cover - budget accounting makes this unreachable
        head = head[:-overflow]
    return head + closers + notice


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
