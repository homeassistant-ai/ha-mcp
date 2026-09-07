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
Renovate runs this script before committing a websockets pin update.
It downloads that version's sdist and refreshes the vendored tree;
``tests/src/unit/test_vendored_websockets.py`` fails whenever the
vendored copy drifts from the pin, so a renovate bump that forgets to
regenerate cannot merge.

Usage:
    python scripts/vendor_websockets.py
"""

from __future__ import annotations

import hashlib
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


MANIFEST_NAME = "MANIFEST.sha256"


def iter_vendored_files(tree: Path) -> list[Path]:
    """Every vendored SOURCE file except the manifest, in stable order.

    ``__pycache__`` is skipped: importing the vendored package writes
    bytecode into the tree, so counting it would make the manifest depend on
    whether anything had imported websockets yet.
    """
    return sorted(
        (
            p
            for p in tree.rglob("*")
            if p.is_file() and p.name != MANIFEST_NAME and "__pycache__" not in p.parts
        ),
        key=lambda p: p.relative_to(tree).as_posix(),
    )


def manifest_lines(tree: Path) -> list[str]:
    """Return ``<sha256>  <relative path>`` for every vendored file.

    Recorded at sync time and re-verified by the drift guard, so a
    truncated sync or a hand-edit is caught — checking the version alone
    cannot see either, since both leave version.py intact.
    """
    return [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
        f"{path.relative_to(tree).as_posix()}"
        for path in iter_vendored_files(tree)
    ]


def _write_manifest(tree: Path) -> None:
    # newline="\n" is load-bearing: text mode translates to CRLF on a
    # Windows checkout while git stores LF, so a manifest written here
    # would not match the same tree on a Linux CI runner (and the file
    # hashes itself into the next sync's manifest).
    (tree / MANIFEST_NAME).write_text(
        "\n".join(manifest_lines(tree)) + "\n", encoding="utf-8", newline="\n"
    )


def read_pin() -> str:
    for raw_line in _PIN_FILE.read_text(encoding="utf-8").splitlines():
        if match := re.fullmatch(r"websockets==([A-Za-z0-9.!+-]+)", raw_line.strip()):
            return match.group(1)
    raise SystemExit(f"no 'websockets==<version>' pin found in {_PIN_FILE}")


def _stage_sdist(payload: bytes, version: str, staging: Path) -> int:
    """Extract the sdist's pure-Python sources into ``staging``.

    Returns the number of package files written (the LICENSE is copied too
    but is not part of the count).
    """
    prefix = f"websockets-{version}/src/websockets/"
    license_name = f"websockets-{version}/LICENSE"
    extracted = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name == license_name:
                staging.mkdir(parents=True, exist_ok=True)
                extract = tar.extractfile(member)
                assert extract is not None
                (staging / "LICENSE").write_bytes(extract.read())
                continue
            if not member.name.startswith(prefix) or not member.isfile():
                continue
            relative = member.name[len(prefix) :]
            # Pure-Python only: the optional C accelerator (speedups.c) is
            # deliberately left out — websockets falls back to its Python
            # implementation when the extension is absent.
            # The .pyi stub goes with it: shipping a stub for a module this
            # package does not contain would advertise it to type checkers
            # following py.typed.
            if relative.endswith((".c", ".so", ".pyd", "speedups.pyi")):
                continue
            destination = (staging / relative).resolve()
            # Containment check (CWE-22): member names come from the
            # downloaded archive, so a crafted ``../`` inside the matched
            # prefix must never write outside the vendor dir.
            if not destination.is_relative_to(staging.resolve()):
                raise SystemExit(
                    f"refusing archive member escaping the vendor dir: {member.name}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            extract = tar.extractfile(member)
            assert extract is not None
            destination.write_bytes(extract.read())
            extracted += 1
    return extracted


def _promote(staging: Path) -> None:
    """Swap the staged tree into place, keeping the live one until it lands.

    The live tree is moved ASIDE rather than deleted first: deleting it up
    front means a failed promotion (a permission error, an open handle on
    Windows) leaves the repository with no vendored websockets at all — and
    the package imports only the vendored copy, so that is an unimportable
    checkout whose recovery is a git command the failure never mentions.
    """
    backup = _TARGET.with_name(_TARGET.name + ".previous")
    shutil.rmtree(backup, ignore_errors=True)
    had_previous = _TARGET.exists()
    if had_previous:
        _TARGET.rename(backup)
    try:
        staging.rename(_TARGET)
    except BaseException:
        if had_previous:
            backup.rename(_TARGET)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def main() -> int:
    version = read_pin()
    url = _SDIST_URL.format(version=version)
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as response:
        payload: bytes = response.read()

    # Extract into a staging dir and swap at the end: extracting over the
    # live tree would leave a half-populated vendored package behind on any
    # mid-loop failure (a rejected traversal member, a write error), and the
    # only recovery would be a git checkout the failure never mentions.
    staging = _TARGET.with_name(_TARGET.name + ".incoming")
    if staging.exists():
        shutil.rmtree(staging)

    try:
        extracted = _stage_sdist(payload, version, staging)
        if not (staging / "__init__.py").is_file():
            raise SystemExit("sdist layout unexpected: no websockets/__init__.py")
        (staging / "VENDORED").write_text(
            f"websockets=={version}\n"
            "Vendored by scripts/vendor_websockets.py — do not edit by hand.\n",
            encoding="utf-8",
            newline="\n",
        )
        _write_manifest(staging)
    except BaseException:
        # Never leave a half-written staging tree behind; the live vendored
        # package has not been touched at this point.
        shutil.rmtree(staging, ignore_errors=True)
        raise

    _promote(staging)
    print(f"vendored websockets=={version}: {extracted} files -> {_TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
