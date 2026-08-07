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

Prints nothing on success; writes the notes to --out. A version that is absent
-- or present with an empty section, which two released versions in this repo's
own changelog are -- yields an empty file, which is the signal for the caller's
own fallback text. Which of the two happened is reported on stderr, so the run
log distinguishes a version we failed to find from one that genuinely shipped
an empty section.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

# GitHub's maximum for a release body. The API validates a character count,
# not a byte count -- its rejection reads "body is too long (maximum is 125000
# characters)" -- so the cut below is made in characters to match.
GITHUB_RELEASE_BODY_LIMIT = 125_000

# Section headings look like "## v8.0.0 (2026-08-02)", emitted by
# templates/CHANGELOG.md.j2. This pattern only has to find where the current
# section ends, so it deliberately matches any version heading; `_heading_re`
# below is the one that has to match a single exact version.
_NEXT_SECTION_RE = re.compile(r"^## v\d")

# A fence opens with three or more backticks or tildes, optionally followed by
# an info string. Both parts are captured: a fence can only be closed by the
# same character repeated at least as many times *and* nothing but whitespace
# after it. Closing a ````-fence with ```, or mistaking a second ```python for
# a closer, leaves it open -- and everything after the cut, the truncation
# notice included, then renders as code.
#
# At most three spaces of indentation: four or more makes the line indented
# code, not a fence, so treating it as one would open a fence that never
# closes and silence `<details>` tracking behind it.
_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")

_DETAILS_TAG_RE = re.compile(r"</?details>")

# Truncation can land inside a block the changelog template opened: a fenced
# code block, or the `<details><summary>Internal Changes</summary>` element
# semantic-release wraps internal commits in. Both are closed after the cut --
# an unclosed `<details>` in particular would swallow the truncation notice
# into a collapsed section, so a reader sees no sign the notes are incomplete.
# Their lengths are reserved before lines are selected, so closing them can
# never push the body back over the limit.
_DETAILS_CLOSE = "\n</details>"


def _heading_re(version: str) -> re.Pattern[str]:
    """Match the heading for exactly `version`, not a longer version sharing its prefix.

    The lookahead rejects `.` and word characters (so `8.0.0` misses
    `## v8.0.01`) and also `-` / `+`, so a stable version never matches a
    prerelease or build-metadata heading like `## v8.0.0-rc.1`.
    """
    return re.compile(rf"^## v{re.escape(version.removeprefix('v'))}(?![\w.+-])")


def extract_section(changelog: str, version: str) -> str | None:
    """Return the changelog body for `version`, or None when it has no heading.

    The body is everything after the version's heading up to the next version
    heading (or end of file), with surrounding blank lines stripped.

    None and "" are different answers: None means no `## v<version>` heading
    exists, "" means the heading is there but its section is empty (`## v4.8.0`
    and `## v1.0.0` in this repo's changelog both are). Both leave the caller
    writing fallback notes, but only the first says the extractor failed to
    find the release -- so they are reported differently.
    """
    heading = _heading_re(version)
    lines = changelog.splitlines(keepends=True)

    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return None

    body = lines[start + 1 :]
    end = next((i for i, line in enumerate(body) if _NEXT_SECTION_RE.match(line)), None)
    if end is not None:
        body = body[:end]
    return "".join(body).strip()


def _truncation_notice(repo: str, version: str, limit: int) -> str:
    url = f"https://github.com/{repo}/blob/v{version.removeprefix('v')}/CHANGELOG.md"
    return (
        "\n\n---\n\n"
        f"_Release notes truncated to fit GitHub's {limit:,}-character release-body "
        f"limit. Full changelog: {url}_\n"
    )


