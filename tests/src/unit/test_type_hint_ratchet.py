"""Type-hint ratchet: no new Python function without full type hints.

``type_hint_baseline.json`` lists every tracked Python file in scope that has
functions missing a parameter or return hint, with their number. A listed file
may not gain one, and no other file may have one.
``python scripts/type_hint_ratchet.py`` lowers or drops entries after a merge,
never raises or adds one.

The repository pin at the bottom passes on arrival and fails when an
unannotated function is added. The tests above it drive the rules with
made-up sources.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ._ratchet_repo import commit, load_script, make_ratchet_repo

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINE_PATH = Path(__file__).with_name("type_hint_baseline.json")

ratchet = load_script("type_hint_ratchet")

UNANNOTATED = b"def load(path):\n    return path\n"
ANNOTATED = b"def load(path: str) -> str:\n    return path\n"


def _count(source: str) -> int:
    return ratchet.count_unannotated(source.encode())


@pytest.mark.parametrize(
    "source",
    [
        "def f(x) -> int: ...",
        "def f(x: int): ...",
        "def f(x: int, y) -> None: ...",
        "def f(x, y: int) -> None: ...",
        "def f(*args) -> None: ...",
        "def f(**kwargs) -> None: ...",
        "def f(*, key) -> None: ...",
        "def f(x, /) -> None: ...",
        "async def f(x: int): ...",
        "def f() -> None:\n    def inner(x): ...",
        "class A:\n    def f(self) -> None:\n        def inner(x) -> None: ...",
        "class A:\n    @staticmethod\n    def f(x) -> None: ...",
        "class A:\n    @builtins.staticmethod\n    def f(x) -> None: ...",
        "class A:\n    def __init__(self): ...",
        "def __init__(x: int): ...",
    ],
)
def test_a_missing_hint_is_counted(source: str) -> None:
    """Each shape lacks a hint mypy's disallow_untyped_defs asks for."""
    assert _count(source) == 1


@pytest.mark.parametrize(
    "source",
    [
        "def f(x: int, *a: int, k: int = 1, **kw: int) -> None: ...",
        "class A:\n    def f(self, x: int) -> None: ...",
        "class A:\n    @classmethod\n    def f(cls) -> None: ...",
        "class A:\n    @functools.cache\n    def f(self) -> int: ...",
        "class A:\n    def __init__(self, x: int): ...",
        "class A:\n    def __init__(self, *, x: int): ...",
        "class A:\n    def __init__(self: A): ...",
        "class A:\n    def __init_subclass__(cls, **kw: int): ...",
        "class A:\n    if True:\n        def f(self) -> None: ...",
        "f = lambda x: x",
    ],
)
def test_a_signature_mypy_accepts_is_not_counted(source: str) -> None:
    """Counting these would list files whose hints are complete."""
    assert _count(source) == 0


def test_every_unannotated_function_in_a_file_is_counted() -> None:
    source = "def a(x): ...\nclass B:\n    def c(self): ...\n    def d(self, y): ..."

    assert _count(source) == 3


def test_new_file_with_an_unannotated_function_is_rejected() -> None:
    """Without this a new test module lands with no hints at all."""
    violations = ratchet.find_violations({"tests/new.py": 1, "tests/ok.py": 0}, {})

    assert len(violations) == 1
    assert "tests/new.py" in violations[0]


def test_listed_file_that_gained_one_is_rejected() -> None:
    violations = ratchet.find_violations({"tests/old.py": 6}, {"tests/old.py": 5})

    assert len(violations) == 1
    assert "from 5 to 6" in violations[0]


def test_listed_file_that_improved_passes() -> None:
    """Annotating old functions must not force an edit of the shared baseline:
    two such edits conflict. The post-merge run lowers the entry."""
    assert ratchet.find_violations({"tests/old.py": 2}, {"tests/old.py": 5}) == []


def test_baseline_raised_or_added_over_the_base_is_rejected() -> None:
    """The baseline is a plain file. Without this an agent blocked by the
    ratchet could raise its own entry and every other check would pass."""
    growth = ratchet.find_growth({"tests/a.py": 6, "tests/b.py": 1}, {"tests/a.py": 5})

    assert len(growth) == 2
    assert ratchet.find_growth({"tests/a.py": 4}, {"tests/a.py": 5, "x.py": 1}) == []


