"""Tests reach no private name through a string.

``scripts/private_test_access.py`` runs here on a small repository written for
each test, and once on this checkout.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "private_test_access.py"


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("private_test_access", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path, source: str, tree: str = "tests/src/unit") -> Path:
    for each in ("tests/src/unit", "tests/addon"):
        (tmp_path / each).mkdir(parents=True, exist_ok=True)
        (tmp_path / each / "test_other.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / tree / "test_sample.py").write_text(source, encoding="utf-8")
    return tmp_path


def _run(
    guard: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    status = guard.main(["--repo", str(tmp_path)])
    return status, capsys.readouterr().out


REFUSED = {
    "getattr": "getattr(mod, '_helper')\n",
    "hasattr": "hasattr(mod, '_helper')\n",
    "setattr": "setattr(mod, '_helper', 1)\n",
    "delattr": "delattr(mod, '_helper')\n",
    "builtins-alias": "from builtins import getattr as read\nread(mod, '_helper')\n",
    "getattr-static": "inspect.getattr_static(mod, '_helper')\n",
    "attrgetter": "operator.attrgetter('state._helper')\n",
    "dunder-getattribute": "object.__getattribute__(mod, '_helper')\n",
    "import-module": "importlib.import_module('ha_mcp.tools._deep')\n",
    "dunder-import": "__import__('ha_mcp._private')\n",
    "patch": "patch('ha_mcp.module._helper')\n",
    "patch-decorator": "@patch('ha_mcp.module._helper')\ndef test_it(m):\n    pass\n",
    "patch-target-keyword": "mock.patch(target='ha_mcp.module._helper')\n",
    "patch-object": "patch.object(mod, '_helper')\n",
    "patch-object-keyword": "patch.object(mod, attribute='_helper')\n",
    "patch-multiple-key": "patch.multiple('ha_mcp.module', _helper=1)\n",
    "patch-dict-target": "patch.dict('ha_mcp.module._TABLE', {})\n",
    "patch-dict-namespace-key": "patch.dict(mod.__dict__, {'_helper': 1})\n",
    "monkeypatch-setattr": "monkeypatch.setattr(mod, '_helper', 1)\n",
    "monkeypatch-setattr-keyword": "monkeypatch.setattr(mod, name='_helper', value=1)\n",
    "monkeypatch-dotted": "monkeypatch.setattr('ha_mcp.module._helper', 1)\n",
    "monkeypatch-delattr": "monkeypatch.delattr(mod, '_helper')\n",
    "monkeypatch-setitem-namespace": "monkeypatch.setitem(vars(mod), '_helper', 1)\n",
    "vars-subscript": "vars(mod)['_helper']\n",
    "dict-subscript": "mod.__dict__['_helper']\n",
    "namespace-get": "vars(mod).get('_helper')\n",
    "module-constant": "NAME = '_helper'\ndef test_it():\n    getattr(mod, NAME)\n",
    "local-constant": "def test_it():\n    name = '_helper'\n    getattr(mod, name)\n",
    "concatenation": "PREFIX = '_'\ngetattr(mod, PREFIX + 'helper')\n",
    "f-string": "NAME = 'helper'\ngetattr(mod, f'_{NAME}')\n",
    "f-string-runtime-prefix": "patch(f'{module_name}._helper')\n",
    "conditional": "getattr(mod, '_helper' if flag else 'public')\n",
    "tuple-unpacking": "def test_it():\n    name, count = ('_helper', 1)\n    getattr(mod, name)\n",
    "loop": "for name in ('public', '_helper'):\n    getattr(mod, name)\n",
    "loop-over-bound-tuple": (
        "NAMES = ('public', '_helper')\n"
        "for module, names in ((mod, NAMES),):\n"
        "    for name in names:\n"
        "        monkeypatch.setattr(module, name, None)\n"
    ),
    "parametrize": (
        "@pytest.mark.parametrize('name', ['public', pytest.param('_helper', id='x')])\n"
        "def test_it(name):\n    getattr(mod, name)\n"
    ),
    "parametrize-tuple-row": (
        "@pytest.mark.parametrize(('name', 'count'), [('_helper', 1)])\n"
        "def test_it(name, count):\n    getattr(mod, name)\n"
    ),
    "helper-parameter": (
        "def _surface(name, value):\n    return patch.object(Tools, name, value)\n"
        "def test_it():\n    _surface('_search', None)\n"
    ),
    "helper-keyword-through-helper": (
        "def inner(attribute):\n    getattr(mod, attribute)\n"
        "def outer(name):\n    inner(attribute=name)\n"
        "outer('_helper')\n"
    ),
    "parametrize-constant-rows": (
        "NAMES = ['public', '_helper']\n"
        "@pytest.mark.parametrize('name', NAMES)\n"
        "def test_it(name):\n    getattr(mod, name)\n"
    ),
    "same-named-helpers-in-two-scopes": (
        "def test_one():\n    def check(target):\n        getattr(mod, target)\n"
        "    check('_helper')\n"
        "def test_two():\n    def check(name):\n        pass\n    check('public')\n"
    ),
    "default-argument": "def test_it(value=getattr(mod, '_helper')):\n    pass\n",
    "suppression-comment": "getattr(mod, '_helper')  # noqa: SLF001\n",
}


@pytest.mark.parametrize("source", REFUSED.values(), ids=REFUSED.keys())
def test_private_name_through_a_string_is_refused(guard, tmp_path, capsys, source):
    status, out = _run(guard, _repo(tmp_path, source), capsys)

    assert status == 1
    assert out.startswith("tests/src/unit/test_sample.py:")
    assert "reached through a string" in out.splitlines()[0]


ALLOWED = {
    "public": "getattr(mod, 'public')\n",
    "dunder": "getattr(mod, '__name__')\n",
    "vendor-package": "patch('ha_mcp._vendor.fastmcp.server.thing')\n",
    "own-self": "def test_it(self):\n    getattr(self, '_helper')\n",
    "own-cls": "def test_it(cls):\n    patch.object(cls, '_helper')\n",
    "runtime-name": "def test_it(name):\n    getattr(mod, name)\n",
    "parameter-shadows-constant": "NAME = '_helper'\ndef test_it(NAME):\n    getattr(mod, NAME)\n",
    "import-shadows-constant": (
        "NAME = '_helper'\ndef test_it():\n    from consts import NAME\n    getattr(mod, NAME)\n"
    ),
    "replacement-value": "monkeypatch.setattr('ha_mcp.module.NAME', '_value')\n",
    "patch-dict-data-key": "patch.dict('os.environ', {'_FLAG': '1'})\n",
    "patch-multiple-option": "patch.multiple('ha_mcp.module', autospec=True, NAME=1)\n",
    "http-patch": "client.patch('/api/_private/thing')\n",
    "other-helpers-arguments": (
        "def lookup(name):\n    getattr(mod, name)\n"
        "def unrelated(name):\n    pass\n"
        "lookup('public')\nunrelated('_helper')\n"
    ),
    "class-body-not-seen-by-method": (
        "NAME = 'public'\nclass TestIt:\n    NAME = '_helper'\n"
        "    def test_it(self):\n        getattr(mod, NAME)\n"
    ),
    "namespace-of-self": "vars(self)['_helper']\n",
    "getattr-static-own": "inspect.getattr_static(self, '_helper')\n",
    "patch-multiple-own": "patch.multiple(self, _helper=1)\n",
    "patch-dict-own-namespace": "patch.dict(vars(self), {'_helper': 1})\n",
    "f-string-format-spec": "NAME = '_helper'\ngetattr(mod, f'{NAME!r}')\n",
}


@pytest.mark.parametrize("source", ALLOWED.values(), ids=ALLOWED.keys())
def test_public_targets_and_data_strings_are_allowed(guard, tmp_path, capsys, source):
    status, out = _run(guard, _repo(tmp_path, source), capsys)

    assert (status, out) == (
        0,
        "No private string targets in 3 unit and app test files.\n",
    )


def test_too_many_alternatives_fail_instead_of_passing_unchecked(
    guard, tmp_path, capsys
):
    """The last of 289 combinations is private; none may be dropped unchecked."""
    prefixes = ", ".join(repr(c) for c in "ABCDEFGHIJKLMNOP_")
    suffixes = ", ".join(repr(c) for c in "abcdefghijklmnopq")
    source = (
        f"for prefix in ({prefixes}):\n"
        f"    for suffix in ({suffixes}):\n"
        "        getattr(mod, prefix + suffix)\n"
    )

    status, out = _run(guard, _repo(tmp_path, source), capsys)

    assert status == 1
    assert out.startswith("tests/src/unit/test_sample.py:3: ")


def test_app_tests_are_checked_and_the_call_line_is_reported(guard, tmp_path, capsys):
    source = "x = 1\nmonkeypatch.setattr(\n    mod,\n    '_helper',\n    1,\n)\n"

    status, out = _run(guard, _repo(tmp_path, source, "tests/addon"), capsys)

    assert status == 1
    assert out.splitlines()[0] == (
        "tests/addon/test_sample.py:2: private name '_helper' reached through a string"
    )


@pytest.mark.parametrize("tree", ["tests/src/unit", "tests/addon"])
def test_a_missing_tree_fails_instead_of_passing_empty(guard, tmp_path, capsys, tree):
    _repo(tmp_path, "x = 1\n")
    for path in (tmp_path / tree).iterdir():
        path.unlink()

    status, out = _run(guard, tmp_path, capsys)

    assert status == 1
    assert f"{tree}: no Python files to check" in out


def test_unparseable_source_fails_with_its_line(guard, tmp_path, capsys):
    status, out = _run(guard, _repo(tmp_path, "x = 1\ndef broken(:\n"), capsys)

    assert status == 1
    assert out.startswith("tests/src/unit/test_sample.py:2: ")


def test_no_test_reaches_a_private_name_through_a_string(guard, capsys):
    assert guard.main([]) == 0, capsys.readouterr().out
