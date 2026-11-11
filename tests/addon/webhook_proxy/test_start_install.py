"""How the app puts its bundled component into /config/custom_components, and
what it reports: a first install needs a Core restart to load, a new version
needs one to take effect, anything else needs none."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ._start import App, make_app
from ._variants import Variant


@pytest.fixture
def app(variant: Variant, fs_root: Path, monkeypatch: pytest.MonkeyPatch) -> App:
    return make_app(variant, fs_root, monkeypatch)


def _install(app: App) -> tuple[bool, bool]:
    first_install, version_changed = app.step("_install_integration")()
    return first_install, version_changed


def _manifest(component_dir: Path) -> Any:
    return json.loads((component_dir / "manifest.json").read_text())


def test_first_install_copies_the_component(app: App, variant: Variant) -> None:
    assert _install(app) == (True, False)

    assert _manifest(app.deployed_component) == _manifest(variant.component_dir)


def test_new_version_is_installed_and_reported(app: App, variant: Variant) -> None:
    app.deploy_component(version="0.0.1")

    assert _install(app) == (False, True)

    assert _manifest(app.deployed_component) == _manifest(variant.component_dir)


def test_same_version_is_left_alone(app: App) -> None:
    app.deploy_component()

    assert _install(app) == (False, False)


def test_corrupt_deployed_manifest_is_repaired_without_a_restart_prompt(
    app: App, variant: Variant
) -> None:
    """An unreadable deployed manifest says nothing about a version change, so
    the files are refreshed without asking the user to restart Core."""
    app.deploy_component()
    (app.deployed_component / "manifest.json").write_text("not json{{{")

    assert _install(app) == (False, False)

    assert _manifest(app.deployed_component) == _manifest(variant.component_dir)
