"""Sync the vendored websockets copy from the pinned version.

ha-mcp ships its own private copy of the ``websockets`` library under
``ha_mcp._vendor.websockets`` and imports only that copy. Rationale
(issues #2135/#2146): the shared site-packages copy inside Home Assistant
is nobody's property — ~20 integration libraries (ring-doorbell,
samsungtvws, homematicip, google-genai, ...) each drag it in with
mutually conflicting version demands, HA itself never declares it, and
any of those installs can replace or tear it in place at any time. A
vendored copy is immune to all of it, and CI always tests exactly the
version production runs.

The pin lives in ``src/ha_mcp/_vendor/requirements.txt`` (a standard
requirements file so renovate bumps it and dependency scanners read it).
This script downloads that version's sdist and refreshes the vendored
tree; ``tests/src/unit/test_vendored_websockets.py`` fails whenever the
vendored copy drifts from the pin, so a renovate bump that forgets to
regenerate cannot merge.

Usage:
    python scripts/vendor_websockets.py
"""

from __future__ import annotations

import io
import re
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VENDOR_DIR = _REPO_ROOT / "src" / "ha_mcp" / "_vendor"
_PIN_FILE = _VENDOR_DIR / "requirements.txt"
_TARGET = _VENDOR_DIR / "websockets"
_SDIST_URL = "https://pypi.org/packages/source/w/websockets/websockets-{version}.tar.gz"


def read_pin() -> str:
    for raw_line in _PIN_FILE.read_text(encoding="utf-8").splitlines():
        if match := re.fullmatch(r"websockets==([A-Za-z0-9.!+-]+)", raw_line.strip()):
            return match.group(1)
    raise SystemExit(f"no 'websockets==<version>' pin found in {_PIN_FILE}")


def main() -> int:
    version = read_pin()
    url = _SDIST_URL.format(version=version)
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as response:
        payload: bytes = response.read()

    if _TARGET.exists():
        shutil.rmtree(_TARGET)

    prefix = f"websockets-{version}/src/websockets/"
    license_name = f"websockets-{version}/LICENSE"
    extracted = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name == license_name:
                _TARGET.mkdir(parents=True, exist_ok=True)
                extract = tar.extractfile(member)
                assert extract is not None
                (_TARGET / "LICENSE").write_bytes(extract.read())
                continue
            if not member.name.startswith(prefix) or not member.isfile():
                continue
            relative = member.name[len(prefix) :]
            # Pure-Python only: the optional C accelerator (speedups.c) is
            # deliberately left out — websockets falls back to its Python
            # implementation when the extension is absent.
            if relative.endswith((".c", ".so", ".pyd")):
                continue
            destination = _TARGET / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            extract = tar.extractfile(member)
            assert extract is not None
            destination.write_bytes(extract.read())
            extracted += 1

    if not (_TARGET / "__init__.py").is_file():
        raise SystemExit("sdist layout unexpected: no websockets/__init__.py")
    (_TARGET / "VENDORED").write_text(
        f"websockets=={version}\n"
        "Vendored by scripts/vendor_websockets.py — do not edit by hand.\n",
        encoding="utf-8",
    )
    print(f"vendored websockets=={version}: {extracted} files -> {_TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
