#!/usr/bin/env python3
"""Extract MCP tool metadata via AST parsing (no runtime dependencies).

Parses tool source files statically to extract names, tags, annotations,
descriptions, and parameter schemas. Produces:
  - site/src/data/tools.json  (for Astro site tool explorer)
  - README.md update          (table between markers, badge count)

Usage:
    python scripts/extract_tools.py
    python scripts/extract_tools.py --check  # CI mode: exit 1 if out of sync
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, TypeGuard

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "src" / "ha_mcp" / "tools"
TOOLS_JSON_PATH = REPO_ROOT / "site" / "src" / "data" / "tools.json"
README_PATH = REPO_ROOT / "README.md"
DOCS_PATH = REPO_ROOT / "homeassistant-addon" / "DOCS.md"


README_START_MARKER = "<!-- TOOLS_TABLE_START -->"
README_END_MARKER = "<!-- TOOLS_TABLE_END -->"
DOCS_START_MARKER = "<!-- ADDON_TOOLS_START -->"
DOCS_END_MARKER = "<!-- ADDON_TOOLS_END -->"

TOOL_FILES = sorted(list(TOOLS_DIR.glob("tools_*.py")) + [TOOLS_DIR / "backup.py"])

ANNOTATION_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

PACKAGE_ROOT = REPO_ROOT / "src" / "ha_mcp"

# ``Field`` keywords with a home of their own in the extracted parameter.
# Every OTHER keyword that resolves statically is recorded under
# ``constraints`` — ``ge``/``le``, but also ``strict``, ``allow_inf_nan`` and
# whatever a future signature reaches for. A whitelist here would drop those
# silently, and unwrapping ``Annotated`` took away the raw source that used to
# expose them.
#
# Names stay as pydantic spells them, deliberately NOT translated to JSON
# Schema keywords: ``min_length`` means ``minLength`` on a string but
# ``minItems`` on a list, and ``type`` here is a Python annotation
# (``list[str] | None``) rather than a JSON Schema type, so there is nothing to
# read the distinction from.
FIELD_OWN_KEYS = ("description", "default")

# f-string conversions, keyed by ``ast.FormattedValue.conversion``: -1 is none,
# and the rest are the ord() of the ``!s`` / ``!r`` / ``!a`` flag. Ignoring
# these would render ``f"{X!r}"`` unquoted — a wrong description rather than a
# missing one.
_CONVERSIONS: dict[int, Callable[[Any], str]] = {
    -1: str,
    ord("s"): str,
    ord("r"): repr,
    ord("a"): ascii,
}

# String methods safe to apply to an already-resolved literal.
STR_METHODS = ("upper", "lower", "capitalize", "title", "strip", "lstrip", "rstrip")


class ModuleScope(NamedTuple):
    """Statically resolvable module-level names.

    ``consts`` maps a name to its literal value; ``funcs`` maps a zero-argument
    function's name to the literal it returns. Both are needed because tool
    files build parameter descriptions out of shared constants and helpers
    (``f"... {MAX_LIMIT}"``, ``... + get_security_documentation()``) rather than
    inline literals, and those descriptions are the point of the catalog.
    """

    consts: dict[str, Any]
    funcs: dict[str, Any]


EMPTY_SCOPE = ModuleScope({}, {})

# Distinct from None, which is itself a resolvable value: ``DEFAULT = None``
# assigned to a name and then passed as ``Field(default=DEFAULT)`` must survive.
UNRESOLVED: Any = object()


def _static_value(node: ast.expr, scope: ModuleScope) -> Any:
    """Resolve an expression to its value, or ``UNRESOLVED`` when it cannot be.

    Deliberately an interpreter over a handful of node types rather than
    ``eval``: the generator never executes repository source.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return scope.consts.get(node.id, UNRESOLVED)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_value(node.left, scope)
        right = _static_value(node.right, scope)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        return UNRESOLVED
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        # ``Field(ge=-1)`` parses as a unary minus over a literal, not a Constant.
        operand = _static_value(node.operand, scope)
        if isinstance(operand, (int, float)) and not isinstance(operand, bool):
            return -operand if isinstance(node.op, ast.USub) else operand
        return UNRESOLVED
    if isinstance(node, ast.JoinedStr):
        return _joined_str_value(node, scope)
    if isinstance(node, ast.Subscript):
        return _subscript_value(node, scope)
    if isinstance(node, ast.Call):
        return _call_value(node, scope)
    return UNRESOLVED


