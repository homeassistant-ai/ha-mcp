"""Static regression: every top-level key a tool can emit on its response
must be enumerated in the tool's ``fields=`` parameter description so AI
agents know it can be projected via ``fields=[...]``.

Caught ``dismissed_repair_count`` missing from ``ha_get_overview`` — added
in PR #1309 but not enumerated when PR #1225 introduced the ``Available
keys: ...`` docstring list.

The check is purely static. For each tool with a ``fields=`` parameter
we AST-walk the function(s) that build the projectable dict, collect
every string-literal key that gets assigned at the top level, and
assert that set is documented.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "ha_mcp"

# project_fields auto-retains these — never required in an Available keys list.
_AUTO_RETAINED = frozenset({"success", "warnings"})


# Per-tool spec. Each tool's ``fields=`` description enumerates the
# projectable top-level keys; this test verifies the source actually
# emits no more than what's documented.
#
# Keys:
#   tool            human-readable tool name (used for pytest id only)
#   docstring       (module_relpath, function_name) — where to read the
#                   ``fields=`` Annotated[..., Field(description=...)] text
#   var_harvest     list of (module_relpath, function_name, var_name) —
#                   harvest keys from assignments to ``var_name`` inside
#                   the named function (dict-literal init, ``var["k"] =``,
#                   ``var.setdefault("k", ...)``, ``var.update({"k": ...})``)
#   return_harvest  list of (module_relpath, function_name) — harvest
#                   top-level keys from every ``return {...}`` dict literal
#                   in that function. Used for helpers that build the
#                   projectable dict outright or for helpers whose dict is
#                   ``**splatted`` into the response dict literal.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "tool": "ha_get_overview",
        "docstring": ("tools/tools_search.py", "ha_get_overview"),
        "var_harvest": [
            ("tools/tools_search.py", "ha_get_overview", "result"),
            # `settings_url` is assigned after `project_fields(result, ...)`
            # to a separate `projected` var, so it bypasses `fields=`
            # filtering and is always emitted when the sidecar is running.
            ("tools/tools_search.py", "ha_get_overview", "projected"),
            ("tools/smart_search.py", "get_system_overview", "base_response"),
        ],
        "return_harvest": [],
    },
    {
        "tool": "ha_search_entities",
        "docstring": ("tools/tools_search.py", "ha_search_entities"),
        "var_harvest": [
            ("tools/tools_search.py", "ha_search_entities", "result"),
            ("tools/tools_search.py", "ha_search_entities", "response"),
        ],
        "return_harvest": [
            ("tools/tools_search.py", "_exact_match_search"),
            ("tools/smart_search.py", "smart_entity_search"),
        ],
        # ha_search_entities composes the projectable dict under many
        # local names (``area_search_data``, ``empty_area_data``,
        # ``domain_listing``...) and ``smart_entity_search`` assembles
        # ``response = {...}`` before mutating + returning. Catch both
        # by harvesting from any dict literal whose keys include a
        # response-shape marker.
        "marker_harvest": [
            (
                "tools/tools_search.py",
                "ha_search_entities",
                frozenset({"success", "results", "query"}),
            ),
            (
                "tools/smart_search.py",
                "smart_entity_search",
                frozenset({"success", "results", "query", "matches"}),
            ),
        ],
        # `matches` is the internal name smart_entity_search uses; the
        # wrapper renames it to `results` via `result.pop("matches")`
        # before the response reaches the caller, so it's never
        # user-visible despite the AST harvest finding it.
        "exclude_internal": frozenset({"matches"}),
    },
    {
        # ha_get_state's ``fields=`` projects HA's native entity-record
        # schema (entity_id, state, attributes, ...) — keys come from HA's
        # API, not from code we own, so AST harvest finds nothing. Instead
        # we pin the expected key set as a manifest and require the
        # docstring to enumerate exactly those keys. Updating HA's schema
        # forces an explicit manifest edit, which the test then catches.
        "tool": "ha_get_state",
        "docstring": ("tools/tools_search.py", "ha_get_state"),
        "var_harvest": [],
        "return_harvest": [],
        "documented_must_equal": frozenset(
            {
                "entity_id",
                "state",
                "attributes",
                "last_changed",
                "last_reported",
                "last_updated",
                "context",
            }
        ),
    },
    {
        "tool": "ha_get_history",
        "docstring": ("tools/tools_history.py", "ha_get_history"),
        "var_harvest": [
            ("tools/tools_history.py", "_fetch_history", "history_data"),
            ("tools/tools_history.py", "_fetch_statistics", "statistics_data"),
        ],
        "return_harvest": [],
    },
    {
        "tool": "ha_config_list_areas",
        "docstring": ("tools/tools_areas.py", "ha_config_list_areas"),
        "var_harvest": [
            ("tools/tools_areas.py", "ha_config_list_areas", "response"),
        ],
        "return_harvest": [],
    },
    {
        "tool": "ha_list_services",
        "docstring": ("tools/tools_services.py", "ha_list_services"),
        "var_harvest": [
            ("tools/tools_services.py", "ha_list_services", "result"),
        ],
        "return_harvest": [
            ("tools/tools_services.py", "_process_services"),
            ("tools/util_helpers.py", "build_pagination_metadata"),
        ],
    },
]


_KEYS_SECTION_RE = re.compile(
    r"(?:Available|History|Statistics)\s+keys:\s*([^.]+)\.",
    re.IGNORECASE,
)
_IDENT_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\b")
_PAREN_NOTE_RE = re.compile(r"\([^)]*\)")


def _read_module(rel_path: str) -> ast.Module:
    return ast.parse((SRC_ROOT / rel_path).read_text())


def _find_function(
    module: ast.Module, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(module):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise LookupError(f"function {name!r} not found")


def _parse_documented_keys(desc: str) -> set[str]:
    """Pull identifier tokens from any ``Available/History/Statistics keys: ...`` sentence."""
    keys: set[str] = set()
    for m in _KEYS_SECTION_RE.finditer(desc):
        chunk = _PAREN_NOTE_RE.sub("", m.group(1))
        for tok in _IDENT_RE.finditer(chunk):
            keys.add(tok.group(1))
    return keys


def _extract_documented_keys(rel_path: str, func_name: str) -> set[str]:
    module = _read_module(rel_path)
    func = _find_function(module, func_name)

    # Find the ``fields`` argument among regular + kwonly args.
    all_args = list(func.args.args) + list(func.args.kwonlyargs)
    for arg in all_args:
        if arg.arg != "fields" or arg.annotation is None:
            continue
        # Expect Annotated[Type, Field(description="...")]
        anno = arg.annotation
        if not isinstance(anno, ast.Subscript):
            continue
        slice_node = anno.slice
        elts: list[ast.expr]
        if isinstance(slice_node, ast.Tuple):
            elts = list(slice_node.elts)
        else:
            elts = [slice_node]
        for elt in elts:
            if (
                isinstance(elt, ast.Call)
                and isinstance(elt.func, ast.Name)
                and elt.func.id == "Field"
            ):
                for kw in elt.keywords:
                    if kw.arg != "description":
                        continue
                    # The description is often a parenthesised
                    # implicit-concatenated string literal — ast.literal_eval
                    # collapses that into one str.
                    try:
                        desc = ast.literal_eval(kw.value)
                    except ValueError:
                        continue
                    if isinstance(desc, str):
                        return _parse_documented_keys(desc)
    return set()


def _harvest_var_keys(rel_path: str, func_name: str, var_name: str) -> set[str]:
    """Top-level keys assigned to ``var_name`` inside ``func_name``."""
    module = _read_module(rel_path)
    try:
        func = _find_function(module, func_name)
    except LookupError:
        return set()
    keys: set[str] = set()

    for node in ast.walk(func):
        # var = {"k1": ..., "k2": ...}  or  var: Type = {...}
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            for tgt in targets:
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id == var_name
                    and isinstance(value, ast.Dict)
                ):
                    for k in value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == var_name
                    and isinstance(tgt.slice, ast.Constant)
                    and isinstance(tgt.slice.value, str)
                ):
                    keys.add(tgt.slice.value)

        # var.setdefault("k", ...)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == var_name
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)

        # var.update({"k": ...})
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == var_name
            and node.args
            and isinstance(node.args[0], ast.Dict)
        ):
            for k in node.args[0].keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)

    return keys


def _harvest_return_keys(rel_path: str, func_name: str) -> set[str]:
    """Top-level string-literal keys from every ``return {...}`` in the function."""
    module = _read_module(rel_path)
    try:
        func = _find_function(module, func_name)
    except LookupError:
        return set()
    keys: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return keys


def _harvest_marker_dicts(
    rel_path: str, func_name: str, markers: frozenset[str]
) -> set[str]:
    """Harvest top-level keys from every dict literal (assigned or returned)
    inside ``func_name`` whose key set intersects ``markers``.

    Use for tools whose response is built in many locally-named dicts
    (``area_search_data``, ``empty_area_data``, ``domain_listing``, ...) —
    too many to enumerate in ``var_harvest``. The marker set acts as a
    "this dict is response-shaped" filter.
    """
    module = _read_module(rel_path)
    try:
        func = _find_function(module, func_name)
    except LookupError:
        return set()
    keys: set[str] = set()

    def _collect(d: ast.Dict) -> None:
        this_keys = {
            k.value
            for k in d.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if this_keys & markers:
            keys.update(this_keys)

    for node in ast.walk(func):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Return)) and isinstance(
            node.value, ast.Dict
        ):
            _collect(node.value)
    return keys


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=lambda s: s["tool"])
def test_fields_description_lists_every_emitted_key(spec: dict[str, Any]) -> None:
    doc_module, doc_func = spec["docstring"]
    documented = _extract_documented_keys(doc_module, doc_func)
    assert documented, (
        f"{spec['tool']}: could not parse `Available keys:` enumeration from "
        f"the `fields=` Field description in {doc_module}:{doc_func}. The "
        f"docstring should contain a sentence like `Available keys: a, b, c.`"
    )

    # Manifest mode: the projectable keys come from an external contract
    # (e.g. HA's entity-record schema) that we can't AST-harvest from
    # source. Require docstring == manifest so any drift on either side is
    # caught.
    manifest = spec.get("documented_must_equal")
    if manifest is not None:
        assert documented == manifest, (
            f"{spec['tool']}: `fields=` `Available keys:` enumeration "
            f"({sorted(documented)!r}) drifted from the pinned manifest "
            f"({sorted(manifest)!r}). If HA's entity-state schema changed, "
            f"update both the docstring and the `documented_must_equal` "
            f"frozenset in this test."
        )
        return

    emitted: set[str] = set()
    for mod, fn, var in spec["var_harvest"]:
        emitted |= _harvest_var_keys(mod, fn, var)
    for mod, fn in spec["return_harvest"]:
        emitted |= _harvest_return_keys(mod, fn)
    for mod, fn, markers in spec.get("marker_harvest", []):
        emitted |= _harvest_marker_dicts(mod, fn, markers)
    emitted -= spec.get("exclude_internal", frozenset())

    assert emitted, (
        f"{spec['tool']}: harvested no response keys — check that the "
        f"var_harvest/return_harvest spec still matches the source."
    )

    errors: list[str] = []
    missing_from_docs = emitted - documented - _AUTO_RETAINED
    if missing_from_docs:
        errors.append(
            f"emitted but NOT enumerated in `Available keys:`: "
            f"{sorted(missing_from_docs)!r}. Add them to the docstring so "
            f"AI agents can project them via `fields=[...]`."
        )
    # Dual direction: documented keys must appear somewhere in the
    # scanned response builders. Catches "docstring lists key, code
    # deleted the assignment" — sibling drift to #1381's bug where
    # `notifications`/`repairs` were documented but only assigned
    # conditionally. (Static AST can flag "never assigned anywhere";
    # "assigned only inside `if x:`" requires runtime checking — see
    # PR #1381's `TestHaGetOverviewAlwaysEmittedKeys` for that pattern.)
    if not spec.get("skip_dual_check"):
        missing_from_code = documented - emitted - _AUTO_RETAINED
        if missing_from_code:
            errors.append(
                f"listed in `Available keys:` but NEVER assigned in any "
                f"scanned response builder: {sorted(missing_from_code)!r}. "
                f"Either remove from the docstring or extend the test's "
                f"var_harvest/return_harvest/marker_harvest spec if the "
                f"key is assigned in a helper that's not yet scanned."
            )

    assert not errors, f"{spec['tool']}: " + " | ".join(errors)