@dataclass(frozen=True)
class _OpenBlocks:
    """Which markdown blocks are still open after consuming some lines.

    Immutable so that `feed` can be used to ask "what would the state be if I
    took this line?" without committing to it -- which is exactly what the
    budget check in `cap_to_limit` needs.
    """

    # The delimiter of the open fence ("```", "````", "~~~"), or None, and the
    # indentation it opened at -- the closer has to reproduce that indentation
    # or it leaves the container the fence lives in instead of closing it.
    fence: str | None = None
    fence_indent: str = ""
    details_depth: int = 0

    def feed(self, line: str) -> _OpenBlocks:
        """The state after `line`, leaving this instance untouched."""
        match = _FENCE_RE.match(line)
        if match:
            indent, delimiter, trailing = match.group(1), match.group(2), match.group(3)
            if self.fence is not None:
                # Only the same character, repeated at least as often and
                # carrying nothing but whitespace, closes it. A closing fence
                # cannot have an info string, so ```python inside a ```-block
                # is ordinary content.
                if (
                    delimiter[0] == self.fence[0]
                    and len(delimiter) >= len(self.fence)
                    and not trailing.strip()
                ):
                    return replace(self, fence=None, fence_indent="")
                return self
            # A backtick fence's info string may not itself contain a backtick.
            # GFM reads such a line as ordinary text, so opening a fence here
            # would make the closer emitted later read as a new opening fence
            # and swallow everything after it, the notice included.
            if delimiter[0] != "`" or "`" not in trailing:
                return replace(self, fence=delimiter, fence_indent=indent)
            # Not a fence after all -- fall through and treat it as text.
        if self.fence:  # `<details>` inside a code block is text, not markup
            return self
        # Left to right, clamping after each close, rather than netting the
        # line's tags: `</details><details>` nets to zero but really leaves one
        # open, and its closer would then be dropped.
        depth = self.details_depth
        for tag in _DETAILS_TAG_RE.findall(line):
            depth = depth + 1 if tag == "<details>" else max(depth - 1, 0)
        return replace(self, details_depth=depth)

    @property
    def closers(self) -> str:
        """The suffix that closes everything still open, innermost first."""
        fence = f"\n{self.fence_indent}{self.fence}" if self.fence else ""
        return fence + _DETAILS_CLOSE * self.details_depth


def cap_to_limit(body: str, repo: str, version: str, limit: int) -> str:
    """Return `body` unchanged, or truncated on a line boundary plus a notice.

    Truncating on a line boundary keeps the markdown readable. A line is only
    taken when the *closed* form still fits -- the closers for whatever it
    leaves open are charged against the budget before the line is accepted, so
    the result never exceeds `limit`.

    Raises ValueError when `limit` leaves no room for the truncation notice, or
    no room for even the first complete line beyond it. Silently emitting a
    notice-free body would hand GitHub a release that looks complete but is
    not; a body of nothing but "these notes were truncated" is not worth
    publishing; and a mid-line cut would strand an opening fence or `<details>`
    with no closer, swallowing the notice. An unusable --limit fails loudly
    instead of producing any of the three.
    """
    if len(body) <= limit:
        return body

    notice = _truncation_notice(repo, version, limit)
    budget = limit - len(notice)
    if budget <= 0:
        raise ValueError(
            f"--limit {limit} cannot hold the {len(notice)}-character truncation "
            "notice, so the notes would be silently cut with no indication"
        )

    open_blocks = _OpenBlocks()
    kept: list[str] = []
    used = 0
    for line in body.splitlines(keepends=True):
        candidate = open_blocks.feed(line)
        if used + len(line) + len(candidate.closers) > budget:
            break
        kept.append(line)
        used += len(line)
        open_blocks = candidate

    if not kept:
        raise ValueError(
            f"--limit {limit} cannot retain even the first complete line of the "
            "notes; cutting mid-line would leave any opening fence or <details> "
            "unclosed and swallow the truncation notice"
        )

    head = "".join(kept).rstrip()
    closers = open_blocks.closers
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
    except (OSError, UnicodeDecodeError) as e:
        # UnicodeDecodeError subclasses ValueError, not OSError, so a changelog
        # with a bad byte would otherwise exit on a raw traceback.
        print(f"extract_release_notes: {e}", file=sys.stderr)
        return 1

    version = args.version.removeprefix("v")
    section = extract_section(changelog, args.version)
    body = ""
    if section is None:
        print(
            f"extract_release_notes: no '## v{version}' heading in {args.changelog}",
            file=sys.stderr,
        )
    elif not section:
        print(
            f"extract_release_notes: the '## v{version}' section is empty",
            file=sys.stderr,
        )
    else:
        try:
            # Cap the trailing newline too, so the written file never exceeds --limit.
            body = cap_to_limit(section + "\n", args.repo, args.version, args.limit)
        except ValueError as e:
            print(f"extract_release_notes: {e}", file=sys.stderr)
            return 1
    try:
        # newline="\n" so the file on disk matches the character budget the cap
        # was computed against, on any platform rather than only on the runner.
        args.out.write_text(body, encoding="utf-8", newline="\n")
    except OSError as e:
        print(f"extract_release_notes: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