def _joined_str_value(node: ast.JoinedStr, scope: ModuleScope) -> Any:
    """Resolve an f-string whose interpolations are all statically known."""
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant):
            parts.append(str(value.value))
            continue
        if not isinstance(value, ast.FormattedValue) or value.format_spec is not None:
            return UNRESOLVED
        resolved = _static_value(value.value, scope)
        if resolved is UNRESOLVED:
            return UNRESOLVED
        parts.append(_CONVERSIONS[value.conversion](resolved))
    return "".join(parts)


def _subscript_value(node: ast.Subscript, scope: ModuleScope) -> Any:
    """Resolve ``SOME_TEXT[:1]`` style slicing of a known string."""
    target = _static_value(node.value, scope)
    if not isinstance(target, str) or not isinstance(node.slice, ast.Slice):
        return UNRESOLVED
    bounds: list[int | None] = []
    for part in (node.slice.lower, node.slice.upper, node.slice.step):
        if part is None:
            bounds.append(None)
            continue
        resolved = _static_value(part, scope)
        if not isinstance(resolved, int):
            return UNRESOLVED
        bounds.append(resolved)
    return target[slice(*bounds)]


def _call_value(node: ast.Call, scope: ModuleScope) -> Any:
    """Resolve a zero-argument helper call or a string method on a literal."""
    if node.args or node.keywords:
        return UNRESOLVED
    if isinstance(node.func, ast.Name):
        return scope.funcs.get(node.func.id, UNRESOLVED)
    if isinstance(node.func, ast.Attribute) and node.func.attr in STR_METHODS:
        target = _static_value(node.func.value, scope)
        if isinstance(target, str):
            return getattr(target, node.func.attr)()
    return UNRESOLVED


def _import_source(path: Path, node: ast.ImportFrom) -> Path | None:
    """Map a relative ``from . import x`` to the file it reads from."""
    if node.level == 0:
        return None
    base = path.parent
    for _ in range(node.level - 1):
        base = base.parent
    if node.module is None:
        # ``from . import NAME`` — the names live in the package initializer.
        candidates: tuple[Path, ...] = (base / "__init__.py",)
    else:
        candidate = base.joinpath(*node.module.split("."))
        candidates = (candidate.with_suffix(".py"), candidate / "__init__.py")
    for option in candidates:
        if option.is_file() and PACKAGE_ROOT in option.parents:
            return option
    return None


def _apply_imports(path: Path, tree: ast.Module, scope: ModuleScope) -> None:
    """Pull imported constants and helpers into ``scope``."""
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        source = _import_source(path, node)
        if source is None:
            continue
        imported = _module_scope(source)
        for alias in node.names:
            local = alias.asname or alias.name
            if alias.name in imported.consts:
                scope.consts[local] = imported.consts[alias.name]
            if alias.name in imported.funcs:
                scope.funcs[local] = imported.funcs[alias.name]


