#!/usr/bin/env python3
"""Fail when production code has a public name only the tests use.

A test reaches the code through the entry point a user reaches (tests/AGENTS.md,
"Unit Tests Run on Real Home Assistant Core"). A helper made public for a test,
or a public name whose last production caller is gone, lets a test skip that
entry point; this finds both. A name is defined at module level (def, class,
assignment) or as a method, without a leading underscore, in ``src/ha_mcp``,
``custom_components`` or ``scripts``. Production uses it when:

- production Python names it (a name, an attribute, an import), its own
  definition aside;
- production Python holds it in a string (``getattr``, a registry), an
  ``__all__`` entry aside;
- a file production reads names it (pyproject, workflows, manifests, YAML,
  shell);
- the framework calls it by convention: a decorated function or method, a Home
  Assistant hook (``async_setup*``, ``async_step_*``, ...), a config flow class,
  a method of a class with a base (it may override one), a tool module's
  ``register_*_tools``, ``main``.

A name production does not use and a test names is refused, unless it is one
of ``EXEMPT``: the module state the tests reset between tests.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PRODUCTION = ("src/ha_mcp", "custom_components", "scripts")
NON_PYTHON = (
    "pyproject.toml",
    ".github/**/*.yml",
    ".github/**/*.yaml",
    "**/manifest.json",
    "**/*.sh",
    "lefthook.yml",
    "homeassistant-addon*/**/*.yaml",
    "sgconfig.yml",
)
HA_HOOK = re.compile(
    r"^(async_setup.*|async_unload.*|async_remove.*|async_migrate.*|async_step_.*|"
    r"async_get_options_flow|async_get_config_entry_diagnostics|async_reset_.*|"
    r"async_get_.*_flow|async_create_fix_flow|register_.*_tools|main|setup|run)$"
)
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Module state the tests reset between tests; production never needs to.
EXEMPT = frozenset(
    {
        "src/ha_mcp/config.py:reset_embedded_connection",
        "src/ha_mcp/redaction.py:clear_known_secret_values",
        "src/ha_mcp/policy/value_sources.py:clear_value_source_cache",
        "custom_components/ha_mcp_tools/llm_api.py:reset_schema_converter",
        "src/ha_mcp/utils/usage_logger.py:shutdown_usage_logger",
    }
)


def _production(repo: Path) -> list[Path]:
    return [
        path
        for tree in PRODUCTION
        for path in sorted((repo / tree).rglob("*.py"))
        if "_vendor" not in path.parts and "skills-vendor" not in path.parts
    ]


def _definitions(tree: ast.Module) -> Iterator[str]:
    """The public names a module defines that the framework does not call."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A config flow registers through its ``domain=`` class keyword.
            registered = isinstance(node, ast.ClassDef) and any(
                keyword.arg == "domain" for keyword in node.keywords
            )
            if not node.decorator_list and not registered:
                yield node.name
            if isinstance(node, ast.ClassDef) and not node.bases:
                yield from (
                    item.name
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not item.decorator_list
                )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            yield from (t.id for t in targets if isinstance(t, ast.Name))


def _words(paths: list[Path]) -> set[str]:
    words: set[str] = set()
    for path in paths:
        try:
            words |= set(WORD.findall(path.read_text(encoding="utf-8")))
        except UnicodeDecodeError, IsADirectoryError:
            continue
    return words


def _named(tree: ast.Module) -> set[str]:
    """Every name a module reads, imports or holds in a string."""
    listed = {
        elt.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
        and isinstance(node.value, (ast.List, ast.Tuple))
        for elt in node.value.elts
        if isinstance(elt, ast.Constant)
    }
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            named.add(node.id)
        elif isinstance(node, ast.Attribute):
            named.add(node.attr)
        elif isinstance(node, ast.alias):
            named.add((node.asname or node.name).split(".")[-1])
            named.add(node.name.split(".")[-1])
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value not in listed
        ):
            named |= set(WORD.findall(node.value))
    return named


def test_only_names(repo: Path) -> list[str]:
    """``path:name`` of every public name only the tests use, EXEMPT aside."""
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8")) for path in _production(repo)
    }
    used: set[str] = set().union(*map(_named, trees.values()))
    used |= _words(
        [
            path
            for pattern in NON_PYTHON
            for path in repo.glob(pattern)
            if ".venv" not in path.parts and "node_modules" not in path.parts
        ]
    )
    tested = _words(sorted((repo / "tests").rglob("*.py")))
    found = []
    for path, tree in trees.items():
        relative = path.relative_to(repo).as_posix()
        for name in _definitions(tree):
            if name.startswith("_") or HA_HOOK.match(name) or name in used:
                continue
            if name in tested and f"{relative}:{name}" not in EXEMPT:
                found.append(f"{relative}:{name}")
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    found = test_only_names(args.repo)
    if not found:
        print("No public name is used only by the tests.")
        return 0
    print("\n".join(found))
    print(
        "\nOnly the tests use these public names. A test reaches the code through "
        "the entry point a user reaches (tests/AGENTS.md, 'Unit Tests Run on Real "
        "Home Assistant Core'): call the tool, the command or the script's main, "
        "and make the name private or delete it."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
