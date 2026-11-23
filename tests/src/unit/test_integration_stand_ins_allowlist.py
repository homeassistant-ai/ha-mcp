"""The allowlist of ``scripts/integration_stand_ins.py``: what an entry
exempts, and what the check refuses in the list itself."""

from __future__ import annotations

import pytest

from ._guard_run import run_guard
from ._integration_stand_ins_repo import LISTED, SAMPLE, sample_repo
from ._integration_stand_ins_repo import guard as guard


def test_an_allowlisted_case_passes(guard, tmp_path, capsys):
    repo = sample_repo(tmp_path, {SAMPLE: "mock_component(hass, 'hassio')\n"}, LISTED)

    assert run_guard(guard, repo, capsys) == (
        0,
        "No unlisted stand-in for a real integration; 1 allowlisted.\n",
    )


def test_an_allowlist_entry_that_matches_nothing_fails(guard, tmp_path, capsys):
    repo = sample_repo(tmp_path, {SAMPLE: "MockModule('made_up_probe')\n"}, LISTED)

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.splitlines()[0] == (
        "tests/src/unit/integration_stand_in_allowlist.yml: "
        f"{SAMPLE} no longer registers 'hassio'; remove the entry"
    )


def test_an_entry_allows_only_its_own_file(guard, tmp_path, capsys):
    other = "tests/addon/test_other.py"
    repo = sample_repo(
        tmp_path,
        {SAMPLE: "mock_component(hass, 'hassio')\n", other: "MockModule('hassio')\n"},
        LISTED,
    )

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.startswith(f"{other}:1: ")


@pytest.mark.parametrize(
    "allowlist",
    [
        f"- file: {SAMPLE}\n  integration: hassio\n",
        f"- file: {SAMPLE}\n  integration: hassio\n  reason: ''\n",
        LISTED + "  extra: 1\n",
        LISTED + "  1: x\n",
        f"- file: {SAMPLE}\n  integration: hassio\n  reason: '   '\n",
    ],
    ids=["no-reason", "empty-reason", "unknown-key", "non-string-key", "blank-reason"],
)
def test_an_entry_without_exactly_a_file_integration_and_reason_fails(
    guard, tmp_path, capsys, allowlist
):
    repo = sample_repo(
        tmp_path, {SAMPLE: "mock_component(hass, 'hassio')\n"}, allowlist
    )

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert "entry 1 needs exactly a non-empty file, integration, reason" in out


def test_a_key_repeated_in_an_entry_fails(guard, tmp_path, capsys):
    allowlist = LISTED.replace("  reason:", "  reason: ''\n  reason:")
    repo = sample_repo(
        tmp_path, {SAMPLE: "mock_component(hass, 'hassio')\n"}, allowlist
    )

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert "integration_stand_in_allowlist.yml:4: repeats the key 'reason'" in out


def test_an_entry_cannot_silence_the_checks_own_failures(guard, tmp_path, capsys):
    """More names than are checked stays a failure with its marker allowlisted."""
    names = ", ".join(f"'{letter}'" for letter in "abcdefghijklmnopq")
    source = (
        f"A = [{names}]\nB = [{names}]\n"
        "for a in A:\n    for b in B:\n        MockModule(f'{a}{b}')\n"
    )
    allowlist = LISTED.replace("integration: hassio", 'integration: "\\x01"')
    repo = sample_repo(tmp_path, {SAMPLE: source}, allowlist)

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert "entry 1 names no integration: '\\x01'" in out
    assert f"{SAMPLE}:5: more than 256 possible names to check" in out


@pytest.mark.parametrize(
    ("allowlist", "problem"),
    [
        pytest.param("{}\n", "must be a list of entries", id="mapping"),
        pytest.param("null\n", "must be a list of entries", id="null"),
        pytest.param("~\n", "must be a list of entries", id="tilde"),
        pytest.param("&rows [*rows]\n", "cannot be read", id="holds-itself"),
    ],
)
def test_an_allowlist_that_is_not_a_list_of_entries_fails(
    guard, tmp_path, capsys, allowlist, problem
):
    repo = sample_repo(tmp_path, {SAMPLE: "MockModule('made_up_probe')\n"}, allowlist)

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert problem in out


def test_an_allowlist_of_aliases_within_aliases_is_read_once(guard, tmp_path, capsys):
    """Each alias holds the one before it twice: read once for every place it
    stands, the sixtieth would never be done."""
    rows = "".join(f"- &a{n} [*a{n - 1}, *a{n - 1}]\n" for n in range(1, 60))
    repo = sample_repo(
        tmp_path, {SAMPLE: "MockModule('made_up_probe')\n"}, f"- &a0 [x]\n{rows}"
    )

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert "entry 60 needs exactly a non-empty file, integration, reason" in out


def test_a_refused_entry_still_names_its_file(guard, tmp_path):
    """The ratchet compares two versions of the list, also when one is bad."""
    listed = tmp_path / "allowlist.yml"
    listed.write_text(f"- file: {SAMPLE}\n  integration: hassio\n  reason: ''\n")
    problems: list[str] = []

    assert guard.load_allowlist(listed, problems) == {(SAMPLE, "hassio"): ""}
    assert problems