def _assigned_literal(node: ast.stmt) -> tuple[str, ast.expr] | None:
    """The (name, value expression) of a simple module-level assignment."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id, node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        if isinstance(node.target, ast.Name):
            return node.target.id, node.value
    return None


def _takes_no_arguments(args: ast.arguments) -> bool:
    """Whether the function can genuinely be called with no arguments.

    Every parameter kind counts, not just ``args``: a helper with a required
    positional-only or keyword-only parameter raises ``TypeError`` when called
    bare, so resolving ``helper()`` to its return value would describe code
    that cannot run.
    """
    return not (
        args.posonlyargs or args.args or args.kwonlyargs or args.vararg or args.kwarg
    )


def _returned_literal(node: ast.stmt) -> tuple[str, ast.expr] | None:
    """The (name, returned expression) of a zero-argument function.

    The whole body is searched, not just its top level: a helper that returns
    one string inside an ``if`` and another after it has no single value, and
    taking the last one would publish whichever branch the source happened to
    end with.
    """
    if not isinstance(node, ast.FunctionDef) or not _takes_no_arguments(node.args):
        return None
    returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
    if len(returns) != 1 or returns[0].value is None:
        return None
    return node.name, returns[0].value


def _definition_pass(tree: ast.Module, scope: ModuleScope) -> bool:
    """Resolve what it can this pass; report whether anything new resolved."""
    progressed = False
    for node in tree.body:
        assigned = _assigned_literal(node)
        if assigned is not None:
            name, expr = assigned
            if name in scope.consts:
                continue
            value = _static_value(expr, scope)
            if value is not UNRESOLVED:
                scope.consts[name] = value
                progressed = True
            continue
        returned = _returned_literal(node)
        if returned is not None:
            name, expr = returned
            if name in scope.funcs:
                continue
            value = _static_value(expr, scope)
            if value is not UNRESOLVED:
                scope.funcs[name] = value
                progressed = True
    return progressed


def _apply_definitions(tree: ast.Module, scope: ModuleScope) -> None:
    """Record module-level literals and zero-argument literal-returning helpers.

    Repeated until nothing new resolves: a single source-order pass misses a
    name that is defined after its use (a helper returning a constant declared
    lower in the file), and the parameter that reads it would silently lose its
    description.
    """
    while _definition_pass(tree, scope):
        pass


_SCOPE_CACHE: dict[Path, ModuleScope] = {}
_RESOLVING: set[Path] = set()


def _module_scope(path: Path) -> ModuleScope:
    """Build the statically resolvable names for one module.

    Each module is parsed once and cached: the package's import graph is wide
    enough that re-walking it per tool file turns the extraction exponential.
    A module already on the resolution stack (an import cycle) contributes
    nothing rather than recursing forever, and that partial answer is not
    cached.
    """
    if path in _SCOPE_CACHE:
        return _SCOPE_CACHE[path]
    if path in _RESOLVING:
        return EMPTY_SCOPE

    scope = ModuleScope({}, {})
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return scope

    _RESOLVING.add(path)
    try:
        _apply_imports(path, tree, scope)
        _apply_definitions(tree, scope)
    finally:
        _RESOLVING.discard(path)

    _SCOPE_CACHE[path] = scope
    return scope


def _is_annotated(node: ast.expr) -> bool:
    """Whether ``node`` names ``Annotated``, imported bare or via ``typing``.

    Tool files import it bare (``ast.Name``); the dotted ``typing.Annotated``
    spelling (``ast.Attribute``) is accepted too so the extractor does not
    depend on which import a file happens to use.
    """
    if isinstance(node, ast.Name):
        return node.id == "Annotated"
    return isinstance(node, ast.Attribute) and node.attr == "Annotated"


def _is_field_call(node: ast.expr) -> TypeGuard[ast.Call]:
    """Whether ``node`` is a pydantic ``Field(...)`` call."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "Field"
    return isinstance(func, ast.Attribute) and func.attr == "Field"


def _field_keyword(info: dict, kw: ast.keyword, scope: ModuleScope) -> None:
    """Fold one ``Field(...)`` keyword into the parameter info."""
    if kw.arg is None:
        return
    value = _static_value(kw.value, scope)
    if value is UNRESOLVED:
        return
    if kw.arg in FIELD_OWN_KEYS:
        info[kw.arg] = value
    else:
        info.setdefault("constraints", {})[kw.arg] = value