@pytest.fixture
def temp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty git repository with no exclusions and an empty baseline."""
    return make_ratchet_repo(tmp_path, monkeypatch, ratchet.BASELINE_NAME)


def _write(repo: Path, path: str, content: bytes, stage: bool = True) -> None:
    (repo / path).write_bytes(content)
    if stage:
        subprocess.run(["git", "add", path], cwd=repo, check=True)


def _write_baseline(repo: Path, entries: dict[str, int], stage: bool = True) -> None:
    _write(repo, ratchet.BASELINE_NAME, json.dumps(entries).encode(), stage)


def test_hook_judges_the_staged_content_not_the_working_tree(
    temp_repo: Path,
) -> None:
    """An unstaged fix must not hide the unannotated function the commit holds,
    and an unstaged baseline edit must not allow it."""
    _write(temp_repo, "test_new.py", UNANNOTATED)
    _write(temp_repo, "test_new.py", ANNOTATED, stage=False)
    _write_baseline(temp_repo, {"test_new.py": 1}, stage=False)

    assert ratchet.main(["--staged", "--check"], repo_root=temp_repo) == 1
    assert ratchet.main(["--check"], repo_root=temp_repo) == 0


def test_hook_never_writes_the_baseline(temp_repo: Path) -> None:
    """Every pull request touching a listed file would otherwise edit the one
    shared file, and those edits conflict."""
    _write(temp_repo, "test_old.py", UNANNOTATED)
    _write_baseline(temp_repo, {"test_old.py": 3})

    assert ratchet.main(["--staged", "--check"], repo_root=temp_repo) == 0
    assert json.loads((temp_repo / ratchet.BASELINE_NAME).read_bytes()) == {
        "test_old.py": 3
    }


def test_post_merge_run_lowers_the_baseline(temp_repo: Path) -> None:
    """The sync lowers improved entries and drops clean files. It writes
    sorted, indented JSON so each sync commit changes only those lines, and
    it skips source files that are not Python."""
    _write(temp_repo, "test_old.py", UNANNOTATED)
    _write(temp_repo, "test_a.py", UNANNOTATED)
    _write(temp_repo, "test_fixed.py", ANNOTATED)
    _write(temp_repo, "app.js", b"const f = (x) => x;\n")
    _write_baseline(temp_repo, {"test_old.py": 3, "test_a.py": 1, "test_fixed.py": 2})

    assert ratchet.main([], repo_root=temp_repo) == 0
    assert (temp_repo / ratchet.BASELINE_NAME).read_text(encoding="utf-8") == (
        '{\n  "test_a.py": 1,\n  "test_old.py": 1\n}\n'
    )


def test_base_check_rejects_a_hand_raised_entry(temp_repo: Path) -> None:
    _write(temp_repo, "test_old.py", UNANNOTATED)
    _write_baseline(temp_repo, {"test_old.py": 1})
    commit(temp_repo)
    _write_baseline(temp_repo, {"test_old.py": 2}, stage=False)

    assert ratchet.main(["--base", "HEAD"], repo_root=temp_repo) == 1


def test_base_check_rejects_a_function_added_under_a_stale_entry(
    temp_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Until the sync runs, the base's entry can sit above its file's count.
    Adding an unannotated function back under it must fail."""
    _write(temp_repo, "test_old.py", UNANNOTATED)
    _write_baseline(temp_repo, {"test_old.py": 3})
    commit(temp_repo)
    _write(temp_repo, "test_old.py", UNANNOTATED * 2, stage=False)

    assert ratchet.main(["--base", "HEAD"], repo_root=temp_repo) == 1
    assert "test_old.py: functions without full type hints went from 1 to 2" in (
        capsys.readouterr().err
    )


def test_base_check_passes_an_annotated_change(temp_repo: Path) -> None:
    """The base check reads the base commit's own files. If that read came
    back empty, every listed file would look new and fail every pull request.
    An unchanged clean file must pass too."""
    _write(temp_repo, "test_old.py", UNANNOTATED * 2)
    _write(temp_repo, "test_clean.py", ANNOTATED)
    _write_baseline(temp_repo, {"test_old.py": 2})
    commit(temp_repo)
    _write(temp_repo, "test_old.py", UNANNOTATED + ANNOTATED, stage=False)

    assert ratchet.main(["--base", "HEAD"], repo_root=temp_repo) == 0


def test_repository_matches_the_baseline() -> None:
    """Fails when a file gains a function without full type hints."""
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    violations = ratchet.find_violations(ratchet.measure(REPO_ROOT), baseline)

    assert not violations, "\n" + "\n".join(violations)
