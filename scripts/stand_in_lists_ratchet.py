#!/usr/bin/env python3
"""Fail when a pull request exempts a test from the real-Core checks.

The unit and app (add-on) tests run on real Home Assistant Core
(tests/AGENTS.md, "Unit Tests Run on Real Home Assistant Core"). Files written
before each check are listed with it: the ``ignores`` of the ast-grep rules
that scan the tests and the ``SLF001`` entries of the two test ``ruff.toml``
files. mypy has no list: a ``pyproject.toml`` override that names a ``tests.``
module would be one, and any added is refused, as is every other way of
checking the tests less than at the base (below).
``tests/core_stand_in_guard.py`` has no list now: a guard without
``_GRANDFATHERED`` counts as one with an empty list, and its hook must not see
the test item. The lists are plain files, so a pull request could add its own
new test to one and every check would pass. With ``--base`` this compares them
with the base commit.

Two rules decide every added entry:

- A file that does not exist at the base is new, and a new file is never
  listed, whatever else the pull request changes. A renamed file is new.
- A file that does exist at the base may be listed only when the check itself
  is new or wider than at the base: the check did not exist, or its ast-grep
  rule's definition changed. A wider rule matches older files it did not
  match before; nothing else justifies a longer list.

The allowlist of ``scripts/integration_stand_ins.py`` lists a file with each
real integration it keeps a stand-in for, and gets the same two rules for every
added file and integration. That check counts as wider when the code of one
of its scripts (``INTEGRATION_CHECK``) changed: an edit cannot be told to
widen or narrow it, and the check itself still fails an entry that matches no
stand-in it finds. The list is read with the check's own reader, so an entry
the check refuses fails here too. A file taken off it is converted, as below.
Its entries are kept stand-ins with a reason, not files waiting for
conversion: an edit to a listed file does not take it off, and a file
converted off the other lists may stay on it.

A listed file is exempt as a whole, so a pull request that edits one could
add a new faked test to it. A listed file the pull request changes is
therefore converted, which takes it off every list.

A file the pull request takes off any list is converted, and a converted file
is on none of them, the integration allowlist aside: one taken off an ast-grep
rule's list but left on ``SLF001`` still reaches into private members the
conversion was meant to remove.

The script also refuses the ways around a list: narrowing a rule's ``files``,
removing or renaming a rule (except one named in ``RETIRED_RULES`` whose list
is empty), turning ``SLF001`` off in a test ``ruff.toml``, and for mypy: a
setting that checks the tests less, a test path excluded, Core's or our own
imports skipped, a ``mypy.ini`` that would replace ``pyproject.toml``'s
settings, and a Fast Checks step that no longer fails on ``MYPY_ON_TESTS``. The
in-file switches (``# mypy:`` comments, ``no_type_check``) are the ast-grep
rule ``no-stand-in-suppressions``.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import yaml  # type: ignore[import-untyped]


def _allowlist_reader() -> ModuleType:
    """``scripts/stand_in_allowlist.py`` beside this script: the reader
    ``scripts/integration_stand_ins.py`` reads its allowlist with."""
    path = Path(__file__).resolve().with_name("stand_in_allowlist.py")
    spec = importlib.util.spec_from_file_location("stand_in_allowlist", path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_READER = _allowlist_reader()

REPO = Path(__file__).resolve().parent.parent
GUARD = "tests/core_stand_in_guard.py"
RUFF_FILES = ("tests/src/unit/ruff.toml", "tests/addon/ruff.toml")
PYPROJECT = "pyproject.toml"
WORKFLOW = ".github/workflows/pr.yml"
# The Fast Checks command that runs mypy on the tests; it stays in the workflow.
MYPY_ON_TESTS = "uv run mypy tests/src/unit/ tests/addon/"
FAST_CHECKS = "fast-checks"
# A step condition that runs the step after an earlier one failed too.
ALWAYS = "success() || failure()"
# mypy reads either of these instead of pyproject.toml.
MYPY_CONFIG_FILES = ("mypy.ini", ".mypy.ini")
# The value of each setting that checks the most. The tests' value of one of
# these may only change to it; any other setting may not change at all, except
# the lists below and ``MYPY_NEUTRAL`` (``exclude`` and ``overrides`` are
# checked on their own).
MYPY_STRICT: dict[str, object] = {
    "ignore_errors": False,
    "ignore_missing_imports": False,
    "follow_imports": "normal",
    "strict_optional": True,
    "implicit_optional": False,
    "check_untyped_defs": True,
    "allow_untyped_globals": False,
    "allow_redefinition": False,
    "disallow_untyped_defs": True,
    "disallow_incomplete_defs": True,
    "disallow_untyped_calls": True,
    "disallow_untyped_decorators": True,
    "disallow_any_generics": True,
    "warn_return_any": True,
    "warn_unused_ignores": True,
    "warn_redundant_casts": True,
    "warn_unreachable": True,
    "strict_equality": True,
    "strict": True,
}
MYPY_NEUTRAL = frozenset(
    {"python_version", "mypy_path", "cache_dir", "warn_unused_configs"}
    | {"explicit_package_bases", "exclude", "overrides"}
)
# Lists whose gained entry checks less, and lists whose lost entry does.
MYPY_GAINED_LOOSENS = ("disable_error_code", "always_true", "always_false")
MYPY_LOST_LOOSENS = ("enable_error_code", "plugins")
# The packages whose types a test is checked against: skipping their imports
# turns those types into Any.
TYPED_FOR_TESTS = frozenset(
    {"homeassistant", "custom_components", "ha_mcp", "pytest_homeassistant"}
)
# The parts of an ast-grep rule that decide what it matches.
RULE_DEFINITION = ("rule", "constraints", "utils", "language")
# The list of test modules mypy skips, as the conversion check names it.
MYPY_LIST = f"{PYPROJECT} (mypy)"
# A rule deleted with what it refused: it named three shared fake modules, and
# no file is left to import from any of them. Matters only while a pull
# request's base still has the rule, and only with its list empty.
RETIRED_RULES = frozenset({"no-shared-core-fakes"})
INTEGRATION_CHECK = (
    "scripts/integration_stand_ins.py",
    "scripts/stand_in_syntax.py",
    "scripts/stand_in_scopes.py",
    "scripts/stand_in_resolver.py",
    "scripts/stand_in_allowlist.py",
)
INTEGRATION_ALLOWLIST: str = _READER.ALLOWLIST


class Base:
    """The base commit of a pull request, read through git."""

    def __init__(self, repo: Path, commit: str) -> None:
        self.repo = repo
        self.commit = commit

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        # Git exports GIT_DIR and friends to hooks (lefthook), which would
        # point it at another repository than ``repo``.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def is_commit(self) -> bool:
        return self._git("cat-file", "-e", f"{self.commit}^{{commit}}").returncode == 0

    def names(self, directory: str) -> list[str]:
        """The files directly under ``directory`` at the base."""
        listed = self._git("ls-tree", "--name-only", self.commit, f"{directory}/")
        if listed.returncode != 0:
            raise RuntimeError(f"cannot list {directory} at the base: {listed.stderr}")
        return listed.stdout.split()

    def has(self, path: str) -> bool:
        """Whether ``path`` is a file at the base; a directory is not one."""
        listed = self._git("ls-tree", self.commit, "--", path)
        if listed.returncode != 0:
            raise RuntimeError(f"cannot look up {path} at the base: {listed.stderr}")
        # "<mode> <type> <object>\t<path>"
        return any(line.split()[1] == "blob" for line in listed.stdout.splitlines())

    def changed(self) -> set[str]:
        """The tracked files whose text in the checkout differs from the base."""
        diff = self._git("diff", "--name-only", self.commit, "--")
        if diff.returncode != 0:
            raise RuntimeError(f"cannot compare with the base: {diff.stderr}")
        return set(diff.stdout.split("\n")) - {""}

    def text(self, path: str) -> str | None:
        """``path`` at the base, or None when it did not exist there."""
        if not self.has(path):
            return None
        shown = self._git("show", f"{self.commit}:{path}")
        if shown.returncode != 0:
            raise RuntimeError(f"cannot read {path} at the base: {shown.stderr}")
        return shown.stdout


def _test_rules(texts: dict[str, str]) -> dict[str, dict[str, Any]]:
    """The ast-grep rules that scan the tests, by rule id."""
    rules: dict[str, dict[str, Any]] = {}
    for path, text in texts.items():
        rule = yaml.safe_load(text)
        if not isinstance(rule, dict) or "id" not in rule:
            raise ValueError(f"{path} is not an ast-grep rule")
        if any(str(glob).startswith("tests/") for glob in rule.get("files") or []):
            rules[str(rule["id"])] = rule
    return rules


def _definition(rule: dict[str, Any]) -> dict[str, Any]:
    return {key: rule.get(key) for key in RULE_DEFINITION}


def _guard_entries(text: str) -> set[str]:
    tree = ast.parse(text)
    stores = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "_GRANDFATHERED"
        and isinstance(node.ctx, ast.Store)
    ]
    if not stores:
        # No list exempts no file, as long as the hook cannot pick out a test.
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "pytest_runtest_call"
            and (node.args.args or node.args.posonlyargs or node.args.kwonlyargs)
            for node in ast.walk(tree)
        ):
            raise ValueError(
                f"{GUARD}: pytest_runtest_call takes the test item but the guard "
                "has no _GRANDFATHERED list"
            )
        return set()
    values = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Assign) and node.targets == [stores[0]])
        or (isinstance(node, ast.AnnAssign) and node.target is stores[0])
    ]
    if len(stores) != 1 or len(values) != 1:
        raise ValueError(
            f"{GUARD} must assign _GRANDFATHERED once, by plain assignment"
        )
    value = values[0].value
    if value is None:
        raise ValueError(f"{GUARD} declares _GRANDFATHERED without a value")
    # A name or an expression in the list would hide the file it stands for.
    for node in ast.walk(value):
        builds_set = isinstance(node, ast.Name) and node.id in ("frozenset", "set")
        if not builds_set and not isinstance(
            node, (ast.Call, ast.Set, ast.List, ast.Tuple, ast.Constant, ast.Load)
        ):
            raise ValueError(f"{GUARD} must list its files as string literals")
    return {
        constant.value
        for constant in ast.walk(value)
        if isinstance(constant, ast.Constant) and isinstance(constant.value, str)
    }


def _ruff_entries(path: str, text: str, problems: list[str]) -> set[str]:
    lint = tomllib.loads(text).get("lint", {})
    if "SLF001" not in lint.get("extend-select", []):
        problems.append(f"{path}: SLF001 is no longer selected")
    problems.extend(
        f"{path}: {key} turns SLF001 off for every test"
        for key in ("ignore", "extend-ignore")
        if any(str(code).startswith("SLF") for code in lint.get(key, []))
    )
    folder = path.rsplit("/", 1)[0]
    entries = set()
    for table in ("per-file-ignores", "extend-per-file-ignores"):
        for pattern, codes in lint.get(table, {}).items():
            if any(str(code).startswith("SLF") for code in codes):
                entries.add(f"{folder}/{pattern}")
    return entries


def _modules(override: dict[str, Any]) -> list[str]:
    module = override.get("module", [])
    return [module] if isinstance(module, str) else list(module)


def _module_path(base: Base, module: str) -> str:
    stem = module.replace(".", "/")
    return f"{stem}/__init__.py" if base.has(f"{stem}/__init__.py") else f"{stem}.py"


class _MypyState(NamedTuple):
    exempt: set[str]
    # The settings mypy applies to each test module: the global ones, then each
    # override whose pattern reaches that module, in order.
    settings: dict[str, dict[str, Any]]
    excluded: set[str]
    # (module pattern, follow_imports) for the packages a test is typed against.
    untyped: set[tuple[str, str]]


def _test_paths(repo: Path) -> list[str]:
    """The test files mypy checks, and their directories (``dir/``)."""
    files = sorted(
        path.relative_to(repo).as_posix()
        for tree in ("tests/src/unit", "tests/addon")
        for path in (repo / tree).rglob("*.py")
    )
    dirs = {f"{name.rsplit('/', 1)[0]}/" for name in files}
    return [*files, *sorted(dirs)]


def _covers(pattern: str, modules: Sequence[str]) -> bool:
    """Whether a mypy override's module pattern applies to any of ``modules``."""
    return any(
        fnmatch.fnmatchcase(module, pattern)
        or (pattern.endswith(".*") and module == pattern[:-2])
        for module in modules
    )