def _annotated_metadata(slice_node: ast.expr, scope: ModuleScope) -> dict:
    """Read ``type`` plus any ``Field(...)`` metadata out of an Annotated slice."""
    info: dict = {}
    if not isinstance(slice_node, ast.Tuple) or not slice_node.elts:
        return info

    info["type"] = ast.unparse(slice_node.elts[0])
    # Everything after the type is metadata, but only ``Field(...)`` carries
    # keywords this catalog describes. A bare validator (JSON_STRING_COERCION)
    # is a name rather than a call; another metadata call's keywords are its
    # own, and recording them as constraints would invent a rule pydantic
    # never applied.
    for elt in slice_node.elts[1:]:
        if not _is_field_call(elt):
            continue
        for kw in elt.keywords:
            _field_keyword(info, kw, scope)
    return info


def _union_field_info(annotation: ast.BinOp, scope: ModuleScope) -> dict:
    """Merge both sides of an ``X | Y`` annotation into one field info.

    ``Annotated[int, Field(ge=1)] | None`` keeps the Annotated part nested in a
    union, so each side is read on its own and the types are rejoined
    (``int | None``) rather than dumped as source into ``type``.
    """
    operands = [
        _extract_field_info(annotation.left, scope),
        _extract_field_info(annotation.right, scope),
    ]
    info: dict = {}
    # Metadata belongs to the branch that declared it, and only ``X | None``
    # lets it stand for the parameter: ``None`` carries no values of its own to
    # contradict it. Anywhere else — a second metadata-bearing branch, or a
    # plain type like ``Annotated[int, Field(ge=1)] | str`` — publishing it
    # would claim one branch's rules govern values of the other, so nothing is
    # published rather than something false.
    described = [o for o in operands if any(k != "type" for k in o)]
    others = [o for o in operands if o is not described[0]] if described else []
    if len(described) == 1 and all(o.get("type") == "None" for o in others):
        info.update({k: v for k, v in described[0].items() if k != "type"})
    info["type"] = " | ".join(o["type"] for o in operands if o.get("type"))
    return info


def _extract_field_info(
    annotation: ast.expr | None, scope: ModuleScope = EMPTY_SCOPE
) -> dict:
    """Extract type and description from Annotated[type, Field(...)] patterns."""
    if annotation is None:
        return {}

    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _union_field_info(annotation, scope)

    if isinstance(annotation, ast.Subscript) and _is_annotated(annotation.value):
        return _annotated_metadata(annotation.slice, scope)

    return {"type": ast.unparse(annotation)}


def _tool_name_from_tool_decorator(
    dec: ast.Call, node: ast.AsyncFunctionDef
) -> str | None:
    """Resolve the tool name from a Pattern 2 ``@tool(name="ha_*")`` decorator."""
    for kw in dec.keywords:
        if (
            kw.arg == "name"
            and isinstance(kw.value, ast.Constant)
            and str(kw.value.value).startswith("ha_")
        ):
            return str(kw.value.value)
    # Fallback: @tool() without name= on ha_* function
    if node.name.startswith("ha_"):
        return node.name
    return None


def _find_tool_decorator(
    node: ast.AsyncFunctionDef,
) -> tuple[ast.Call | None, str | None]:
    """Find the @mcp.tool / @tool decorator on a function and its tool name."""
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        # Pattern 1: @mcp.tool(...) — closure pattern, function named ha_*
        if isinstance(func, ast.Attribute) and func.attr == "tool":
            if node.name.startswith("ha_"):
                return dec, node.name
        # Pattern 2: @tool(name="ha_*") — class method pattern
        if isinstance(func, ast.Name) and func.id == "tool":
            tool_name = _tool_name_from_tool_decorator(dec, node)
            if tool_name is not None:
                return dec, tool_name
    return None, None


