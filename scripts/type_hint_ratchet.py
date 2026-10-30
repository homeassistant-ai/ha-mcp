"""Keep the number of Python functions without type hints from growing.

``AGENTS.md`` asks for a type hint on every function signature. mypy enforces
that for the trees it checks, but not for the tests. A function counts as
unannotated when a parameter or the return type has no hint. As with mypy's
``disallow_untyped_defs``, the ``self`` or ``cls`` parameter of a
non-static method needs none, and neither does the return type of an
``__init__`` or ``__init_subclass__`` method with any hint. Lambdas are not
counted.

``tests/src/unit/test_type_hint_ratchet.py`` fails when a file not listed in
``type_hint_baseline.json`` has an unannotated function, or a listed file has
more than its entry. A file that has fewer passes, so a pull request need not
edit the baseline and two of them do not conflict over it. After a merge,
``.github/workflows/sync-ratchet-baselines.yml`` runs

    python scripts/type_hint_ratchet.py

on master and commits the lowered baseline. The command lowers or drops
entries. It never raises an entry and never adds a file, so it cannot accept
a new unannotated function: annotate it instead.

``--check`` reports violations without writing the baseline; the lefthook
pre-commit hook runs it with ``--staged`` to read the staged content. CI also
runs ``--base <ref>``: it fails when the baseline raises or adds an entry
compared with that commit's baseline, or when a file has more unannotated
functions than the baseline the sync would write from that commit allows.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import module_size_ratchet  # type: ignore[import-not-found]

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "tests" / "src" / "unit" / "type_hint_baseline.json"
BASELINE_NAME = BASELINE_PATH.relative_to(REPO_ROOT).as_posix()
REPIN_COMMAND = "python scripts/type_hint_ratchet.py"

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_IMPLICIT_NONE = ("__init__", "__init_subclass__")


def _is_staticmethod(decorator: ast.expr) -> bool:
    if isinstance(decorator, ast.Attribute):
        return decorator.attr == "staticmethod"
    return isinstance(decorator, ast.Name) and decorator.id == "staticmethod"


def _is_unannotated(
    function: ast.FunctionDef | ast.AsyncFunctionDef, is_method: bool
) -> bool:
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    static = any(map(_is_staticmethod, function.decorator_list))
    rest = [arg for arg in (args.vararg, args.kwarg) if arg is not None]
    checked = positional[1:] if is_method and not static else positional
    params = [*checked, *args.kwonlyargs, *rest]
    if any(param.annotation is None for param in params):
        return True
    if function.returns is not None:
        return False
    # mypy reads these methods as returning None once any parameter, even
    # self, has a hint.
    hinted = any(arg.annotation for arg in [*positional, *params])
    return not (is_method and function.name in _IMPLICIT_NONE and hinted)


def count_unannotated(source: bytes, path: str = "<unknown>") -> int:
    """Return how many functions in ``source`` lack a type hint on a
    parameter or the return type. Nested functions count too."""
    count = 0
    stack: list[tuple[ast.AST, bool]] = [(ast.parse(source, filename=path), False)]
    while stack:
        node, in_class = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCTIONS):
                count += _is_unannotated(child, in_class)
                stack.append((child, False))
            else:
                # A def under an `if` in a class body is still a method.
                stack.append((child, in_class or isinstance(child, ast.ClassDef)))
    return count


def measure(
    repo_root: Path, staged: bool = False, ref: str | None = None
) -> dict[str, int]:
    """Return the number of unannotated functions in every tracked Python
    file in scope."""
    sources = module_size_ratchet.read_sources(repo_root, staged, ref)
    return {
        path: count_unannotated(content, path)
        for path, content in sources.items()
        if path.endswith(".py")
    }


def lowered_baseline(
    counts: dict[str, int], baseline: dict[str, int]
) -> dict[str, int]:
    """Return the baseline with improved files lowered, and clean or deleted
    files dropped."""
    lowered: dict[str, int] = module_size_ratchet.lowered_baseline(counts, baseline, 0)
    return lowered


def find_violations(counts: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Return one message per file with more unannotated functions than its
    entry allows; a file with no entry allows none."""
    violations: list[str] = []
    for path, count in sorted(counts.items()):
        allowed = baseline.get(path, 0)
        if count <= allowed:
            continue
        if path in baseline:
            violations.append(
                f"{path}: functions without full type hints went from {allowed} "
                f"to {count}. Annotate the parameters and return type of the "
                "functions you added or changed."
            )
        else:
            violations.append(
                f"{path}: functions without full type hints went from 0 to "
                f"{count}, and a file not in {BASELINE_NAME} may have none. "
                "Annotate every parameter and the return type."
            )
    return violations


def find_growth(baseline: dict[str, int], base: dict[str, int]) -> list[str]:
    """Return one message per entry ``baseline`` adds or raises over ``base``."""
    messages: list[str] = []
    for path, allowed in sorted(baseline.items()):
        before = base.get(path)
        if before is not None and allowed <= before:
            continue
        was = "not listed" if before is None else f"at {before}"
        messages.append(
            f"{path}: {BASELINE_NAME} allows {allowed} unannotated functions, "
            f"but the base branch has it {was}. Only `{REPIN_COMMAND}` edits "
            "the baseline, and it never raises an entry: annotate the "
            "functions instead."
        )
    return messages


def find_growth_since(repo_root: Path, ref: str) -> list[str]:
    """Return one message per file with more unannotated functions than the
    baseline the sync would write from ``ref`` allows.

    Until the sync runs after a merge, ``ref``'s baseline can still hold an
    entry above its file's count. Checking against that entry would let a
    pull request add unannotated functions back under it.
    """
    base_baseline = module_size_ratchet.load_baseline(
        module_size_ratchet.read_text(repo_root, BASELINE_NAME, ref=ref)
    )
    synced = lowered_baseline(measure(repo_root, ref=ref), base_baseline)
    return find_violations(measure(repo_root), synced)


def main(argv: list[str] | None = None, repo_root: Path = REPO_ROOT) -> int:
    """With ``--base``, compare the baseline and the files with that commit.
    Otherwise lower the baseline (only measure it with ``--check``) and
    return 1 if a file has more unannotated functions than it allows."""
    args = module_size_ratchet.parse_args(argv, __doc__.splitlines()[0])
    if args.base:
        status: int = module_size_ratchet.compare_with_base(
            repo_root, args.base, BASELINE_NAME, find_growth, find_growth_since
        )
        return status
    # With --staged the baseline comes from the index too, so an unstaged
    # edit to it cannot hide a staged unannotated function.
    baseline = module_size_ratchet.load_baseline(
        module_size_ratchet.read_text(repo_root, BASELINE_NAME, args.staged)
    )
    counts = measure(repo_root, staged=args.staged)
    lowered = lowered_baseline(counts, baseline)
    if not args.check:
        (repo_root / BASELINE_NAME).write_text(
            json.dumps(lowered, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"{len(baseline) - len(lowered)} entries dropped, {len(lowered)} remain")
    status = module_size_ratchet.report(find_violations(counts, lowered))
    return status


if __name__ == "__main__":
    sys.exit(main())
