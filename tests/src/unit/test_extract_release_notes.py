"""Unit tests for scripts/extract_release_notes.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "extract_release_notes.py"
_spec = importlib.util.spec_from_file_location("extract_release_notes", _SCRIPT)
assert _spec and _spec.loader
extract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract)

REPO = "homeassistant-ai/ha-mcp"
LIMIT = extract.GITHUB_RELEASE_BODY_LIMIT

CHANGELOG = """# CHANGELOG

<!-- version list -->

## v8.0.0 (2026-08-02)

### Features

- Add the thing ([#100](https://github.com/x/y/pull/100))

## v7.14.2 (2026-07-22)

### Bug Fixes

- Fix the other thing ([#99](https://github.com/x/y/pull/99))
"""


def test_extracts_section_between_two_headings() -> None:
    body = extract.extract_section(CHANGELOG, "8.0.0")
    assert "- Add the thing" in body
    assert "Fix the other thing" not in body
    assert "## v7.14.2" not in body


def test_extracts_trailing_section_to_end_of_file() -> None:
    body = extract.extract_section(CHANGELOG, "7.14.2")
    assert "- Fix the other thing" in body
    assert body.endswith("pull/99))"), "trailing blank lines must be stripped"


def test_leading_v_on_version_is_accepted() -> None:
    assert extract.extract_section(CHANGELOG, "v8.0.0") == extract.extract_section(
        CHANGELOG, "8.0.0"
    )


def test_absent_version_yields_empty_so_caller_fallback_engages() -> None:
    assert extract.extract_section(CHANGELOG, "9.9.9") == ""


@pytest.mark.parametrize(
    "heading",
    [
        "## v8.0.01 (2026-08-02)",  # longer version sharing the prefix
        "## v8.0.0-rc.1 (2026-08-02)",  # prerelease
        "## v8.0.0+build.5 (2026-08-02)",  # build metadata
    ],
)
def test_version_does_not_match_a_longer_version_sharing_its_prefix(
    heading: str,
) -> None:
    """'8.0.0' must match none of these -- the old awk prefix match matched all."""
    changelog = f"# CHANGELOG\n\n{heading}\n\nwrong section\n"
    assert extract.extract_section(changelog, "8.0.0") == ""


def test_a_prerelease_version_still_finds_its_own_section() -> None:
    changelog = "# CHANGELOG\n\n## v8.0.0-rc.1 (2026-08-02)\n\n- the rc change\n"
    assert "- the rc change" in extract.extract_section(changelog, "8.0.0-rc.1")


def test_body_under_the_limit_is_returned_verbatim() -> None:
    body = "### Features\n\n- Add the thing\n"
    assert extract.cap_to_limit(body, REPO, "8.0.0", LIMIT) == body


def test_oversized_body_is_truncated_under_the_limit_with_a_pointer() -> None:
    """Regression: a 155k-char section 422'd the release (crs2007/ha-mcp run 31003968475)."""
    body = "".join(f"- change number {i}\n" for i in range(12_000))
    assert len(body) > LIMIT

    capped = extract.cap_to_limit(body, REPO, "8.0.0", LIMIT)

    assert len(capped) <= LIMIT
    assert "truncated" in capped
    assert f"https://github.com/{REPO}/blob/v8.0.0/CHANGELOG.md" in capped
    assert "- change number 0\n" in capped


def test_truncation_cuts_on_a_line_boundary() -> None:
    body = "".join(f"- change number {i}\n" for i in range(12_000))
    capped = extract.cap_to_limit(body, REPO, "8.0.0", LIMIT)

    kept = capped.split("\n\n---\n\n")[0]
    assert all(line.startswith("- change number ") for line in kept.splitlines())


def _fenced_body(line_width: int) -> str:
    """An over-limit fenced block whose lines are `line_width` chars incl. newline."""
    line = "x" * (line_width - 1) + "\n"
    return "```python\n" + line * 40_000 + "```\n"


def test_truncation_closes_a_code_fence_it_cut_into() -> None:
    body = _fenced_body(12)
    assert len(body) > LIMIT, "body must actually need truncating"

    capped = extract.cap_to_limit(body, REPO, "8.0.0", LIMIT)

    kept = capped.split("\n\n---\n\n")[0]
    assert kept.count("```") % 2 == 0, "truncation left an unclosed code fence"


def _details_body(line_width: int) -> str:
    """An over-limit body wrapped in the changelog's `<details>` element."""
    line = "- " + "x" * (line_width - 3) + "\n"
    return (
        "### Features\n\n- visible change\n\n"
        "<details>\n<summary>Internal Changes</summary>\n\n"
        + line * 40_000
        + "\n</details>\n"
    )


def test_truncation_notice_is_not_swallowed_by_an_open_details_block() -> None:
    """An unclosed `<details>` would collapse the notice out of sight."""
    capped = extract.cap_to_limit(_details_body(30), REPO, "8.0.0", LIMIT)

    assert capped.count("<details>") == capped.count("</details>"), (
        "truncation left a <details> open, hiding the notice in a collapsed section"
    )
    assert capped.index("</details>") < capped.index("Release notes truncated"), (
        "the notice must sit outside the collapsed block"
    )


@pytest.mark.parametrize("line_width", [8, 9, 12, 13, 30])
def test_closing_details_never_pushes_the_body_over_the_limit(line_width: int) -> None:
    capped = extract.cap_to_limit(_details_body(line_width), REPO, "8.0.0", LIMIT)
    assert len(capped) <= LIMIT


@pytest.mark.parametrize("line_width", [6, 7, 11, 12, 40])
def test_closing_fence_never_pushes_the_body_over_the_limit(line_width: int) -> None:
    """The closing fence is appended after the cut, so its length must be reserved.

    Widths 6/7/11 are boundary cases where the line-boundary cut lands within
    four characters of the budget -- without the reservation these produced
    125,001-125,002 characters and GitHub would still 422.
    """
    capped = extract.cap_to_limit(_fenced_body(line_width), REPO, "8.0.0", LIMIT)
    assert len(capped) <= LIMIT


def test_a_limit_too_small_for_the_notice_fails_loudly() -> None:
    """Silently emitting a notice-free body would look like complete notes."""
    body = "".join(f"- change number {i}\n" for i in range(12_000))
    notice_len = len(extract._truncation_notice(REPO, "8.0.0", 50))

    with pytest.raises(ValueError, match="truncation notice"):
        extract.cap_to_limit(body, REPO, "8.0.0", notice_len)


def test_main_exits_nonzero_on_an_unusable_limit(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    section = "".join(f"- change number {i}\n" for i in range(12_000))
    changelog.write_text(
        f"# CHANGELOG\n\n## v8.0.0 (2026-08-02)\n\n{section}", encoding="utf-8"
    )
    out = tmp_path / "release_notes.md"

    rc = extract.main(
        [
            "--version",
            "8.0.0",
            "--changelog",
            str(changelog),
            "--out",
            str(out),
            "--limit",
            "10",
        ]
    )

    assert rc == 1
    assert not out.exists(), "no half-formed notes file on failure"


def test_main_writes_capped_notes_to_out(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    section = "".join(f"- change number {i}\n" for i in range(12_000))
    changelog.write_text(
        f"# CHANGELOG\n\n## v8.0.0 (2026-08-02)\n\n{section}", encoding="utf-8"
    )
    out = tmp_path / "release_notes.md"

    rc = extract.main(
        [
            "--version",
            "8.0.0",
            "--changelog",
            str(changelog),
            "--out",
            str(out),
            "--repo",
            REPO,
        ]
    )

    assert rc == 0
    written = out.read_text(encoding="utf-8")
    assert len(written) <= LIMIT, "written file must fit GitHub's release-body limit"
    assert "truncated" in written


def test_main_writes_empty_file_when_version_is_absent(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    out = tmp_path / "release_notes.md"

    assert (
        extract.main(
            ["--version", "9.9.9", "--changelog", str(changelog), "--out", str(out)]
        )
        == 0
    )
    assert out.read_text(encoding="utf-8") == ""


def test_main_reports_a_missing_changelog_instead_of_traceback(tmp_path: Path) -> None:
    rc = extract.main(
        [
            "--version",
            "8.0.0",
            "--changelog",
            str(tmp_path / "nope.md"),
            "--out",
            str(tmp_path / "out.md"),
        ]
    )
    assert rc == 1