def _excludes(pattern: str, paths: Sequence[str]) -> bool:
    """Whether a mypy ``exclude`` regex skips any of ``paths``."""
    try:
        return any(re.search(pattern, path) for path in paths)
    except re.error:
        return True


def _mypy_state(text: str, test_paths: Sequence[str] = ()) -> _MypyState:
    mypy = tomllib.loads(text).get("tool", {}).get("mypy", {})
    test_modules = [
        path.removesuffix(".py").replace("/", ".")
        for path in test_paths
        if path.endswith(".py")
    ] or ["tests"]
    exempt: set[str] = set()
    untyped: set[tuple[str, str]] = set()
    general = {k: v for k, v in mypy.items() if k not in MYPY_NEUTRAL}
    settings = {module: dict(general) for module in test_modules}
    for override in mypy.get("overrides", []):
        modules = _modules(override)
        follow = override.get("follow_imports", "normal")
        untyped.update(
            (m, follow)
            for m in modules
            if follow != "normal" and m.split(".")[0] in TYPED_FOR_TESTS
        )
        named = [m for m in modules if m == "tests" or m.startswith("tests.")]
        if modules != ["tests.*"] and named:
            exempt.update(named)
            continue
        # An override applies to the test modules its pattern reaches: the
        # ``tests.*`` one to all of them, ``*.unit.*`` to the unit tests only.
        for module, applied in settings.items():
            if not any(_covers(pattern, [module]) for pattern in modules):
                continue
            for key, value in override.items():
                if key in MYPY_GAINED_LOOSENS:
                    # A code disabled for every module stays disabled for it.
                    applied[key] = sorted({*applied.get(key, []), *value})
                elif key != "module":
                    applied[key] = value
    exclude = mypy.get("exclude", [])
    excluded = {
        pattern
        for pattern in ([exclude] if isinstance(exclude, str) else exclude)
        if _excludes(pattern, test_paths)
    }
    return _MypyState(exempt, settings, excluded, untyped)


