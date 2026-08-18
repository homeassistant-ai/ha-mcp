"""The shipped English never sends a user down a menu path that is gone.

Home Assistant 2026.2 renamed the *Add-ons* panel to *Apps* and its store to
the *App store*. Every string this project ships to a user — the settings-UI
catalog, the integration's config-flow text, and the runtime hints in
``update_check`` and the screenshot provisioner — therefore has to name the
current label. The retired one may still appear, but only next to the current
one, as the note for installations older than 2026.2.

This is a sweep rather than a per-string pin: the strings move (a reword, a
new hint, a new deployment mode), and a pin only guards the wording it was
written for, while the class of fault — shipping a navigation nobody can
follow — is what the reader actually pays for.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent

# Surfaces a user reads, in the language they are authored in. Prose that no
# one navigates from (README, site, docs) is out: those are edited as a whole
# and carry the compat sentence in their own shape.
PYTHON_SURFACES = (
    REPO_ROOT / "src/ha_mcp/update_check.py",
    REPO_ROOT / "src/ha_mcp/dashboard_screenshot/provision.py",
)
JSON_SURFACES = (
    REPO_ROOT / "src/ha_mcp/settings_ui/locales/en.json",
    REPO_ROOT / "custom_components/ha_mcp_tools/strings.json",
    REPO_ROOT / "custom_components/ha_mcp_tools/translations/en.json",
)

# (what Home Assistant no longer shows, what it shows instead). A string may
# carry the retired form only if it also carries the current one.
RETIRED_WITH_CURRENT = (
    (
        re.compile(r"Settings\s*(?:->|→|>)\s*Add-ons"),
        re.compile(r"Settings\s*(?:->|→|>)\s*Apps"),
    ),
    (re.compile(r"[Aa]dd-on [Ss]tore"), re.compile(r"App store|Install app")),
)


def _python_strings(path: Path) -> list[str]:
    """Every shipped string literal in a module, f-strings flattened.

    Adjacent literals are already one constant after parsing, so a menu path
    split across source lines arrives whole — which is how it is read.
    Docstrings are skipped: they describe the Supervisor API to the next
    maintainer, where the slugs and endpoints really are still add-ons.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }

    strings: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            strings.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            strings.append(
                "".join(
                    part.value
                    for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            )
    return strings


def _json_strings(path: Path) -> list[str]:
    def walk(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [s for item in value.values() for s in walk(item)]
        if isinstance(value, list):
            return [s for item in value for s in walk(item)]
        return []

    return walk(json.loads(path.read_text(encoding="utf-8")))


def _surfaces() -> list[tuple[Path, list[str]]]:
    return [(path, _python_strings(path)) for path in PYTHON_SURFACES] + [
        (path, _json_strings(path)) for path in JSON_SURFACES
    ]


@pytest.mark.parametrize(
    ("retired", "current"),
    RETIRED_WITH_CURRENT,
    ids=["menu-path", "store-name"],
)
def test_a_retired_label_never_ships_without_the_current_one(
    retired: re.Pattern[str], current: re.Pattern[str]
) -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {text[:160]}"
        for path, strings in _surfaces()
        for text in strings
        if retired.search(text) and not current.search(text)
    ]

    assert not offenders, (
        f"{len(offenders)} shipped string(s) name {retired.pattern!r} with no "
        f"{current.pattern!r} beside it, so a user on Home Assistant 2026.2 or "
        "later is sent to a panel that no longer carries that name:\n  "
        + "\n  ".join(offenders)
    )


def test_the_sweep_reads_the_surfaces_it_claims_to() -> None:
    """Positive control: an empty extractor would pass every check above.

    Each surface has to yield strings, and the current labels have to be
    findable in them — otherwise the assertions above hold vacuously.
    """
    surfaces = _surfaces()
    empty = [
        str(path.relative_to(REPO_ROOT)) for path, strings in surfaces if not strings
    ]
    assert not empty, f"no strings extracted from {empty}"

    current_label = re.compile(r"Settings\s*(?:->|→|>)\s*Apps")
    with_current = [
        str(path.relative_to(REPO_ROOT))
        for path, strings in surfaces
        if any(current_label.search(text) for text in strings)
    ]
    assert with_current, (
        "no surface names the current menu path at all — the sweep would pass "
        "on a repository that had never been updated"
    )
