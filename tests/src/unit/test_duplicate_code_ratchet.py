"""Duplicate-code ratchet: no new copies of a Python function or class.

``duplicate_code_baseline.json`` lists the groups of copies that exist, keyed
by a hash of their normalized code. ``python scripts/duplicate_code_ratchet.py``
rewrites it to the current groups, but only when no group has a new copy.

The repository pin at the bottom passes on arrival and fails when a copy is
added. The tests above it drive the rules with made-up sources.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from ._ratchet_repo import make_ratchet_repo

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "duplicate_code_ratchet.py"
BASELINE_PATH = Path(__file__).with_name("duplicate_code_baseline.json")


def _load_ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("duplicate_code_ratchet", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ratchet = _load_ratchet()

HELPER = b'''
def load(path: str) -> dict:
    """Read a settings file."""
    text = open(path).read()
    return parse(text)
'''

# HELPER with other names, docstring, decorator and type hints.
RENAMED_HELPER = b"""
@cache
def read_config(file):
    content = open(file).read()
    return parse(content)
"""


def _pasted(code: bytes) -> bytes:
    """Return ``code`` in a module that also holds other code.

    A byte-identical file is read once, so a pasted copy needs a module
    that differs from the original.
    """
    return code + b"\nLIMIT = 10\n"


def test_copy_with_other_names_and_hints_is_found() -> None:
    """Renaming the variables is the usual way a pasted helper differs."""
    groups = ratchet.find_copies({"a.py": HELPER, "b.py": RENAMED_HELPER})

    assert list(groups.values()) == [["a.py::load", "b.py::read_config"]]


def test_different_calls_are_not_copies() -> None:
    """Two helpers that call different functions do different work."""
    other = HELPER.replace(b"parse(text)", b"validate(text)")

    assert ratchet.find_copies({"a.py": HELPER, "b.py": other}) == {}


def test_classes_with_different_fields_are_not_copies() -> None:
    """Field names are a class's interface, so two schemas with the same
    field types are different classes."""
    first = b"class A:\n    name: str\n    size: int\n"
    second = b"class B:\n    path: str\n    count: int\n"

    assert ratchet.find_copies({"a.py": first, "b.py": second}) == {}


def test_class_fields_stay_apart_from_method_locals_of_the_same_name() -> None:
    """A method's local variable must not make the class field of the same
    name look renamed, or two classes with different fields would match."""
    first = b"class A:\n    size = 1\n\n    def f(self):\n        size = 2\n        return size\n"
    second = first.replace(b"A", b"B").replace(b"size", b"count")

    groups = ratchet.find_copies({"a.py": first, "b.py": second})

    assert list(groups.values()) == [["a.py::A.f", "b.py::B.f"]]


def test_renamed_import_alias_and_nested_helper_are_still_copies() -> None:
    """Both are names bound inside the function, like its variables."""
    original = b"""
def load(path):
    import json as codec
    def clean(text):
        return text.strip()
    return codec.loads(clean(open(path).read()))
"""
    renamed = original.replace(b"codec", b"decoder").replace(b"clean", b"tidy")

    groups = ratchet.find_copies({"a.py": original, "b.py": renamed})

    assert list(groups.values()) == [["a.py::load", "b.py::load"]]


def test_imports_of_different_modules_are_not_copies() -> None:
    """The imported module is what the code depends on, not a local name."""
    first = b"def load(text):\n    import json\n    return json.loads(text)\n"
    second = first.replace(b"json", b"yaml")

    assert ratchet.find_copies({"a.py": first, "b.py": second}) == {}


def test_a_nested_name_does_not_rename_the_outer_global() -> None:
    """A nested function's own ``json`` must not make the outer function's
    global ``json`` look local, or helpers calling different modules match."""
    first = b"""
def load(path):
    def clean(json):
        return json.strip()
    return json.loads(clean(open(path).read()))
"""
    second = first.replace(b"json", b"yaml")

    assert ratchet.find_copies({"a.py": first, "b.py": second}) == {}


def test_renamed_match_capture_is_still_a_copy() -> None:
    """A ``case`` capture binds a name like an assignment does."""
    original = b"""
