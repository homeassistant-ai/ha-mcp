#!/usr/bin/env python3
"""Fail when a pull request exempts a test from the real-Core checks.

The unit and app (add-on) tests run on real Home Assistant Core
(tests/AGENTS.md, "Unit Tests Run on Real Home Assistant Core"). Files written
before each check are listed with it: the ``ignores`` of the ast-grep rules
that scan the tests, ``_GRANDFATHERED`` in ``tests/core_stand_in_guard.py``,
the ``SLF001`` entries of the two test ``ruff.toml`` files and the mypy
overrides that name a ``tests.`` module in ``pyproject.toml``. The lists are
plain files, so a pull request could add its own new test to one and every
check would pass. With ``--base`` this compares them with the base commit.

Two rules decide every added entry:

- A file that does not exist at the base is new, and a new file is never
  listed, whatever else the pull request changes. A renamed file is new.
- A file that does exist at the base may be listed only when the check itself
  is new or wider than at the base: the list did not exist, or its ast-grep
  rule's definition changed. A wider rule matches older files it did not
  match before; nothing else justifies a longer list.

A listed file is exempt as a whole, so a pull request could also add a new
faked test to one. For every file on an ast-grep rule's list that the pull
request changes, the script counts what the rule refuses in the file at the
base and in the checkout: the count may fall or stay, never rise.

The script also refuses the ways around a list: narrowing a rule's ``files``,
removing or renaming a rule, loosening the ``tests.*`` mypy settings, and
turning ``SLF001`` off in a test ``ruff.toml``.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO = Path(__file__).resolve().parent.parent
GUARD = "tests/core_stand_in_guard.py"
RUFF_FILES = ("tests/src/unit/ruff.toml", "tests/addon/ruff.toml")
PYPROJECT = "pyproject.toml"
WORKFLOW = ".github/workflows/pr.yml"
# The Fast Checks step that runs mypy on the tests.
MYPY_ON_TESTS = "scripts/mypy_tests_exact.py"
# The parts of an ast-grep rule that decide what it matches.
RULE_DEFINITION = ("rule", "constraints", "utils", "language")
MYPY_LOOSENING = ("ignore_errors", "ignore_missing_imports", "follow_imports")


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
    values: list[ast.Assign | ast.AnnAssign | ast.AugAssign] = []
    for node in ast.walk(ast.parse(text)):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "_GRANDFATHERED" for t in targets):
            values.append(node)
    if len(values) != 1 or isinstance(values[0], ast.AugAssign):
        raise ValueError(f"{GUARD} must assign _GRANDFATHERED exactly once")
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


def _mypy_state(text: str) -> tuple[set[str], dict[str, Any], set[str]]:
    """The exempt test modules, the ``tests.*`` settings and the test excludes."""
    mypy = tomllib.loads(text).get("tool", {}).get("mypy", {})
    exempt: set[str] = set()
    settings: dict[str, Any] = {}
    for override in mypy.get("overrides", []):
        modules = _modules(override)
        if modules == ["tests.*"]:
            settings = {k: v for k, v in override.items() if k != "module"}
            continue
        exempt.update(m for m in modules if m == "tests" or m.startswith("tests."))
    # A code disabled for every module is disabled for the tests too.
    settings["disable_error_code"] = sorted(
        {*settings.get("disable_error_code", []), *mypy.get("disable_error_code", [])}
    )
    exclude = mypy.get("exclude", [])
    excluded = {
        pattern
        for pattern in ([exclude] if isinstance(exclude, str) else exclude)
        if "tests" in pattern
    }
    return exempt, settings, excluded


def _loosened_settings(
    head: dict[str, Any], base: dict[str, Any], checked_at_base: bool
) -> list[str]:
    """How the ``tests.*`` override checks less than at the base."""
    found = [
        f"{PYPROJECT}: the tests.* mypy override gained {key}"
        for key in MYPY_LOOSENING
        if key in head and key not in base
    ]
    if checked_at_base:
        gained = set(head.get("disable_error_code", [])) - set(
            base.get("disable_error_code", [])
        )
        found.extend(
            f"{PYPROJECT}: mypy no longer reports {code} in tests"
            for code in sorted(gained)
        )
        if head.get("check_untyped_defs") is False:
            found.append(f"{PYPROJECT}: mypy no longer checks untyped tests")
    return found


def _check_mypy(base: Base, head_text: str, problems: list[str]) -> None:
    base_text = base.text(PYPROJECT)
    workflow = base.text(WORKFLOW) or ""
    checked_at_base = base_text is not None and MYPY_ON_TESTS in workflow
    head_exempt, head_settings, head_excluded = _mypy_state(head_text)
    base_exempt, base_settings, base_excluded = (
        _mypy_state(base_text) if base_text is not None else (set(), {}, set())
    )
    problems.extend(
        f"{PYPROJECT}: mypy exclude gained {pattern!r}"
        for pattern in sorted(head_excluded - base_excluded)
    )
    problems.extend(_loosened_settings(head_settings, base_settings, checked_at_base))
    for module in sorted(head_exempt - base_exempt):
        if "*" in module:
            problems.append(f"{PYPROJECT}: {module} exempts a whole tree from mypy")
        elif not base.has(_module_path(base, module)):
            problems.append(f"{PYPROJECT}: {module} is new and exempt from mypy")
        elif checked_at_base:
            problems.append(f"{PYPROJECT}: {module} was added to the mypy list")


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
    _check_listed_files(base, repo, head, problems)


def _ast_grep() -> str:
    found = shutil.which(
        "ast-grep", path=str(Path(sys.executable).parent)
    ) or shutil.which("ast-grep")
    if not found:
        raise RuntimeError("ast-grep-cli (dev group) is not installed")
    return found


def _match_counts(rule: dict[str, Any], root: Path, files: list[str]) -> dict[str, int]:
    """How often ``rule`` matches each of ``files`` under ``root``, with the
    rule's own ``files`` and ``ignores`` set aside."""
    scanned = {k: v for k, v in rule.items() if k not in ("files", "ignores")}
    with tempfile.TemporaryDirectory() as directory:
        rule_file = Path(directory) / "rule.yml"
        rule_file.write_text(yaml.safe_dump(scanned), encoding="utf-8")
        proc = subprocess.run(
            [_ast_grep(), "scan", "--rule", str(rule_file), "--json=compact", *files],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    try:
        rows = json.loads(proc.stdout)
    except ValueError as err:
        raise RuntimeError(f"ast-grep failed on {rule['id']}: {proc.stderr}") from err
    counts = dict.fromkeys(files, 0)
    for row in rows:
        counts[Path(row["file"]).as_posix()] += 1
    return counts


def _check_listed_files(
    base: Base, repo: Path, rules: dict[str, dict[str, Any]], problems: list[str]
) -> None:
    """A listed file may change, but not gain what its list excuses."""
    changed = base.changed()
    for rule_id, rule in sorted(rules.items()):
        files = sorted(
            entry
            for entry in map(str, rule.get("ignores") or [])
            if entry in changed and (repo / entry).is_file() and base.has(entry)
        )
        if not files:
            continue
        now = _match_counts(rule, repo, files)
        with tempfile.TemporaryDirectory() as directory:
            for entry in files:
                target = Path(directory) / entry
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(base.text(entry) or "", encoding="utf-8")
            before = _match_counts(rule, Path(directory), files)
        problems.extend(
            f"rules: {entry} has {now[entry]} of what {rule_id} refuses, "
            f"{before[entry]} at the base; a listed file may not gain any"
            for entry in files
            if now[entry] > before[entry]
        )


def _check_file_list(
    base: Base, path: str, head: set[str], before: set[str] | None, problems: list[str]
) -> None:
    for entry in sorted(head - (before or set())):
        if not base.has(entry):
            problems.append(f"{path}: lists the new file {entry}")
        elif before is not None:
            problems.append(f"{path}: {entry} was added to the list")


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
    _check_mypy(base, (repo / PYPROJECT).read_text(encoding="utf-8"), problems)
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
            "as new. A listed file is exempt for what it already holds: a test added "
            "to it goes in a new file, or the file is converted."
        )
        return 1
    print("No test was exempted from the real-Core checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
