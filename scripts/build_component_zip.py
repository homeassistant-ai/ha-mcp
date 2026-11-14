# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Package tracked working-tree component files as a flat HACS release ZIP."""

from __future__ import annotations

import argparse
import subprocess
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def build(component: Path, output: Path) -> None:
    entries = subprocess.check_output(
        ["git", "-C", str(component), "ls-files", "--stage", "-z", "--", "."]
    ).split(b"\0")
    files: list[tuple[str, int]] = []
    for entry in filter(None, entries):
        metadata, raw_name = entry.split(b"\t", 1)
        raw_mode, _, stage = metadata.split()
        if raw_mode not in {b"100644", b"100755"} or stage != b"0":
            raise ValueError("The integration must contain regular, resolved Git files")
        files.append((raw_name.decode("utf-8"), int(raw_mode, 8)))
    if not {"manifest.json", "__init__.py"}.issubset(name for name, _ in files):
        raise ValueError("The tracked integration lacks manifest.json or __init__.py")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, mode in files:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, (component / name).read_bytes())
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component-dir", type=Path, default=_ROOT / "custom_components/ha_mcp_tools"
    )
    parser.add_argument(
        "--output", type=Path, default=_ROOT / "dist/integration/ha_mcp_tools.zip"
    )
    args = parser.parse_args()
    build(args.component_dir, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
