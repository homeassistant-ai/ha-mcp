"""``.github/ISSUE_TEMPLATE/*.yml`` must stay parseable and keep its shape.

Nothing in CI parses these files. GitHub's failure mode is silent: an issue
form that does not validate is dropped from the template picker and the
reporter gets a blank issue instead, with no error anywhere. ``config.yml`` is
the quietest of all — a malformed ``contact_links`` entry simply does not
render.

Same pattern as ``test_coderabbit_config.py``: pin a file CI never reads to the
shape the repo relies on.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_DIR = _REPO_ROOT / ".github" / "ISSUE_TEMPLATE"

# Every `body` entry type GitHub's issue-form schema accepts.
_BODY_TYPES = frozenset({"markdown", "input", "textarea", "dropdown", "checkboxes"})


def _template_paths() -> list[Path]:
    return sorted(p for p in _TEMPLATE_DIR.glob("*.yml") if p.name != "config.yml")


def _template_ids() -> list[str]:
    return [p.name for p in _template_paths()]


def test_every_template_file_parses() -> None:
    """A YAML error anywhere in the directory costs the whole picker entry."""
    for path in sorted(_TEMPLATE_DIR.glob("*.yml")):
        yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", _template_paths(), ids=_template_ids())
def test_form_carries_the_required_top_level_keys(path: Path) -> None:
    """GitHub requires name, description and body on an issue form."""
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: top level must be a mapping"
    for key in ("name", "description", "body"):
        assert data.get(key), f"{path.name}: missing or empty '{key}'"
    assert isinstance(data["body"], list) and data["body"], (
        f"{path.name}: 'body' must be a non-empty list"
    )


@pytest.mark.parametrize("path", _template_paths(), ids=_template_ids())
def test_every_body_entry_declares_a_valid_type(path: Path) -> None:
    """An unrecognised `type` invalidates the form silently."""
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    for index, entry in enumerate(data["body"]):
        assert isinstance(entry, dict), f"{path.name}: body[{index}] is not a mapping"
        kind = entry.get("type")
        assert kind in _BODY_TYPES, (
            f"{path.name}: body[{index}] has type {kind!r}, "
            f"not one of {sorted(_BODY_TYPES)}"
        )


@pytest.mark.parametrize("path", _template_paths(), ids=_template_ids())
def test_connection_preflight_never_becomes_a_required_field(path: Path) -> None:
    """The pre-flight checklist is guidance, not a gate.

    It carries a "Not applicable" option precisely so a reporter whose problem
    is not a connection problem can move on. Marking it required would force a
    wrong answer, and would block filing for anyone the checklist does not
    apply to.
    """
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in data["body"]:
        if entry.get("id") != "connect_preflight":
            continue
        assert entry["type"] == "checkboxes"
        options = entry["attributes"]["options"]
        assert any("Not applicable" in option["label"] for option in options), (
            f"{path.name}: the pre-flight block must keep an opt-out option"
        )
        for option in options:
            assert not option.get("required", False), (
                f"{path.name}: pre-flight options must not be required"
            )


def test_every_template_url_in_source_names_a_real_form() -> None:
    """Tie the ``?template=`` filenames in source to the files on disk.

    ``tools_bug_report.py`` builds its submission URLs from hard-coded form
    filenames. Renaming a form leaves every other test green — the ones above
    parametrize over a glob, and the code-side test pins the URL string — so
    the two halves never meet. The e2e classifier now skips the suite for this
    directory too, so nothing else would catch it either.
    """
    pattern = re.compile(r"issues/new\?template=([\w.-]+)")
    referenced: dict[str, Path] = {}
    for source in (_REPO_ROOT / "src").rglob("*.py"):
        if "_vendor" in source.parts:
            continue
        for name in pattern.findall(source.read_text(encoding="utf-8")):
            referenced.setdefault(name, source)
    assert referenced, "no ?template= URLs found in src/ — has the pattern moved?"
    for name, source in sorted(referenced.items()):
        assert (_TEMPLATE_DIR / name).is_file(), (
            f"{source.relative_to(_REPO_ROOT)} builds an issue URL for "
            f"{name!r}, which does not exist in .github/ISSUE_TEMPLATE/"
        )


def test_config_contact_links_are_well_formed() -> None:
    """Each contact link needs all three keys and an https URL to render."""
    config_path = _TEMPLATE_DIR / "config.yml"
    data: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    links = data.get("contact_links")
    assert isinstance(links, list) and links, "config.yml: no contact_links"
    for index, link in enumerate(links):
        assert isinstance(link, dict), f"contact_links[{index}] is not a mapping"
        for key in ("name", "url", "about"):
            assert link.get(key), f"contact_links[{index}]: missing '{key}'"
        assert str(link["url"]).startswith("https://"), (
            f"contact_links[{index}]: url must be https"
        )