def _mypy_step_problem(workflow: Path) -> str | None:
    """Why Fast Checks does not fail on ``MYPY_ON_TESTS``'s errors, if it does not.

    The step must run the command as it is, fail the job on its exit status,
    and run whenever the job does.
    """
    try:
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        return f"cannot be read: {err}"
    jobs = data.get("jobs") if isinstance(data, dict) else None
    job = jobs.get(FAST_CHECKS) if isinstance(jobs, dict) else None
    # A condition or ``continue-on-error`` of any value (``false`` included) is
    # one an edit can turn on.
    if not isinstance(job, dict) or {"if", "continue-on-error"} & job.keys():
        return f"the {FAST_CHECKS} job is gone or does not always run"
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        lines = [line.strip() for line in str(step.get("run", "")).splitlines()]
        if f"{MYPY_ON_TESTS} || status=1" not in lines:
            continue
        # Without ``if`` a step runs only after the earlier ones succeeded.
        if step.get("if") != ALWAYS or "continue-on-error" in step:
            return f"its `{MYPY_ON_TESTS}` step does not always run or fail"
        if [line for line in lines if line][-1] != 'exit "$status"':
            return f"its `{MYPY_ON_TESTS}` step does not exit on the status"
        return None
    return f"Fast Checks no longer runs `{MYPY_ON_TESTS} || status=1`"


