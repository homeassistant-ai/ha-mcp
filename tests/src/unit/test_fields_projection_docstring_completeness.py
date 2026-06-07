"""Static regression: every top-level key a tool can emit on its response
must be enumerated in the tool's ``fields=`` parameter description so AI
agents know it can be projected via ``fields=[...]``.

The check is purely static. For each tool with a ``fields=`` parameter
we AST-walk the function(s) that build the projectable dict, collect
every string-literal key that gets assigned at the top level, and
assert that set is documented in both directions:

- ``emitted ⊄ documented`` — code emits a key the docstring doesn't list,
  so AI agents reading the description never learn it can be requested.
- ``documented ⊄ emitted-anywhere-in-scanned-source`` — the docstring
  promises a key that's no longer assigned (e.g. an assignment was
  removed in a refactor but the enumeration wasn't updated). Static AST
  flags "never assigned anywhere"; conditional-only assignment requires
  runtime checking.
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
            (
                "tools/smart_search/_overview.py",
                "_assemble_overview_response",
                "base_response",
            ),
        ],
        "return_harvest": [],
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
        "tool": "ha_list_floors_areas",
        "docstring": ("tools/tools_areas.py", "ha_list_floors_areas"),
        "var_harvest": [
            ("tools/tools_areas.py", "ha_list_floors_areas", "response"),
        ],
        "return_harvest": [],
    },
    {
        # ha_search's ``fields=`` projects the top-level response shape —
        # the always-keep diagnostic / pagination set plus the per-surface
        # bucket keys. The set is well-defined by the orchestrator's
        # response-init dict literal, the four per-surface pagination
        # assignments in the merge loop, the dashboards opt-in bucket
        # from ``_CONFIG_BUCKETS``, and ``partial_reason`` set via the
        # merge accumulator + ``_apply_*_partial_flag`` helpers. Use a
        # ``documented_must_equal`` manifest so any future drift on
        # either side (new key emitted but not enumerated, or vice
        # versa) surfaces as a single clear error rather than a partial-
        # var-harvest miss.
        "tool": "ha_search",
        "docstring": ("tools/tools_search.py", "ha_search"),
        "var_harvest": [],
        "return_harvest": [],
        "documented_must_equal": frozenset(
            {
                "success",
                "query",
                "search_types",
                "entities",
                "automations",
                "scripts",
                "scenes",
                "helpers",
                "dashboards",
                "entity_total_matches",
                "config_total_matches",
                "count",
                "offset",
                "limit",
                "has_more",
                "next_offset",
                "entity_has_more",
                "entity_next_offset",
                "config_has_more",
                "config_next_offset",
                # Toggle-gated entity-branch feature output — kept at top
                # level + in ``_ALWAYS_KEEP_PROJECTION`` so a caller using
                # ``group_by_domain=True`` can pair it with ``fields=``.
                "by_domain",
                # Conditional diagnostic (fuzzy + state_filter) — kept so
                # callers projecting via ``fields=`` still get the dual-
                # count explanation.
                "state_filter_note",
                # Resolved area names (fuzzy `area_filter` may match
                # multiple areas) — caller value beyond the input echo.
                "area_names",
                # Entity-branch internal mode label — kept (E2E suite pins
                # at 17+ assertion sites, callers rely on it to identify
                # which entity-search path produced the result).
                "search_type",
                # Caller-input echoes — kept (E2E suite pins, so callers
                # actually read them back).
                "domain_filter",
                "area_filter",
                # Conditional zero-result diagnostic — kept (E2E suite
                # pins at 2 sites).
                "message",
                "warnings",
                "errors",
                "partial",
                "partial_reason",
            }
        ),
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
    """Pull identifier tokens from any ``Available/History/Statistics keys: ...`` sentence.

    Parenthesised notes are stripped before the section regex runs so a
    period inside a note (e.g. ``foo (since v1.2)``) doesn't truncate the
    enumeration.
    """
    desc = _PAREN_NOTE_RE.sub("", desc)
    keys: set[str] = set()
    for m in _KEYS_SECTION_RE.finditer(desc):
        for tok in _IDENT_RE.finditer(m.group(1)):
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

        # var["k"] += ...  (covers ``warnings`` accumulation and similar
        # augmented writes that AST splits off from the plain ``Assign``
        # node).
        if (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Subscript)
            and isinstance(node.target.value, ast.Name)
            and node.target.value.id == var_name
            and isinstance(node.target.slice, ast.Constant)
            and isinstance(node.target.slice.value, str)
        ):
            keys.add(node.target.slice.value)

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
    """Two-pass harvest of every key landing on a response-shaped dict.

    Use for tools whose response is built in many locally-named dicts
    (``area_search_data``, ``empty_area_data``, ``domain_list_data`` …) —
    too many to enumerate in ``var_harvest``. The marker set acts as a
    "this dict is response-shaped" filter.

    Pass 1 — flag a var as response-shaped via *either* signal:

    - It is initialised as a dict literal whose key set intersects ``markers``
      (catches ``search_data = {"results": ..., "total_matches": ...}``).
    - A subscript assignment writes a marker key to it (catches ``result =
      await smart_entity_search(...)`` followed by ``result["results"] =
      result.pop("matches")`` — the init isn't a literal, but the post-init
      marker-key write identifies the var as response-shaped).

    Pass 2 — for every flagged var, run ``_harvest_var_keys`` to collect
    subscript / ``setdefault`` / ``update`` assignments. Literal keys from
    Pass 1 are unioned in.
    """
    module = _read_module(rel_path)
    try:
        func = _find_function(module, func_name)
    except LookupError:
        return set()

    keys: set[str] = set()
    marker_vars: set[str] = set()

    def _literal_keys(d: ast.Dict) -> set[str]:
        return {
            k.value
            for k in d.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }

    for node in ast.walk(func):
        # Pass 1a — marker-bearing dict literal assigned or returned.
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
            node.value, ast.Dict
        ):
            literal = _literal_keys(node.value)
            if literal & markers:
                keys.update(literal)
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for tgt in targets:
                    if isinstance(tgt, ast.Name):
                        marker_vars.add(tgt.id)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            literal = _literal_keys(node.value)
            if literal & markers:
                keys.update(literal)

        # Pass 1b — subscript write of a marker key surfaces the var as
        # response-shaped even when its init wasn't a literal. Both plain
        # `var["k"] = ...` and augmented `var["k"] += ...` flag the var.
        subscript_targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            subscript_targets.extend(node.targets)
        elif isinstance(node, ast.AugAssign):
            subscript_targets.append(node.target)
        for tgt in subscript_targets:
            if (
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.value, ast.Name)
                and isinstance(tgt.slice, ast.Constant)
                and isinstance(tgt.slice.value, str)
                and tgt.slice.value in markers
            ):
                marker_vars.add(tgt.value.id)

    # Pass 2 — pick up subscript / setdefault / update on every flagged var.
    for var in marker_vars:
        keys |= _harvest_var_keys(rel_path, func_name, var)

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
    # deleted the assignment". Static AST flags "never assigned
    # anywhere"; "assigned only inside `if x:`" (conditional emission
    # of a documented-as-always-present key) is a runtime concern.
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


# ---------------------------------------------------------------------------
# Meta-tests: pin the scanner's own invariants so they can't silently drift.
# ---------------------------------------------------------------------------


def test_entities_branch_emissions_are_either_stripped_or_documented() -> None:
    """Closes the structural blindness called out in PR #1529 round-3 hygiene #4
    and tightened in round 4 to also see subscript-assigned keys.

    The parametrized ``ha_search`` drift-check runs in manifest mode and
    is structurally blind to keys the entities branch emits but that
    ``_merge_payload_metadata`` then propagates into the orchestrator
    response. A new key added to ``ha_search_entities``'s sub-payload
    builders would silently land at the top level of ``ha_search`` and
    pass the manifest check (the manifest is hand-maintained, doesn't
    re-derive).

    Harvest via ``_harvest_marker_dicts`` (literal + subscript +
    ``setdefault`` + ``update`` on any marker-flagged var) so the post-init
    ``result["by_domain"] = ...`` / ``result.setdefault("offset", ...)``
    style is covered alongside the dict-literal shape. Round-3 caught only
    the literals; round-4 closes the dominant emission style.

    Require every emitted key to be either:

    - in ``_ENTITIES_BRANCH_SKIP_KEYS`` (intentionally stripped by the
      orchestrator), OR
    - in the ``documented_must_equal`` manifest for ``ha_search``
      (intentionally surfaced at the top level + listed in the
      ``fields=`` ``Available keys`` enumeration + retained in
      ``_ALWAYS_KEEP_PROJECTION``).

    Adding a new key to either bucket forces the contract decision
    explicitly; landing in neither raises this test.
    """
    from ha_mcp.tools.tools_search import _ENTITIES_BRANCH_SKIP_KEYS

    markers = frozenset({"results", "total_matches"})
    emitted = _harvest_marker_dicts(
        "tools/tools_search.py", "ha_search_entities", markers
    )
    assert emitted, (
        "harvested no response-shaped dicts from ha_search_entities — the "
        "marker set ({'results', 'total_matches'}) may need updating"
    )

    ha_search_spec = next(s for s in TOOL_SPECS if s["tool"] == "ha_search")
    documented = ha_search_spec["documented_must_equal"]
    stripped = set(_ENTITIES_BRANCH_SKIP_KEYS)

    uncategorised = emitted - stripped - documented - _AUTO_RETAINED
    assert not uncategorised, (
        f"ha_search_entities emits {sorted(uncategorised)!r} into its "
        f"sub-payload, but the orchestrator neither strips them (not in "
        f"_ENTITIES_BRANCH_SKIP_KEYS) nor documents them (not in the "
        f"`Available keys:` enumeration / documented_must_equal manifest). "
        f"Either add to _ENTITIES_BRANCH_SKIP_KEYS to strip, or add to the "
        f"manifest + docstring + _ALWAYS_KEEP_PROJECTION to surface."
    )


def test_harvester_finds_dismissed_repair_count_in_ha_get_overview() -> None:
    """The bug this whole test file exists to prevent.

    ``dismissed_repair_count`` is conditionally assigned to ``result`` inside
    ``ha_get_overview``. If the AST harvest ever stops finding it (e.g. a
    refactor moves the assignment into a helper not listed in the spec, or
    the harvester loses subscript-assignment handling), the parametrized
    case for ``ha_get_overview`` would still pass — both sides of the diff
    would shrink in lockstep. Pin the find here so regressions surface.
    """
    keys = _harvest_var_keys("tools/tools_search.py", "ha_get_overview", "result")
    assert "dismissed_repair_count" in keys, (
        "AST harvest of `ha_get_overview` lost `dismissed_repair_count`. "
        "The regression-catch guarantee this test file provides is broken."
    )
    documented = _extract_documented_keys("tools/tools_search.py", "ha_get_overview")
    assert "dismissed_repair_count" in documented, (
        "`dismissed_repair_count` was removed from `ha_get_overview`'s "
        "`Available keys:` enumeration. If the response key was genuinely "
        "removed, update this meta-test too; otherwise restore the docstring."
    )


def test_tool_specs_covers_every_fields_using_tool() -> None:
    """Discover every tool with a ``fields`` parameter and assert TOOL_SPECS
    enumerates them all.

    Without this guard, deleting a TOOL_SPECS entry shrinks the
    parametrize silently — the remaining cases still pass and the dropped
    tool gets no drift coverage.
    """
    tools_dir = SRC_ROOT / "tools"
    discovered: set[str] = set()
    for path in sorted(tools_dir.glob("tools_*.py")):
        module = ast.parse(path.read_text())
        for node in ast.walk(module):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("ha_"):
                continue
            for arg in (*node.args.args, *node.args.kwonlyargs):
                if arg.arg == "fields":
                    discovered.add(node.name)
                    break

    covered = {spec["tool"] for spec in TOOL_SPECS}
    missing = discovered - covered
    assert not missing, (
        f"Tools with a `fields=` parameter are missing from TOOL_SPECS: "
        f"{sorted(missing)!r}. Add a spec entry so the drift check covers "
        f"them, or document why the tool is intentionally excluded."
    )


@pytest.mark.parametrize(
    "spec",
    [s for s in TOOL_SPECS if s.get("exclude_internal")],
    ids=lambda s: s["tool"],
)
def test_exclude_internal_keys_actually_appear_in_raw_harvest(
    spec: dict[str, Any],
) -> None:
    """``exclude_internal`` entries should still be present in the raw
    AST harvest. If a key listed there stops being emitted (e.g. the
    helper rename it documents was undone, or the internal field was
    deleted), the exclusion is dead code — silently masking nothing.
    """
    raw: set[str] = set()
    for mod, fn, var in spec["var_harvest"]:
        raw |= _harvest_var_keys(mod, fn, var)
    for mod, fn in spec["return_harvest"]:
        raw |= _harvest_return_keys(mod, fn)
    for mod, fn, markers in spec.get("marker_harvest", []):
        raw |= _harvest_marker_dicts(mod, fn, markers)

    dead = spec["exclude_internal"] - raw
    assert not dead, (
        f"{spec['tool']}: `exclude_internal` lists key(s) {sorted(dead)!r} "
        f"that no longer appear in the AST harvest — the exclusion is "
        f"masking nothing. Either remove from `exclude_internal` or "
        f"investigate whether the rename it documents was undone."
    )
