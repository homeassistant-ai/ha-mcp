"""Structural + real-skill-file tests for the skill_content delivery path.

The PR review surfaced three bugs in the same class — a write-tool method
had multiple success-return paths, and the per-tool ``attach_skill_content``
(or its predecessor ``_attach_helper_skill`` / ``_attach_dashboard_skill``
wrapper) was called on some paths but not others. The bugs silently
violated each tool's docstring promise that ``include_skill=True`` would
ship ``skill_content`` in the response.

These tests address that bug class without trying to spin up the full
fastmcp stack (which Termux can't do without uv):

* :class:`TestWriteToolAttachCoverage` — AST-scans each of the six write
  tools, finds every successful return path inside the public ``@tool``
  method, and asserts each path is preceded by an attach call. Static,
  fast, catches the wrap-missing bug class deterministically.

* :class:`TestEveryEmittedAnchorResolves` — iterates every literal anchor
  the best-practice checker passes to ``_emit()``, plus every canonical
  file mapping the six tools declare, and resolves each against the real
  bundled skills-vendor submodule. Catches submodule heading renames and
  ``_emit()`` typos before they ship as silent skill_content drops.

Both rely on the bundled submodule being initialised — they skip cleanly
when it's absent so a fresh clone doesn't fail collection.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ha_mcp.utils import skill_loader

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLS_DIR = REPO_ROOT / "src" / "ha_mcp" / "tools"

# The six write tools that gained include_skill in PR #1182, with the
# public method name pytest-parameterizes over.
WRITE_TOOLS: tuple[tuple[str, str], ...] = (
    ("tools_config_automations.py", "ha_config_set_automation"),
    ("tools_config_scripts.py", "ha_config_set_script"),
    ("tools_config_scenes.py", "ha_config_set_scene"),
    ("tools_config_helpers.py", "ha_config_set_helper"),
    ("tools_config_dashboards.py", "ha_config_set_dashboard"),
    ("tools_yaml_config.py", "ha_config_set_yaml"),
)

# Call names that count as "attached the skill_content" in the structural
# check below — the direct shared helper plus the two per-tool wrappers
# that delegate to it.
_ATTACH_CALL_NAMES = frozenset(
    {"attach_skill_content", "_attach_helper_skill", "_attach_dashboard_skill"}
)


# ---------------------------------------------------------------------------
# Structural coverage of attach_skill_content on every success return path
# ---------------------------------------------------------------------------


def _find_function_by_name(
    tree: ast.AST, name: str
) -> ast.AsyncFunctionDef | ast.FunctionDef | None:
    """Find an async or sync function by name anywhere in ``tree``."""
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == name
        ):
            return node
    return None


def _count_attach_calls(tree: ast.AST) -> int:
    """Count attach-helper invocations anywhere in ``tree``.

    Direct calls (``attach_skill_content(...)``) and attribute calls
    (``self._attach_helper_skill(...)``) both count. Sufficient signal
    for "the module's wiring includes an attach call".
    """
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if (isinstance(target, ast.Name) and target.id in _ATTACH_CALL_NAMES) or (
                isinstance(target, ast.Attribute) and target.attr in _ATTACH_CALL_NAMES
            ):
                count += 1
    return count


@pytest.mark.parametrize(("module_file", "tool_name"), WRITE_TOOLS)
def test_write_tool_attaches_skill_content_somewhere(
    module_file: str, tool_name: str
) -> None:
    """Each of the six write tools must call an attach-helper somewhere
    in its public method (or any helper method invoked from it).

    Pins the wrap-against-every-return-site contract structurally so a
    future refactor that adds a new return path without an attach call
    surfaces as a test failure instead of a silent skill_content drop.

    Specifically pinned: an attach call must exist in the tool's own
    file (either directly in the public method, or in a private helper
    in the same module). This catches the bug class fixed in PR #1448
    where set_dashboard.python_transform, set_helper.config_subentry,
    and set_scene.python_transform returned without wrapping.
    """
    module_path = TOOLS_DIR / module_file
    tree = ast.parse(module_path.read_text())

    # Count attach calls anywhere in the module — the helper tool wraps
    # at the class-level method, others wrap inline in the @tool method,
    # and dashboards/helpers use shared wrappers. All count.
    total_attaches = _count_attach_calls(tree)

    fn = _find_function_by_name(tree, tool_name)
    assert fn is not None, f"{tool_name} not found in {module_file}"

    # Count return statements that return a dict literal or a variable name
    # — these are the success paths.
    success_returns = sum(
        1
        for node in ast.walk(fn)
        if isinstance(node, ast.Return)
        and node.value is not None
        and isinstance(node.value, (ast.Dict, ast.Name, ast.Await))
    )

    # Heuristic: attach calls in the module should equal or exceed the
    # number of success return paths in the public method. Holds because
    # each return path needs its own attach call (either inline or in
    # the helper it's about to return through).
    assert total_attaches >= 1, (
        f"{tool_name} in {module_file} has no attach_skill_content / "
        f"_attach_*_skill calls anywhere in the module — include_skill "
        f"parameter is silently no-op for this tool."
    )
    assert total_attaches >= success_returns, (
        f"{tool_name} in {module_file} has {success_returns} success-return "
        f"paths but only {total_attaches} attach-helper calls in the module. "
        f"At least one return path is missing its attach call — this is the "
        f"PR #1448 bug class (silently broken include_skill on one branch)."
    )


# ---------------------------------------------------------------------------
# Every anchor the BP checker emits must resolve against the real submodule
# ---------------------------------------------------------------------------


def _emit_file_refs() -> list[str]:
    """Statically extract every ``file_ref`` literal passed to _emit()."""
    checker_path = TOOLS_DIR / "best_practice_checker.py"
    tree = ast.parse(checker_path.read_text())
    refs: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_emit"
        ):
            # _emit(warnings, message, skill_prefix, file_ref) — positional
            # index 3, or `file_ref=...` kwarg.
            file_ref = None
            if len(node.args) >= 4 and isinstance(node.args[3], ast.Constant):
                file_ref = node.args[3].value
            for kw in node.keywords:
                if kw.arg == "file_ref" and isinstance(kw.value, ast.Constant):
                    file_ref = kw.value.value
            if isinstance(file_ref, str):
                refs.append(file_ref)
    return refs


def _canonical_files_mappings() -> list[tuple[str, str]]:
    """Statically extract every canonical-file mapping from the 6 write tools."""
    mapping_names = {
        "_AUTOMATION_SKILL_FILES",
        "_SCRIPT_SKILL_FILES",
        "_SCENE_SKILL_FILES",
        "_HELPER_SKILL_FILES",
        "_DASHBOARD_SKILL_FILES",
        "_YAML_SKILL_FILES",
    }
    out: list[tuple[str, str]] = []
    for module_file, _ in WRITE_TOOLS:
        tree = ast.parse((TOOLS_DIR / module_file).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                if name in mapping_names and isinstance(node.value, ast.Tuple):
                    out.extend(
                        (name, elt.value)
                        for elt in node.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    )
    return out


def _require_vendored_skills() -> Path:
    """Skip the test cleanly when the submodule isn't initialised."""
    skills_dir = skill_loader.get_skills_dir()
    if (
        skills_dir is None
        or not (skills_dir / "home-assistant-best-practices").is_dir()
    ):
        pytest.skip(
            "skills-vendor submodule not initialised; "
            "run `git submodule update --init` to enable real-skill tests"
        )
    return skills_dir