def _extract_tool_metadata(dec: ast.Call) -> tuple[set[str], str, dict[str, bool]]:
    """Extract tags, title, and annotation hints from a tool decorator call."""
    tags: set[str] = set()
    title = ""
    annotations: dict[str, bool] = {}

    for kw in dec.keywords:
        if kw.arg == "tags" and isinstance(kw.value, ast.Set):
            tags = {
                str(elt.value) for elt in kw.value.elts if isinstance(elt, ast.Constant)
            }
        elif kw.arg == "annotations" and isinstance(kw.value, ast.Dict):
            for k, v in zip(kw.value.keys, kw.value.values, strict=True):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    key = str(k.value)
                    if key == "title":
                        title = str(v.value)
                    elif key in ANNOTATION_KEYS:
                        annotations[key] = bool(v.value)

    return tags, title, annotations


def _extract_tool_params(
    node: ast.AsyncFunctionDef, scope: ModuleScope = EMPTY_SCOPE
) -> tuple[dict[str, dict], list[str]]:
    """Extract parameter properties and required-field names from a tool function."""
    properties: dict[str, dict] = {}
    required: list[str] = []
    defaults_offset = len(node.args.args) - len(node.args.defaults)

    for i, arg in enumerate(node.args.args):
        if arg.arg in ("self", "ctx"):
            continue
        p = _extract_field_info(arg.annotation, scope)
        def_idx = i - defaults_offset
        if def_idx >= 0 and def_idx < len(node.args.defaults):
            def_node = node.args.defaults[def_idx]
            if isinstance(def_node, ast.Constant):
                p.setdefault("default", def_node.value)
        elif "default" not in p:
            # ``Field(default=...)`` alone makes the parameter optional, so a
            # signature default is not the only thing that can supply one.
            required.append(arg.arg)
        if p:
            properties[arg.arg] = p

    return properties, required


def _extract_tool_from_node(
    node: ast.AsyncFunctionDef, source_file: str, scope: ModuleScope = EMPTY_SCOPE
) -> dict | None:
    """Build the tool-metadata dict for one function node, or None if not a tool."""
    tool_dec, tool_name = _find_tool_decorator(node)
    if tool_dec is None or tool_name is None:
        return None

    tags, title, annotations = _extract_tool_metadata(tool_dec)
    properties, required = _extract_tool_params(node, scope)

    input_schema: dict = {}
    if properties:
        input_schema = {"properties": properties}
        if required:
            input_schema["required"] = required

    return {
        "name": tool_name,
        "title": title,
        "description": ast.get_docstring(node) or "",
        "inputSchema": input_schema,
        "annotations": annotations,
        "tags": sorted(tags),
        "source_file": source_file,
    }


def extract_tools() -> list[dict]:
    """Extract all tool metadata from source files via AST parsing."""
    tools = []

    for f in TOOL_FILES:
        if not f.exists():
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        scope = _module_scope(f)

        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            tool = _extract_tool_from_node(node, f.name, scope)
            if tool is not None:
                tools.append(tool)

    # Detect duplicate tool names
    seen: dict[str, str] = {}
    for t in tools:
        name = str(t["name"])
        source = str(t["source_file"])
        if name in seen:
            # Raise rather than exit: this function is imported by the locale
            # check, where a bare SystemExit surfaces as an unexplained abort
            # instead of a named failure. ``main`` turns it back into exit 1.
            raise ValueError(
                f"Duplicate tool name '{name}' in {source} (first seen in {seen[name]})"
            )
        seen[name] = source

    tools.sort(key=lambda x: (next(iter(x["tags"]), "zzz"), x["name"]))
    return tools


