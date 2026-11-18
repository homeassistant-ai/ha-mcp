#!/usr/bin/env python3
"""Fail when production code has a public name only the tests use.

A test reaches the code through the entry point a user reaches (tests/AGENTS.md,
"Unit Tests Run on Real Home Assistant Core"). A helper made public for a test,
or a public name whose last production caller is gone, lets a test skip that
entry point; this finds both. A name is defined at module level (def, class,
assignment) or as a method, without a leading underscore, in ``src/ha_mcp``,
``custom_components`` or ``scripts``. Production, which also counts the apps'
Python (``homeassistant-addon*``), uses it when:

- production Python names it (a name, an attribute, an import), its own
  definition aside, or reads it as an attribute of a receiver no import names
  in a module that hands its module on as a value;
- production Python holds it in a string (``getattr``, a registry), an
  ``__all__`` entry or a docstring aside;
- a file production reads names it (pyproject, workflows, manifests, YAML,
  shell);
- the framework calls it by convention: a decorated function or method, a Home
  Assistant hook (``async_setup*``, ``async_step_*``, ...), a config flow class,
  a method of a class with a base (it may override one), a tool module's
  ``register_*_tools``, ``main``.

A name production does not use and a test names is refused, unless it is one
of ``EXEMPT``: the module state the tests reset between tests, and the
cleanup of a server process a test started.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parent.parent
PRODUCTION = ("src/ha_mcp", "custom_components", "scripts")
# Python that calls production names; its own names follow the apps' release flow.
CALLERS = (*PRODUCTION, "homeassistant-addon*")
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
# Module state the tests reset between tests, and the cleanup of a server
# process a test started; production needs neither.
EXEMPT = frozenset(
    {
        "src/ha_mcp/config.py:reset_embedded_connection",
        "src/ha_mcp/__main__.py:reset_main_runtime",
        "src/ha_mcp/settings_ui/__init__.py:reset_http_settings_state",
        "src/ha_mcp/visibility/enforcement.py:reset_hidden_set_cache",
        "src/ha_mcp/redaction.py:clear_known_secret_values",
        "src/ha_mcp/policy/value_sources.py:clear_value_source_cache",
        "custom_components/ha_mcp_tools/llm_api.py:reset_schema_converter",
        "custom_components/ha_mcp_tools/card_definitions.py:reset_card_definitions_state",
        "custom_components/ha_mcp_tools/custom_cards.py:reset_custom_cards_state",
        "custom_components/ha_mcp_tools/oauth_autoapprove.py:reset_callback_refusal_log",
        "src/ha_mcp/utils/usage_logger.py:shutdown_usage_logger",
        "src/ha_mcp/stdio_settings_sidecar.py:retire_sidecar",
        "src/ha_mcp/tools/integration_flow_steps.py:reset_opened_flows",
    }
)


def _python(repo: Path, patterns: tuple[str, ...]) -> list[Path]:
    return [
        path
        for pattern in patterns
        for tree in sorted(repo.glob(pattern))
        for path in sorted(tree.rglob("*.py"))
        if "_vendor" not in path.parts and "skills-vendor" not in path.parts
    ]


def _definitions(tree: ast.Module) -> Iterator[tuple[str, bool]]:
    """``(name, is_method)`` of the public names a module defines that the
    framework does not call."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A config flow registers through its ``domain=`` class keyword.
            registered = isinstance(node, ast.ClassDef) and any(
                keyword.arg == "domain" for keyword in node.keywords
            )
            if not node.decorator_list and not registered:
                yield node.name, False
            if isinstance(node, ast.ClassDef) and not node.bases:
                yield from (
                    (item.name, True)
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not item.decorator_list
                )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            yield from ((t.id, False) for t in targets if isinstance(t, ast.Name))


def _module_name(relative: str) -> str:
    """The dotted name production imports ``relative`` by (``scripts`` is on
    the path of the scripts that import each other)."""
    stem = relative.removesuffix(".py").removesuffix("/__init__")
    for root in ("src/", "scripts/"):
        stem = stem.removeprefix(root)
    return stem.replace("/", ".")


class _Uses(NamedTuple):
    # (module, name) a module reaches: imported from it, read as module.name,
    # or read inside the module that defines it.
    qualified: set[tuple[str, str]]
    # Every attribute read: a method's receiver cannot be resolved statically.
    attributes: set[str]
    # Words in strings (``getattr``, registries), ``__all__`` entries aside.
    strings: set[str]


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (value := _dotted(node.value)):
        return f"{value}.{node.attr}"
    return None


