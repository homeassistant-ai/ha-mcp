"""Release ZIP layout, selection and release-stamp preservation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "build_component_zip", _ROOT / "scripts" / "build_component_zip.py"
)
assert _SPEC and _SPEC.loader
builder = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(builder)


@pytest.fixture
def component(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    folder = tmp_path / "custom_components" / "ha_mcp_tools"
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (folder / "manifest.json").write_text(
        json.dumps({"domain": "ha_mcp_tools", "version": "1.0.0", "requirements": []}),
        encoding="utf-8",
    )
    (folder / "translations").mkdir()
    (folder / "translations" / "en.json").write_text(
        '{"hello":"world"}', encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    return folder


def test_flat_layout_installs_into_a_custom_component_directory(
    component: Path,
) -> None:
    archive_path = builder.build_component_zip(
        component, component.parent / "release.zip"
    )
    installed = component.parent / "installed"
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            "__init__.py",
            "manifest.json",
            "translations/en.json",
        }
        archive.extractall(installed)
    assert (installed / "__init__.py").read_bytes() == (
        component / "__init__.py"
    ).read_bytes()
    assert (
        json.loads((installed / "manifest.json").read_text())["domain"]
        == "ha_mcp_tools"
    )


@pytest.mark.parametrize(
    "distribution,pin", [("ha-mcp", "1.0.1"), ("ha-mcp-dev", "1.1.0.dev42")]
)
def test_packages_working_tree_stamps_instead_of_the_index(
    component: Path, distribution: str, pin: str
) -> None:
    manifest_path = component / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(
        version=pin.split(".dev", maxsplit=1)[0],
        requirements=[f"{distribution}=={pin}"],
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with zipfile.ZipFile(
        builder.build_component_zip(component, component.parent / "release.zip")
    ) as archive:
        assert json.loads(archive.read("manifest.json")) == manifest


def test_untracked_files_and_bytecode_are_not_shipped(component: Path) -> None:
    (component / "local-secret.txt").write_text("not for release", encoding="utf-8")
    (component / "__pycache__").mkdir()
    bytecode = component / "__pycache__" / "module.pyc"
    bytecode.write_bytes(b"cache")
    subprocess.run(
        ["git", "-C", str(component), "add", "-f", str(bytecode)], check=True
    )
    with zipfile.ZipFile(
        builder.build_component_zip(component, component.parent / "release.zip")
    ) as archive:
        assert "local-secret.txt" not in archive.namelist()
        assert not any("__pycache__" in name for name in archive.namelist())


def test_file_timestamps_do_not_change_the_release_zip(component: Path) -> None:
    first = builder.build_component_zip(
        component, component.parent / "first.zip"
    ).read_bytes()
    path = component / "__init__.py"
    path.touch()
    second = builder.build_component_zip(
        component, component.parent / "second.zip"
    ).read_bytes()
    assert first == second


def test_verification_accepts_identical_files_in_a_different_zip_container(
    component: Path,
) -> None:
    original = builder.build_component_zip(component, component.parent / "first.zip")
    repacked = component.parent / "repacked.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(repacked, "w") as target:
        for name in reversed(source.namelist()):
            target.writestr(name, source.read(name))
    assert original.read_bytes() != repacked.read_bytes()
    assert (
        builder.verify_component_zip(component, repacked)
        == "sha256:" + hashlib.sha256(repacked.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "change", ["bytes", "missing", "extra", "duplicate", "symlink"]
)
def test_verification_refuses_a_zip_that_changes_the_installable_snapshot(
    component: Path, change: str
) -> None:
    original = builder.build_component_zip(component, component.parent / "first.zip")
    changed = component.parent / "changed.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(changed, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "__init__.py":
                if change == "missing":
                    continue
                if change == "bytes":
                    data = b"VALUE = 2\n"
                if change == "symlink":
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = 0o120777 << 16
                    target.writestr(info, data)
                    continue
            target.writestr(name, data)
        if change == "extra":
            target.writestr("untracked.py", b"unexpected")
        if change == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                target.writestr("__init__.py", source.read("__init__.py"))
    with pytest.raises(ValueError, match="ZIP"):
        builder.verify_component_zip(component, changed)


def test_missing_tracked_file_does_not_replace_a_previous_artifact(
    component: Path,
) -> None:
    output = component.parent / "release.zip"
    original = builder.build_component_zip(component, output).read_bytes()
    (component / "__init__.py").unlink()
    with pytest.raises(ValueError, match="Missing or linked"):
        builder.build_component_zip(component, output)
    assert output.read_bytes() == original


def test_a_tracked_symlink_is_refused_without_following_it(component: Path) -> None:
    # An index entry exercises this on Windows without requiring symlink privileges.
    blob = (
        subprocess.check_output(
            ["git", "-C", str(component), "hash-object", "-w", "--stdin"],
            input=b"../outside",
        )
        .decode()
        .strip()
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(component),
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{blob},custom_components/ha_mcp_tools/linked.py",
        ],
        check=True,
    )
    with pytest.raises(ValueError, match="Not a regular"):
        builder.build_component_zip(component, component.parent / "release.zip")


def test_a_different_integration_domain_is_not_published(component: Path) -> None:
    path = component / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["domain"] = "another_integration"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="domain and release version"):
        builder.build_component_zip(component, component.parent / "release.zip")