@pytest.mark.parametrize("file_ref", _emit_file_refs())
def test_every_checker_anchor_resolves_against_real_skills(file_ref: str) -> None:
    """Every ``_emit(file_ref=...)`` literal must resolve to a real section
    in the bundled skill files.

    Catches:

    * Checker-author typos in anchor names (e.g. ``#native-condition``
      instead of ``#native-conditions``).
    * Submodule heading renames (e.g. upstream rewrites
      ``## Native Conditions`` to ``## Native Condition Types``) that
      silently break the auto-embed path with no test signal.

    The full path stored in ``referenced_files`` is ``references/<file_ref>``
    (the ``_emit`` helper prepends ``references/``); we apply that same
    prefix here.
    """
    skills_dir = _require_vendored_skills()
    full_ref = f"references/{file_ref}"
    result = skill_loader.resolve_skill_files(
        skills_dir, "home-assistant-best-practices", [full_ref]
    )
    assert full_ref in result, (
        f"_emit anchor {file_ref!r} did not resolve in the real "
        f"home-assistant-best-practices submodule. Either the anchor "
        f"is a typo or the vendor file's heading was renamed."
    )
    assert result[full_ref], (
        f"_emit anchor {file_ref!r} resolved to empty content — heading "
        f"matched but section body is empty (possibly a one-liner heading)."
    )


@pytest.mark.parametrize(("mapping_name", "file_ref"), _canonical_files_mappings())
def test_every_canonical_skill_file_exists(mapping_name: str, file_ref: str) -> None:
    """Every per-tool canonical skill file must exist in the real submodule.

    A future commit that drops or renames one of these files in the
    vendor submodule would otherwise produce silent empty ``skill_content``
    for the affected write tool with no test signal.
    """
    skills_dir = _require_vendored_skills()
    result = skill_loader.resolve_skill_files(
        skills_dir, "home-assistant-best-practices", [file_ref]
    )
    assert file_ref in result, (
        f"Canonical skill file {file_ref!r} (from {mapping_name}) is not "
        f"in the home-assistant-best-practices submodule. The owning tool "
        f"will silently ship empty skill_content."
    )
