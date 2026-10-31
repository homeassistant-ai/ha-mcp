"""Git run by a unit test acts on the test's own repository and reads no config
from the git hook lefthook runs it in or from the contributor (#2907); see
``_unit_test_env`` in ``conftest.py``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def hook_environment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Set the hook variables that redirect git or inject config, plus a global config.

    ``GIT_DIR`` and ``GIT_INDEX_FILE`` as a hook in a linked worktree gets
    them, ``GIT_CONFIG_PARAMETERS`` as from ``git -c ... commit``,
    ``GIT_CONFIG_COUNT`` as a third config source a fixed list of names would
    miss, and a ``user.name`` in ``$HOME/.gitconfig``. ``GIT_CONFIG_GLOBAL`` is
    unset so that git reads that file.

    Under lefthook the variables are set from the start of the process; module
    scope is the closest a test in the same process gets, since it puts them in
    place before the function-scoped ``_unit_test_env`` runs. ``GIT_DIR`` names
    a path that does not exist yet, so a leak creates a repository there rather
    than touching a real one.
    """
    home = tmp_path_factory.mktemp("home")
    (home / ".gitconfig").write_text("[user]\n\tname = Contributor\n", encoding="utf-8")
    decoy = tmp_path_factory.mktemp("decoy") / "repo.git"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        mp.delenv("GIT_CONFIG_GLOBAL", raising=False)
        mp.setenv("GIT_DIR", str(decoy))
        mp.setenv("GIT_INDEX_FILE", str(decoy / "index"))
        mp.setenv("GIT_CONFIG_PARAMETERS", "'user.email'='hook@example.invalid'")
        mp.setenv("GIT_CONFIG_COUNT", "1")
        mp.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
        mp.setenv("GIT_CONFIG_VALUE_0", "true")
        yield decoy


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )


def _init(repo: Path) -> None:
    initialised = _git(repo, "init", "-q")
    assert initialised.returncode == 0, initialised.stderr


def test_git_in_a_temporary_repository_stays_there(
    hook_environment: Path, tmp_path: Path
) -> None:
    _init(tmp_path)
    git_dir = _git(tmp_path, "rev-parse", "--absolute-git-dir").stdout.strip()
    assert Path(git_dir).resolve() == (tmp_path / ".git").resolve()
    (tmp_path / "f").write_text("x\n", encoding="utf-8")
    added = _git(tmp_path, "add", "f")
    assert added.returncode == 0, added.stderr
    assert _git(tmp_path, "ls-files").stdout == "f\n"
    assert not hook_environment.exists()


def test_git_reads_no_config_from_the_hook_or_the_contributor(
    hook_environment: Path, tmp_path: Path
) -> None:
    _init(tmp_path)
    # Exit 1 is git's "key not set"; any other failure fails the test too.
    for key in ("user.name", "user.email", "commit.gpgsign"):
        assert _git(tmp_path, "config", "--get", key).returncode == 1, key
    # git var prints the system config path, whether or not the file exists,
    # unless GIT_CONFIG_NOSYSTEM is set (git 2.42+). Older git does not know
    # the variable and prints nothing either way, so there this check is vacuous.
    assert _git(tmp_path, "var", "GIT_CONFIG_SYSTEM").stdout == ""