def _loosened_settings(head: dict[str, Any], base: dict[str, Any]) -> list[str]:
    """How mypy checks the tests less than at the base."""
    found: list[str] = []
    for key in sorted(head.keys() | base.keys()):
        new, old = head.get(key), base.get(key)
        if new == old:
            continue
        if key in MYPY_GAINED_LOOSENS or key in MYPY_LOST_LOOSENS:
            gained, lost = (
                set(new or []) - set(old or []),
                set(old or []) - set(new or []),
            )
            loosened = gained if key in MYPY_GAINED_LOOSENS else lost
            found.extend(
                f"{PYPROJECT}: mypy no longer reports {entry} in tests"
                if key == "disable_error_code"
                else f"{PYPROJECT}: mypy {key} for the tests "
                f"{'gained' if key in MYPY_GAINED_LOOSENS else 'lost'} {entry!r}"
                for entry in sorted(loosened)
            )
        elif key not in MYPY_STRICT or new != MYPY_STRICT[key]:
            found.append(
                f"{PYPROJECT}: mypy {key} for the tests changed from {old!r} to {new!r}"
            )
    return found


def _check_mypy(base: Base, repo: Path, problems: list[str]) -> None:
    """mypy checks every test as it did at the base, or more."""
    base_text = base.text(PYPROJECT)
    test_paths = _test_paths(repo)
    head = _mypy_state((repo / PYPROJECT).read_text(encoding="utf-8"), test_paths)
    at_base = _mypy_state(base_text or "", test_paths)
    problems.extend(
        f"{PYPROJECT}: mypy exclude gained {pattern!r}"
        for pattern in sorted(head.excluded - at_base.excluded)
    )
    # The same loosening in many modules is reported once.
    problems.extend(
        dict.fromkeys(
            problem
            for module, applied in sorted(head.settings.items())
            for problem in _loosened_settings(applied, at_base.settings[module])
        )
    )
    problems.extend(
        f"{PYPROJECT}: follow_imports = {follow!r} for {module} turns the types "
        "the tests are checked against into Any"
        for module, follow in sorted(head.untyped - at_base.untyped)
    )
    for module in sorted(head.exempt - at_base.exempt):
        if "*" in module:
            problems.append(f"{PYPROJECT}: {module} exempts a whole tree from mypy")
        elif not base.has(_module_path(base, module)):
            problems.append(f"{PYPROJECT}: {module} is new and exempt from mypy")
        else:
            problems.append(f"{PYPROJECT}: {module} was added to the mypy list")
    problems.extend(
        f"{name}: mypy reads it instead of {PYPROJECT}'s settings"
        for name in MYPY_CONFIG_FILES
        if (repo / name).exists()
    )
    if (problem := _mypy_step_problem(repo / WORKFLOW)) is not None:
        problems.append(f"{WORKFLOW}: {problem}")