def generate_docs_section(tools: list[dict]) -> str:
    """Generate the Available Tools section for homeassistant-addon/DOCS.md."""
    categories: dict[str, list[dict]] = {}
    for tool in tools:
        cat = tool["tags"][0] if tool["tags"] else "Other"
        categories.setdefault(cat, []).append(tool)

    lines = [
        DOCS_START_MARKER,
        "",
        f"The add-on provides {len(tools)}+ MCP tools for controlling Home Assistant:",
        "",
        '> **Note:** This list is regenerated from the `master` branch on every push, but the add-on image you have installed only updates on stable releases (biweekly, Wednesdays 10:00 UTC). A tool listed below may not yet be present in your installed runtime. If so, calling it returns an "unknown tool" error until the next stable release.',
        "",
    ]
    if any("beta" in t["tags"] for t in tools):
        lines.extend(
            [
                "> Tools marked **(beta — dev channel only)** are gated behind feature flags and ship with the dev channel add-on only. See [docs/beta.md](https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/beta.md) for setup and caveats.",
                "",
            ]
        )
    for cat in sorted(categories):
        lines.append(f"### {cat}")
        for tool in sorted(categories[cat], key=lambda t: t["name"]):
            desc = (
                tool["description"].split("\n")[0].strip()
                if tool["description"]
                else ""
            )
            entry = f"- `{tool['name']}`"
            if "beta" in tool["tags"]:
                entry += " **(beta — dev channel only)**"
            if desc:
                entry += f" — {desc}"
            lines.append(entry)
        lines.append("")
    lines.append(DOCS_END_MARKER)
    return "\n".join(lines)


def update_docs(tools: list[dict], *, content: str | None = None) -> str:
    """Replace the auto-generated section in DOCS.md between sync markers.

    Args:
        tools: Extracted tool metadata.
        content: File content to use instead of reading DOCS_PATH from disk.
            Pass this when the caller has already read the file (e.g. check_sync)
            to avoid a redundant read. When None, reads DOCS_PATH internally.
    """
    docs = content if content is not None else DOCS_PATH.read_text(encoding="utf-8")
    if DOCS_START_MARKER not in docs or DOCS_END_MARKER not in docs:
        # Raised rather than ``sys.exit``: the same reason the duplicate-name
        # check raises — a library function that exits gives its caller a bare
        # SystemExit to report instead of a named failure.
        raise ValueError(
            f"{DOCS_PATH} is missing sync markers. Add "
            f"{DOCS_START_MARKER!r} and {DOCS_END_MARKER!r} to the file first."
        )
    new_section = generate_docs_section(tools)
    pattern = re.compile(
        rf"{re.escape(DOCS_START_MARKER)}.*?{re.escape(DOCS_END_MARKER)}",
        re.DOTALL,
    )
    updated = pattern.sub(new_section, docs)
    updated = re.sub(
        r"\bprovides \d+\+ tools\b", f"provides {len(tools)}+ tools", updated
    )
    updated = re.sub(
        r"\bcatalog \(~\d+ tools\b", f"catalog (~{len(tools)} tools", updated
    )
    assert DOCS_START_MARKER in updated and DOCS_END_MARKER in updated
    return updated


def generate_tools_json(tools: list[dict]) -> str:
    return json.dumps(tools, indent=2, ensure_ascii=False) + "\n"


def generate_readme_table(tools: list[dict]) -> str:
    categories: dict[str, list[str]] = {}
    for tool in tools:
        cat = tool["tags"][0] if tool["tags"] else "Other"
        name = f"`{tool['name']}`"
        if "beta" in tool["tags"]:
            name += " *(beta)*"
        categories.setdefault(cat, []).append(name)

    lines = [
        README_START_MARKER,
        "",
        f"<summary><b>Complete Tool List ({len(tools)} tools)</b></summary>",
        "",
        "| Category | Tools |",
        "|----------|-------|",
    ]
    lines.extend(
        f"| **{cat}** | {', '.join(sorted(categories[cat]))} |"
        for cat in sorted(categories)
    )
    lines.extend(["", README_END_MARKER])
    return "\n".join(lines)