def kind(event):
    match event:
        case {"type": item, **rest}:
            return item, rest
    return None
"""
    renamed = original.replace(b"item", b"payload").replace(b"rest", b"extra")

    groups = ratchet.find_copies({"a.py": original, "b.py": renamed})

    assert list(groups.values()) == [["a.py::kind", "b.py::kind"]]


def test_field_types_of_a_class_inside_a_function_are_kept() -> None:
    """The types of a local class's fields are its schema, not hints on
    function variables."""
    first = b"def build():\n    class Row:\n        name: str\n        size: int\n    return Row\n"
    second = first.replace(b"str", b"bytes").replace(b"int", b"float")

    assert ratchet.find_copies({"a.py": first, "b.py": second}) == {}


def test_one_statement_bodies_are_not_compared() -> None:
    """Stubs and one-line delegations would fill the baseline with noise."""
    stub = b"def f(x):\n    return g(x)\n"

    assert ratchet.find_copies({"a.py": stub, "b.py": _pasted(stub)}) == {}


def test_closures_are_not_compared_on_their_own() -> None:
    """A closure cannot be imported elsewhere, so a matching pair of test
    fakes inside two test functions is not a copy to fix."""
    test = b"""
def test_{n}():
    calls = []
    def fake(x):
        calls.append(x)
        return x
    run(fake)
    assert calls == [{n}]
"""

    sources = {
        "a.py": test.replace(b"{n}", b"1"),
        "b.py": test.replace(b"{n}", b"2"),
    }

    assert ratchet.find_copies(sources) == {}


def test_identical_files_are_read_once() -> None:
    """The server and the component share code as two identical files kept
    in sync by a parity test. A new function in that file must not fail."""
    groups = ratchet.find_copies(
        {"server/patch.py": HELPER, "component/patch.py": HELPER}
    )

    assert groups == {}


def test_new_copy_is_rejected() -> None:
    groups = ratchet.find_copies({"a.py": HELPER, "b.py": RENAMED_HELPER})

    violations = ratchet.find_violations(groups, {})

    assert len(violations) == 1
    assert "a.py::load" in violations[0] and "b.py::read_config" in violations[0]


def test_another_copy_of_a_listed_group_is_rejected() -> None:
    """A listed group must not become the template for more copies."""
    sources = {"a.py": HELPER, "b.py": RENAMED_HELPER}
    baseline = ratchet.find_copies(sources)

    groups = ratchet.find_copies({**sources, "c.py": _pasted(HELPER)})

    violations = ratchet.find_violations(groups, baseline)
    assert len(violations) == 1
    assert violations[0].startswith("c.py::load: a new copy of a.py::load")


def test_moved_or_renamed_copy_passes() -> None:
    """Splitting a module moves functions; that is not a new copy."""
    baseline = ratchet.find_copies({"a.py": HELPER, "b.py": RENAMED_HELPER})

    groups = ratchet.find_copies({"a.py": HELPER, "pkg/b.py": RENAMED_HELPER})

    assert ratchet.find_violations(groups, baseline) == []


def test_copies_edited_the_same_way_pass() -> None:
    """A rename across the codebase changes every copy's code at once."""
    sources = {"a.py": HELPER, "b.py": RENAMED_HELPER}
    baseline = ratchet.find_copies(sources)
    edited = {
        path: code.replace(b"parse(", b"parse_text(") for path, code in sources.items()
    }

    groups = ratchet.find_copies(edited)

    assert groups.keys() != baseline.keys()
    assert ratchet.find_violations(groups, baseline) == []


def test_copied_class_is_reported_once() -> None:
    """A copied class also copies its methods; one message per method would
    bury the one fix: import the class."""
    cls = b"""
class Store:
    def get(self, key):
        value = self.data[key]
        return value

    def put(self, key, value):
        self.data[key] = value
        self.dirty = True
"""
    groups = ratchet.find_copies({"a.py": cls, "b.py": _pasted(cls)})

    violations = ratchet.find_violations(groups, {})

    assert len(groups) == 3
    assert len(violations) == 1
    assert "a.py::Store" in violations[0]


