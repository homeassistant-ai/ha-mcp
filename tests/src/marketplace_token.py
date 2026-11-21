"""The CI GitHub token for the E2E Marketplace, written before Home Assistant boots.

Stdlib-only, like ``haos_runtime``, so it loads by file path anywhere.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)


def inject_marketplace_token(config_entries_doc: dict[str, Any], token: str) -> bool:
    """Set ``token`` as the Marketplace config entry's GitHub token.

    Mutates ``config_entries_doc`` (a parsed ``.storage/core.config_entries``
    document) in place, setting ``token`` in the ``marketplace`` entry's
    ``data``, the key the Marketplace reads its GitHub account from and its
    own config flow stores after GitHub's device-flow login; the entry's
    other data stays. Returns True if a ``marketplace`` entry was found and
    patched, False if the document has none (doc left untouched).
    """
    for entry in config_entries_doc.get("data", {}).get("entries", []):
        if entry.get("domain") == "marketplace":
            entry["data"] = {**entry.get("data", {}), "token": token}
            return True
    return False


def inject_marketplace_token_in_qcow2(image_path: Path) -> None:
    """Connect the Marketplace inside the qcow2 to GitHub with the CI token.

    The baked Marketplace config entry carries no token, so the Marketplace
    calls the GitHub API unauthenticated — 60 requests/hour per IP, shared
    across GitHub-hosted runners — and refuses to add custom repositories.
    The testcontainer path already injects GITHUB_TOKEN into the seeded
    entry (see conftest's container setup); this brings the HAOS backend to
    parity by editing the config entry inside the per-worker qcow2 before
    boot. GITHUB_TOKEN gets 1,000 requests/hour scoped per repository, so
    the shared-IP limit stops applying.

    Token lifecycle is safe by construction: the workflow saves the image
    cache BEFORE the test step runs, and xdist workers boot per-run
    overlays, so the cached base image never carries a token (which would
    be expired next run anyway — GITHUB_TOKEN dies with the job).

    No-op with a log line when GITHUB_TOKEN is unset (local runs). A
    guestfish failure degrades LOUDLY to a warning instead of failing the
    session: the suite then runs with the Marketplace not connected to
    GitHub (rate-limit flakes possible).
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        LOG.info(
            "GITHUB_TOKEN not set — the Marketplace in %s stays unauthenticated "
            "(GitHub rate-limit flakes possible)",
            image_path,
        )
        return

    import shutil
    import tempfile

    storage_path = "/supervisor/homeassistant/.storage/core.config_entries"
    workdir = Path(tempfile.mkdtemp(prefix="haos-marketplace-token-"))
    try:
        try:
            subprocess.run(
                [
                    "guestfish",
                    "--ro",
                    "-a",
                    str(image_path),
                    "run",
                    ":",
                    "mount",
                    "/dev/sda8",
                    "/",
                    ":",
                    "copy-out",
                    storage_path,
                    str(workdir),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            local = workdir / "core.config_entries"
            doc = json.loads(local.read_text(encoding="utf-8"))
            if not inject_marketplace_token(doc, token):
                LOG.warning(
                    "No marketplace config entry in %s — token not injected; "
                    "the Marketplace stays unauthenticated (GitHub rate-limit "
                    "flakes possible)",
                    image_path,
                )
                return
            local.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            subprocess.run(
                [
                    "guestfish",
                    "--rw",
                    "-a",
                    str(image_path),
                    "run",
                    ":",
                    "mount",
                    "/dev/sda8",
                    "/",
                    ":",
                    "copy-in",
                    str(local),
                    "/supervisor/homeassistant/.storage/",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            LOG.info(
                "Injected GITHUB_TOKEN into the Marketplace config entry in %s",
                image_path,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            stderr = (getattr(exc, "stderr", "") or "").strip()
            LOG.warning(
                "Marketplace token injection failed for %s (%s%s) — the "
                "Marketplace stays unauthenticated (GitHub rate-limit flakes "
                "possible)",
                image_path,
                type(exc).__name__,
                f": {stderr[-300:]}" if stderr else "",
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
