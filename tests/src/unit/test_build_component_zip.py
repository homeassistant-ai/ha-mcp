"""Release archives install the tracked, stamped component without extra files."""

import importlib.util
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "component_zip", _ROOT / "scripts/build_component_zip.py"
)
assert _SPEC and _SPEC.loader
builder = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(builder)


@pytest.fixture
def tracked_component(tmp_path):
    component = tmp_path / "repo/custom_components/ha_mcp_tools"
    component.mkdir(parents=True)
    (component / "__init__.py").write_text("VALUE = 1\n")
    (component / "manifest.json").write_text(
        json.dumps({"domain": "ha_mcp_tools", "version": "1.0.0"})
    )
    for command in (["init", "-q"], ["add", "."]):
        subprocess.run(["git", "-C", str(tmp_path / "repo"), *command], check=True)
    return component


def test_zip_installs_stamped_working_tree_without_untracked_files(
    tracked_component, tmp_path
):
    component = tracked_component
    (component / "manifest.json").write_text(
        json.dumps({"domain": "ha_mcp_tools", "version": "1.0.1"})
    )
    (component / "private.txt").write_text("not part of the integration")
    output = tmp_path / "dist/ha_mcp_tools.zip"
    builder.build(component, output)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"manifest.json", "__init__.py"}
        assert json.loads(archive.read("manifest.json"))["version"] == "1.0.1"
        archive.extractall(tmp_path / "installed/ha_mcp_tools")
    assert (tmp_path / "installed/ha_mcp_tools/__init__.py").read_bytes() == (
        component / "__init__.py"
    ).read_bytes()


def test_checkout_timestamps_do_not_change_the_zip(tracked_component, tmp_path):
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    builder.build(tracked_component, first)
    for path in tracked_component.iterdir():
        os.utime(path, (1_600_000_000, 1_600_000_000))
    builder.build(tracked_component, second)
    assert first.read_bytes() == second.read_bytes()


def test_missing_tracked_file_keeps_an_existing_archive(tracked_component, tmp_path):
    output = tmp_path / "release.zip"
    output.write_bytes(b"previous release")
    (tracked_component / "__init__.py").unlink()
    with pytest.raises(FileNotFoundError):
        builder.build(tracked_component, output)
    assert output.read_bytes() == b"previous release"


def test_non_regular_git_entry_is_refused(tracked_component, tmp_path):
    subprocess.run(
        [
            "git",
            "-C",
            str(tracked_component.parents[1]),
            "update-index",
            "--add",
            "--cacheinfo",
            "120000,e69de29bb2d1d6434b8b29ae775ad8c2e48c5391,custom_components/ha_mcp_tools/linked.py",
        ],
        check=True,
    )
    with pytest.raises(ValueError, match="regular"):
        builder.build(tracked_component, tmp_path / "release.zip")
