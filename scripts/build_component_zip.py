# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Build the flat integration ZIP consumed by HACS and the Marketplace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

COMPONENT_DOMAIN = "ha_mcp_tools"
ASSET_NAME = f"{COMPONENT_DOMAIN}.zip"
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def _tracked_files(component: Path) -> list[tuple[str, Path]]:
    """Select tracked working-tree bytes, including uncommitted release stamps."""
    records = subprocess.check_output(
        ["git", "-C", str(component), "ls-files", "--stage", "-z", "--", "."]
    )
    files: list[tuple[str, Path]] = []
    for record in records.split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, _, stage = metadata.split()
        name = raw_name.decode("utf-8")
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe component path: {name}")
        if mode not in (b"100644", b"100755") or stage != b"0":
            raise ValueError(f"Not a regular, unconflicted component file: {name}")
        if "__pycache__" in relative.parts or relative.suffix in (".pyc", ".pyo"):
            continue
        path = component.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or linked component file: {name}")
        if not path.resolve().is_relative_to(component):
            raise ValueError(f"Component path leaves its directory: {name}")
        files.append((name, path))
    return sorted(files)


def _validated_component_files(component_dir: Path) -> list[tuple[str, Path]]:
    component = component_dir.resolve(strict=True)
    files = _tracked_files(component)
    names = {name for name, _ in files}
    if not {"__init__.py", "manifest.json"} <= names:
        raise ValueError("The tracked integration needs __init__.py and manifest.json")
    manifest = json.loads((component / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("domain") != COMPONENT_DOMAIN or not manifest.get("version"):
        raise ValueError(
            "The integration manifest needs its domain and release version"
        )
    return files


def verify_component_zip(component_dir: Path, archive_path: Path) -> str:
    """Bind an upstream artifact to the exact tracked component bytes."""
    expected = dict(_validated_component_files(component_dir))
    with archive_path.open("rb") as source:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) != len(expected) or {i.filename for i in entries} != set(
                expected
            ):
                raise ValueError("ZIP members differ from the component snapshot")
            for info in entries:
                mode = stat.S_IFMT(info.external_attr >> 16)
                path = expected[info.filename]
                if mode not in (0, stat.S_IFREG) or info.is_dir():
                    raise ValueError(f"Not a regular ZIP member: {info.filename}")
                if (
                    info.file_size != path.stat().st_size
                    or archive.read(info) != path.read_bytes()
                ):
                    raise ValueError(
                        f"ZIP bytes differ from the component snapshot: {info.filename}"
                    )
        source.seek(0)
        return "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()


def build_component_zip(component_dir: Path, output: Path) -> Path:
    """Package the integration's files directly at ZIP root, without repo files."""
    files = _validated_component_files(component_dir)

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output.parent, suffix=".zip", delete=False
    ) as f:
        temporary = Path(f.name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, path in files:
                info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "custom_components"
        / COMPONENT_DOMAIN,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--output", type=Path, default=Path("dist") / ASSET_NAME)
    mode.add_argument(
        "--verify", type=Path, help="Verify an existing ZIP against the component"
    )
    args = parser.parse_args()
    if args.verify:
        print(verify_component_zip(args.component_dir, args.verify))
        return
    path = build_component_zip(args.component_dir, args.output)
    print(f"Built {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
