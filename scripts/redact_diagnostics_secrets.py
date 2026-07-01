#!/usr/bin/env python3
"""Redact credential values from HAOS diagnostics before artifact upload.

The HAOS e2e workflows tar ``.storage`` out of the booted qcow2 into a
diagnostics artifact. Since conftest injects ``GITHUB_TOKEN`` into the HACS
config entry pre-boot (see ``haos_runtime.inject_hacs_token_in_qcow2``),
``core.config_entries`` inside that tar carries the token — expired by the
time anyone can download the artifact (``GITHUB_TOKEN`` is revoked when the
run ends), but a credential must not persist into a downloadable artifact
regardless.

Walks every ``storage.tar`` under the given root and rewrites each in place
with credential-named values in ``core.config_entries`` replaced by
``**REDACTED**``. FAIL-CLOSED: if a tar can't be redacted (corrupt, parse
error), it is DELETED rather than uploaded unredacted, with a notice.

Usage: python3 scripts/redact_diagnostics_secrets.py <diagnostics-root-dir>
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

SECRET_KEYS = frozenset(
    {"token", "access_token", "refresh_token", "client_secret", "password", "api_key"}
)
CONFIG_ENTRIES_MEMBERS = ("./core.config_entries", "core.config_entries")
REDACTED = "**REDACTED**"


def redact_config_entries(doc: dict[str, Any]) -> int:
    """Replace credential-named values in every config entry's ``data``.

    Returns the number of values redacted. Only non-empty string values are
    replaced, so entry shapes (which keys exist) stay diagnosable.
    """
    redacted = 0
    for entry in doc.get("data", {}).get("entries", []):
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if key in SECRET_KEYS and isinstance(value, str) and value:
                data[key] = REDACTED
                redacted += 1
    return redacted


def redact_storage_tar(tar_path: Path) -> int:
    """Rewrite ``tar_path`` in place with ``core.config_entries`` redacted.

    Returns the number of values redacted (0 when the tar has no
    ``core.config_entries`` member or nothing to redact).
    """
    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    total = 0
    with tarfile.open(tar_path, "r") as tf:
        for info in tf.getmembers():
            payload: bytes | None = None
            if info.isfile():
                extracted = tf.extractfile(info)
                payload = extracted.read() if extracted is not None else b""
                if info.name in CONFIG_ENTRIES_MEMBERS:
                    doc = json.loads(payload.decode("utf-8"))
                    total += redact_config_entries(doc)
                    payload = json.dumps(doc, indent=2).encode("utf-8")
                    info.size = len(payload)
            members.append((info, payload))

    with tarfile.open(tar_path, "w") as tf:
        for info, payload in members:
            tf.addfile(info, io.BytesIO(payload) if payload is not None else None)
    return total


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(argv[1])
    if not root.is_dir():
        # Nothing extracted (diagnostics step failed earlier) — nothing to
        # redact, and nothing unredacted can be uploaded. Not an error.
        print(f"redact: {root} does not exist — nothing to do")
        return 0

    for tar_path in sorted(root.rglob("storage.tar")):
        try:
            n = redact_storage_tar(tar_path)
            print(f"redact: {tar_path} — {n} value(s) redacted")
        except Exception as exc:
            # Could not guarantee redaction — drop the tar so the artifact
            # cannot carry an unredacted credential. Diagnostics loss beats
            # credential persistence.
            tar_path.unlink(missing_ok=True)
            print(
                f"redact: FAILED for {tar_path} ({type(exc).__name__}: {exc}) — "
                "tar DELETED so the artifact can't carry unredacted credentials",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