def _check_rules(base: Base, repo: Path, problems: list[str]) -> None:
    head = _test_rules(
        {
            path.relative_to(repo).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted((repo / "rules").glob("*.yml"))
        }
    )
    base_texts = {}
    for name in base.names("rules"):
        text = base.text(name) if name.endswith(".yml") else None
        if text is not None:
            base_texts[name] = text
    old = _test_rules(base_texts)
    problems.extend(
        f"rules: {rule_id} was removed or renamed"
        for rule_id in sorted(set(old) - set(head))
        if rule_id not in RETIRED_RULES or old[rule_id].get("ignores")
    )
    for rule_id, rule in sorted(head.items()):
        before = old.get(rule_id)
        widened = before is None or _definition(before) != _definition(rule)
        if before is not None and not set(before.get("files") or []) <= set(
            rule.get("files") or []
        ):
            problems.append(f"rules: {rule_id} scans fewer files than at the base")
        listed_before = set(before.get("ignores") or []) if before else set()
        for entry in sorted(set(rule.get("ignores") or []) - listed_before):
            if not base.has(str(entry)):
                problems.append(f"rules: {rule_id} lists the new file {entry}")
            elif not widened:
                problems.append(f"rules: {entry} was added to {rule_id}'s list")


def _check_file_list(
    base: Base, path: str, head: set[str], before: set[str] | None, problems: list[str]
) -> None:
    for entry in sorted(head - (before or set())):
        if not base.has(entry):
            problems.append(f"{path}: lists the new file {entry}")
        elif before is not None:
            problems.append(f"{path}: {entry} was added to the list")