def _naming_only(tree: ast.Module) -> set[int]:
    """The strings that name a function without using it: ``__all__`` entries
    and docstrings."""
    return {
        id(elt)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
        and isinstance(node.value, (ast.List, ast.Tuple))
        for elt in node.value.elts
    } | {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, _DOCUMENTED)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _uses(tree: ast.Module, module: str, is_package: bool) -> _Uses:
    exported = _naming_only(tree)
    package = module if is_package else module.rpartition(".")[0]
    uses = _Uses(set(), set(), set())
    # Imported modules this one hands on as a value (into ``HANDLERS``, as an
    # argument), and attributes read through a receiver no import names
    # (``handler = HANDLERS[radio]``, a parameter), which may hold one of them.
    handed_on: set[str] = set()
    unresolved: set[str] = set()

    def walk(scope: ast.AST, enclosing: dict[str, str]) -> None:
        # A scope's imports bind for its whole body, wherever they stand.
        nodes = list(_scope_nodes(scope))
        aliases = {**enclosing, **_bindings(nodes, package)}
        owners = {id(node.value) for node in nodes if isinstance(node, ast.Attribute)}
        for node in nodes:
            if isinstance(node, _SCOPES):
                walk(node, aliases)
            elif isinstance(node, ast.ImportFrom):
                base = _import_base(node, package)
                uses.qualified.update((base, alias.name) for alias in node.names)
            elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
                uses.qualified.add((module, node.id))
                if node.id in aliases and id(node) not in owners:
                    handed_on.add(aliases[node.id])
            elif isinstance(node, ast.Attribute):
                _read_attribute(node, aliases, uses, unresolved)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in exported
            ):
                uses.strings.update(WORD.findall(node.value))

    walk(tree, {})
    uses.qualified.update((m, name) for m in handed_on for name in unresolved)
    return uses


def _read_attribute(
    node: ast.Attribute, aliases: dict[str, str], uses: _Uses, unresolved: set[str]
) -> None:
    """Record ``owner.attr``: the module ``owner`` names, or ``attr`` as read
    through a receiver no import names."""
    uses.attributes.add(node.attr)
    owner = _dotted(node.value)
    head, _, rest = (owner or "").partition(".")
    if head not in aliases:
        unresolved.add(node.attr)
    if owner is not None:
        target = ".".join(filter(None, [aliases.get(head, head), rest]))
        uses.qualified.add((target, node.attr))


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """The nodes of ``scope``'s own body; a nested function's body is its own."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _import_base(node: ast.ImportFrom, package: str) -> str:
    """The module ``from <base> import ...`` reads, relative imports resolved."""
    parts = package.split(".") if node.level else []
    return ".".join(
        [*parts[: len(parts) - node.level + 1], *filter(None, [node.module])]
    )


_LOADERS = frozenset({"module_from_spec", "import_module", "load_source"})
# A module a script loads by its file path, under the name it is bound to
# (``tool_hints = module_from_spec(...)``), which names it after the file.
# ``import_module("a.b")`` names the module itself.
_LOADED = "<loaded>."


def _bindings(nodes: list[ast.AST], package: str) -> dict[str, str]:
    """What each name an import or a module loader in ``nodes`` binds refers to."""
    bound: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                bound[name] = alias.name if alias.asname else name
        elif isinstance(node, ast.ImportFrom):
            base = _import_base(node, package)
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{base}.{alias.name}"
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and (loader := _dotted(node.value.func)) is not None
            and loader.rpartition(".")[2] in _LOADERS
        ):
            call = node.value
            named = (
                call.args[0].value
                if loader.endswith("import_module")
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
                else None
            )
            bound.update(
                (target.id, named or f"{_LOADED}{target.id}")
                for target in node.targets
                if isinstance(target, ast.Name)
            )
    return bound


def _words(paths: list[Path]) -> set[str]:
    words: set[str] = set()
    for path in paths:
        try:
            words |= set(WORD.findall(path.read_text(encoding="utf-8")))
        except UnicodeDecodeError, IsADirectoryError:
            continue
    return words


def test_only_names(repo: Path) -> list[str]:
    """``path:name`` of every public name only the tests use, EXEMPT aside."""
    trees = {
        path.relative_to(repo).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in _python(repo, CALLERS)
    }
    defining = {path.relative_to(repo).as_posix() for path in _python(repo, PRODUCTION)}
    uses = _Uses(set(), set(), set())
    for relative, tree in trees.items():
        mod = _uses(tree, _module_name(relative), relative.endswith("/__init__.py"))
        uses.qualified.update(mod.qualified)
        uses.attributes.update(mod.attributes)
        uses.strings.update(mod.strings)
    uses.strings.update(
        _words(
            [
                path
                for pattern in NON_PYTHON
                for path in repo.glob(pattern)
                if ".venv" not in path.parts and "node_modules" not in path.parts
            ]
        )
    )
    tested = _words(sorted((repo / "tests").rglob("*.py")))
    found = []
    for relative in sorted(defining):
        module = _module_name(relative)
        for name, is_method in _definitions(trees[relative]):
            if name.startswith("_") or HA_HOOK.match(name) or name in uses.strings:
                continue
            loaded = f"{_LOADED}{module.rpartition('.')[2]}"
            reached = {(module, name), (loaded, name)}
            if reached & uses.qualified or (is_method and name in uses.attributes):
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
