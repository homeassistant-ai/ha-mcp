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

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_DIR = _REPO_ROOT / ".github" / "ISSUE_TEMPLATE"

# Every `body` entry type GitHub's issue-form schema accepts.
_BODY_TYPES = frozenset({"markdown", "input", "textarea", "dropdown", "checkboxes"})

# Text file types that can carry a ``?template=`` URL.
_URL_BEARING_SUFFIXES = frozenset({".py", ".md", ".yml", ".yaml", ".astro"})

# Directories the walk skips: build output and dependency trees, plus the two
# places a form name legitimately appears without being a live link — vendored
# upstream code, and the tests that pin these very URLs.
_PRUNED_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "_vendor",
        "build",
        "dist",
        "htmlcov",
        "local",
        "node_modules",
        "site-packages",
        "tests",
        "venv",
        "worktree",
    }
)


def _url_bearing_files() -> list[Path]:
    """Every file in the tree that could carry a ``?template=`` URL.

    Walks the filesystem rather than asking git: the CI runner's checkout is
    owned by a different user than the test process, so ``git ls-files`` exits
    128 there on a dubious-ownership refusal.
    """
    found: list[Path] = []
    for directory, subdirs, names in os.walk(_REPO_ROOT):
        subdirs[:] = [d for d in subdirs if d not in _PRUNED_DIRS]
        for name in names:
            path = Path(directory) / name
            if path.suffix in _URL_BEARING_SUFFIXES:
                found.append(path)
    return found


def _template_paths() -> list[Path]:
    return sorted(p for p in _TEMPLATE_DIR.glob("*.yml") if p.name != "config.yml")


def test_every_template_file_parses() -> None:
    """A YAML error drops that form from the picker, silently."""
    for path in sorted(_TEMPLATE_DIR.glob("*.yml")):
        yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", _template_paths(), ids=lambda p: p.name)
def test_form_carries_the_required_top_level_keys(path: Path) -> None:
    """GitHub requires name, description and body on an issue form."""
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: top level must be a mapping"
    for key in ("name", "description", "body"):
        assert data.get(key), f"{path.name}: missing or empty '{key}'"
    assert isinstance(data["body"], list) and data["body"], (
        f"{path.name}: 'body' must be a non-empty list"
    )


@pytest.mark.parametrize("path", _template_paths(), ids=lambda p: p.name)
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


@pytest.mark.parametrize("path", _template_paths(), ids=lambda p: p.name)
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
    """Tie every ``?template=`` filename in the repo to a file on disk.

    Form filenames are hard-coded in several places — ``tools_bug_report.py``
    builds its submission URLs from them, and ``notify-dev-channel.yml`` posts
    one into dev-channel issues automatically. Renaming or retiring a form
    leaves every other test green: the ones above parametrize over a glob, and
    the code-side test pins the URL string, so the two halves never meet.
    GitHub's failure is silent — an unknown ``?template=`` just hands the
    reporter the picker.

    Scanning only ``src/`` missed exactly that: three references to a
    ``bug_report.md`` that has not existed for some time. An allowlist of roots
    would have kept being one directory short, so this walks the tree
    instead — a link added under ``site/src/``, in an app directory, or in a
    root document is covered without editing this test.
    """
    pattern = re.compile(r"issues/new\?template=([\w.-]+)")
    referenced: dict[str, Path] = {}
    for source in _url_bearing_files():
        for name in pattern.findall(
            source.read_text(encoding="utf-8", errors="ignore")
        ):
            referenced.setdefault(name, source)
    assert referenced, "no ?template= URLs found at all — has the pattern moved?"
    for name, source in sorted(referenced.items()):
        assert (_TEMPLATE_DIR / name).is_file(), (
            f"{source.relative_to(_REPO_ROOT)} builds an issue URL for "
            f"{name!r}, which does not exist in .github/ISSUE_TEMPLATE/"
        )


def test_every_faq_link_targets_a_real_anchor() -> None:
    """Tie the intake forms' deep links to an id on the site FAQ page.

    The forms point reporters at one FAQ section, and that link is the reason
    this intake change exists. Nothing asserted it: rename the section and all
    three links land on the top of the FAQ with no error anywhere. Both ends of
    the coupling now skip the e2e lanes — ``.github/ISSUE_TEMPLATE/*.yml`` and
    ``site/*`` — so a site-only rename would break them with every check green.
    """
    faq = (_REPO_ROOT / "site" / "src" / "pages" / "faq.astro").read_text(
        encoding="utf-8"
    )
    ids = set(re.findall(r'id="([\w-]+)"', faq))
    fragments: dict[str, Path] = {}
    for path in sorted(_TEMPLATE_DIR.glob("*.yml")):
        for fragment in re.findall(
            r"/faq/?#([\w-]+)", path.read_text(encoding="utf-8")
        ):
            fragments.setdefault(fragment, path)
    assert fragments, "no /faq#... links found in the issue forms — regex stale?"
    for fragment, path in sorted(fragments.items()):
        assert fragment in ids, (
            f"{path.name} links to /faq/#{fragment}, which is not an id on "
            "site/src/pages/faq.astro"
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