def _lists(base: Base, repo: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Every list's files, by list name, in the checkout and at the base."""
    head: dict[str, set[str]] = {}
    before: dict[str, set[str]] = {}
    head_rules = _test_rules(
        {
            path.relative_to(repo).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted((repo / "rules").glob("*.yml"))
        }
    )
    base_rules = _test_rules(
        {
            name: text
            for name in base.names("rules")
            if name.endswith(".yml") and (text := base.text(name)) is not None
        }
    )
    for lists, rules in ((head, head_rules), (before, base_rules)):
        for rule_id, rule in rules.items():
            lists[rule_id] = set(map(str, rule.get("ignores") or []))
    guard_base = base.text(GUARD)
    head[GUARD] = _guard_entries((repo / GUARD).read_text(encoding="utf-8"))
    if guard_base is not None:
        before[GUARD] = _guard_entries(guard_base)
    for path in RUFF_FILES:
        head[path] = _ruff_entries(path, (repo / path).read_text(encoding="utf-8"), [])
        ruff_base = base.text(path)
        if ruff_base is not None:
            before[path] = _ruff_entries(path, ruff_base, [])
    head[MYPY_LIST] = {
        _module_path(base, module)
        for module in _mypy_state((repo / PYPROJECT).read_text(encoding="utf-8")).exempt
    }
    pyproject_base = base.text(PYPROJECT)
    if pyproject_base is not None:
        before[MYPY_LIST] = {
            _module_path(base, module) for module in _mypy_state(pyproject_base).exempt
        }
    allowlist_head, allowlist_base = _allowlists(base, repo, [])
    head[INTEGRATION_ALLOWLIST] = {entry for entry, _ in allowlist_head or set()}
    if allowlist_base is not None:
        before[INTEGRATION_ALLOWLIST] = {entry for entry, _ in allowlist_base}
    return head, before


def _check_full_conversion(
    head: dict[str, set[str]], before: dict[str, set[str]], problems: list[str]
) -> None:
    """A file taken off one list is taken off every list. The integration
    allowlist holds kept stand-ins, so a file may stay on it."""
    taken_off: dict[str, list[str]] = {}
    for name, entries in sorted(before.items()):
        for entry in entries - head.get(name, set()):
            taken_off.setdefault(entry, []).append(name)
    lists = {name: e for name, e in head.items() if name != INTEGRATION_ALLOWLIST}
    for entry, names in sorted(taken_off.items()):
        still = sorted(name for name, entries in lists.items() if entry in entries)
        if still:
            problems.append(
                f"{entry} came off {', '.join(names)} but is still on "
                f"{', '.join(still)}; a converted file comes off every list"
            )


def _check_changed_listed(
    base: Base, repo: Path, head: dict[str, set[str]], problems: list[str]
) -> None:
    """A listed file the pull request changes is converted: it is on no list.
    The integration allowlist holds kept stand-ins, so it is not one of these."""
    changed = base.changed()
    lists = {name: e for name, e in head.items() if name != INTEGRATION_ALLOWLIST}
    listed = {entry for entries in lists.values() for entry in entries}
    for entry in sorted(listed & changed):
        if (repo / entry).is_file() and base.has(entry):
            still = sorted(name for name, entries in lists.items() if entry in entries)
            problems.append(
                f"{entry} changed but is still on {', '.join(still)}; a listed "
                "file a pull request changes is converted and comes off every list"
            )


def _allowlisted(text: str | None, problems: list[str]) -> set[tuple[str, str]] | None:
    """The (file, integration) pairs of the integration allowlist, read as its
    check reads them."""
    if text is None:
        return None
    return set(_READER.parse_allowlist(text, problems))


def _code(text: str | None) -> str | None:
    """A script's code, without its comments and docstrings."""
    if text is None:
        return None
    tree = ast.parse(text)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
    return ast.dump(tree)


def _allowlists(
    base: Base, repo: Path, problems: list[str]
) -> tuple[set[tuple[str, str]] | None, set[tuple[str, str]] | None]:
    """The integration allowlist in the checkout, whose bad entries are
    ``problems``, and at the base."""
    path = repo / INTEGRATION_ALLOWLIST
    text = path.read_text(encoding="utf-8") if path.is_file() else None
    return _allowlisted(text, problems), _allowlisted(
        base.text(INTEGRATION_ALLOWLIST), []
    )


def _check_integration_allowlist(base: Base, repo: Path, problems: list[str]) -> None:
    """The allowlist is one its check accepts, and grows by the same two rules
    as every other list, per file and integration."""
    head, before = _allowlists(base, repo, problems)
    widened = before is None or any(
        _code(base.text(name))
        != _code(
            (repo / name).read_text(encoding="utf-8")
            if (repo / name).is_file()
            else None
        )
        for name in INTEGRATION_CHECK
    )
    for entry, integration in sorted((head or set()) - (before or set())):
        if not base.has(entry):
            problems.append(f"{INTEGRATION_ALLOWLIST}: lists the new file {entry}")
        elif not widened:
            problems.append(
                f"{INTEGRATION_ALLOWLIST}: {entry} was added to the list "
                f"for {integration!r}"
            )


def problems_since(repo: Path, commit: str) -> list[str]:
    """Every way the checkout exempts a test that the base did not."""
    base = Base(repo, commit)
    problems: list[str] = []
    _check_rules(base, repo, problems)

    guard_base = base.text(GUARD)
    _check_file_list(
        base,
        GUARD,
        _guard_entries((repo / GUARD).read_text(encoding="utf-8")),
        _guard_entries(guard_base) if guard_base is not None else None,
        problems,
    )
    for path in RUFF_FILES:
        ruff_base = base.text(path)
        _check_file_list(
            base,
            path,
            _ruff_entries(path, (repo / path).read_text(encoding="utf-8"), problems),
            _ruff_entries(path, ruff_base, []) if ruff_base is not None else None,
            problems,
        )
    _check_mypy(base, repo, problems)
    head_lists, base_lists = _lists(base, repo)
    _check_changed_listed(base, repo, head_lists, problems)
    _check_full_conversion(head_lists, base_lists, problems)
    _check_integration_allowlist(base, repo, problems)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--base", required=True, help="commit to compare the lists with"
    )
    parser.add_argument("--repo", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    # An unreadable base would make every list look new and pass.
    if not Base(args.repo, args.base).is_commit():
        print(f"{args.base} is not a commit in this repository; fetch it first")
        return 2

    problems = problems_since(args.repo, args.base)
    if problems:
        print("\n".join(problems))
        print(
            "\nA test is never exempted from the real-Core checks (tests/AGENTS.md, "
            "'Unit Tests Run on Real Home Assistant Core'). Write the new test "
            "against real Core instead: take the core_client fixture, do not reach "
            "into private members, and fix its type errors. A renamed file counts "
            "as new. A listed file the pull request changes is converted, which "
            "takes it off every list."
        )
        return 1
    print("No test was exempted from the real-Core checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