def update_readme(tools: list[dict], *, content: str | None = None) -> str:
    """Replace the tool table in README.md between markers.

    Args:
        tools: Extracted tool metadata.
        content: File content to use instead of reading README_PATH from disk.
            Pass this when the caller has already read the file (e.g. check_sync)
            to avoid a redundant read. When None, reads README_PATH internally.
    """
    readme = content if content is not None else README_PATH.read_text(encoding="utf-8")
    table = generate_readme_table(tools)
    count = len(tools)

    pattern = re.compile(
        rf"<details>\s*\n{re.escape(README_START_MARKER)}.*?{re.escape(README_END_MARKER)}\s*\n</details>",
        re.DOTALL,
    )
    new_block = f"<details>\n{table}\n</details>"

    if pattern.search(readme):
        readme = pattern.sub(new_block, readme)
    else:
        old_pattern = re.compile(
            r"<details>\s*\n<summary><b>[^<]*Complete Tool List[^<]*</b></summary>.*?</details>",
            re.DOTALL,
        )
        if old_pattern.search(readme):
            readme = old_pattern.sub(new_block, readme)
        else:
            # Not a warning: returning the content unchanged made ``--check``
            # report "All files in sync" on the very failure it had just
            # printed, because an unchanged return compares equal to the file
            # it came from. Losing the markers means the table can no longer be
            # regenerated at all, which is the loudest thing this script has to
            # say.
            raise ValueError(
                f"{README_PATH.name} is missing the tool-table markers "
                f"({README_START_MARKER!r} / {README_END_MARKER!r}) and no "
                "legacy Complete Tool List block was found; restore them so "
                "the table can be regenerated"
            )

    readme = re.sub(r"tools-[^-]+-blue", f"tools-{count}-blue", readme)
    return readme


def check_sync(tools: list[dict]) -> bool:
    in_sync = True

    expected_json = generate_tools_json(tools)
    if TOOLS_JSON_PATH.exists():
        if TOOLS_JSON_PATH.read_text(encoding="utf-8") != expected_json:
            print("OUT OF SYNC: site/src/data/tools.json", file=sys.stderr)
            in_sync = False
    else:
        print("MISSING: site/src/data/tools.json", file=sys.stderr)
        in_sync = False

    readme_content = README_PATH.read_text(encoding="utf-8")
    if readme_content != update_readme(tools, content=readme_content):
        print("OUT OF SYNC: README.md", file=sys.stderr)
        in_sync = False

    if DOCS_PATH.exists():
        docs_content = DOCS_PATH.read_text(encoding="utf-8")
        if docs_content != update_docs(tools, content=docs_content):
            print("OUT OF SYNC: homeassistant-addon/DOCS.md", file=sys.stderr)
            in_sync = False

    return in_sync


def _extract_and_apply(args: argparse.Namespace) -> None:
    """Run the extraction and either check or write the generated files.

    Split out so ``main`` can turn every ``ValueError`` this raises — a
    duplicate tool name, a file whose sync markers are gone — into one named
    exit-1 rather than a traceback.
    """
    tools = extract_tools()
    cat_count = len({t["tags"][0] for t in tools if t["tags"]})
    print(f"Extracted {len(tools)} tools across {cat_count} categories")

    if args.check:
        if check_sync(tools):
            print("All files in sync.")
        else:
            print(
                "\nRun 'python scripts/extract_tools.py' to regenerate.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        TOOLS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOOLS_JSON_PATH.write_text(generate_tools_json(tools), encoding="utf-8")
        print(f"Wrote {TOOLS_JSON_PATH.relative_to(REPO_ROOT)}")

        README_PATH.write_text(update_readme(tools), encoding="utf-8")
        print(f"Updated {README_PATH.relative_to(REPO_ROOT)}")

        DOCS_PATH.write_text(update_docs(tools), encoding="utf-8")
        print(f"Updated {DOCS_PATH.relative_to(REPO_ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract MCP tool metadata (AST-based, no runtime deps)"
    )
    parser.add_argument(
        "--check", action="store_true", help="CI mode: check sync without writing"
    )
    args = parser.parse_args()

    try:
        _extract_and_apply(args)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