@pytest.fixture
def temp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty git repository with no exclusions and an empty baseline."""
    return make_ratchet_repo(tmp_path, monkeypatch, ratchet.BASELINE_NAME)


def _stage(repo: Path, path: str, content: bytes) -> None:
    (repo / path).write_bytes(content)
    subprocess.run(["git", "add", path], cwd=repo, check=True)


def _list(repo: Path, groups: dict[str, list[str]]) -> None:
    """Write and stage a baseline that lists ``groups``."""
    _stage(repo, ratchet.BASELINE_NAME, json.dumps(groups).encode())


def _baseline(repo: Path) -> dict[str, list[str]]:
    return json.loads((repo / ratchet.BASELINE_NAME).read_text(encoding="utf-8"))


def test_hook_rejects_a_staged_copy(temp_repo: Path) -> None:
    """The hook command is what the agent sees at commit time, so it must
    fail itself instead of leaving the failure to CI."""
    _stage(temp_repo, "a.py", HELPER)
    _stage(temp_repo, "b.py", RENAMED_HELPER)

    assert ratchet.main(["--staged"], repo_root=temp_repo) == 1


def test_failed_run_keeps_the_entry_of_a_group_that_gained_a_copy(
    temp_repo: Path,
) -> None:
    """If a failing run rewrote the baseline, the grown group's entry would
    be dropped, and removing the new copy would then fail as well."""
    _stage(temp_repo, "a.py", HELPER)
    _stage(temp_repo, "b.py", RENAMED_HELPER)
    listed = ratchet.find_copies({"a.py": HELPER, "b.py": RENAMED_HELPER})
    _list(temp_repo, listed)
    _stage(temp_repo, "c.py", _pasted(HELPER))

    assert ratchet.main(["--staged"], repo_root=temp_repo) == 1
    assert _baseline(temp_repo) == listed


def test_removed_copy_drops_its_group(temp_repo: Path) -> None:
    """A group left in the baseline after its copies are gone would let the
    copy come back unnoticed."""
    _stage(temp_repo, "a.py", HELPER)
    listed = ratchet.find_copies({"a.py": HELPER, "b.py": RENAMED_HELPER})
    _list(temp_repo, listed)

    assert ratchet.main(["--staged"], repo_root=temp_repo) == 0
    assert _baseline(temp_repo) == {}


def test_staged_run_ignores_an_unstaged_removal(temp_repo: Path) -> None:
    """The hook stages the baseline it writes. If it read the working tree,
    a removal left out of the commit would drop the group, and the committed
    files would no longer match the baseline in CI."""
    _stage(temp_repo, "a.py", HELPER)
    _stage(temp_repo, "b.py", RENAMED_HELPER)
    listed = ratchet.find_copies({"a.py": HELPER, "b.py": RENAMED_HELPER})
    _list(temp_repo, listed)
    (temp_repo / "b.py").write_bytes(b"")

    assert ratchet.main(["--staged"], repo_root=temp_repo) == 0
    assert _baseline(temp_repo) == listed


def test_staged_run_ignores_an_unstaged_baseline_edit(temp_repo: Path) -> None:
    """A copy staged for the commit must not pass because an unstaged edit
    to the baseline lists it: the hook would then stage that edit."""
    _stage(temp_repo, "a.py", HELPER)
    _stage(temp_repo, "b.py", RENAMED_HELPER)
    listed = ratchet.find_copies({"a.py": HELPER, "b.py": RENAMED_HELPER})
    (temp_repo / ratchet.BASELINE_NAME).write_text(json.dumps(listed), encoding="utf-8")

    assert ratchet.main(["--staged"], repo_root=temp_repo) == 1


def test_repository_matches_the_baseline() -> None:
    """Fails when a function or class is copied, or when the baseline lists
    copies that are gone."""
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    groups = ratchet.scan(REPO_ROOT)

    violations = ratchet.find_violations(groups, baseline)

    assert not violations, "\n" + "\n".join(violations)
    assert groups == baseline, (
        f"The baseline is out of date. Run `{ratchet.REPIN_COMMAND}` "
        f"and commit {ratchet.BASELINE_NAME}."
    )
