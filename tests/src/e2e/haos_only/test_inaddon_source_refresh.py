"""Verifies the inaddon-tier addon source refresh actually fires per commit.

The inaddon CI tier overwrites the bake-installed addon source with the
PR's HEAD source (``refresh_dev_addon_source_in_qcow2``) and bumps
``config.yaml`` ``version:`` to ``<base>-pr-<GITHUB_SHA[:7]>`` so
Supervisor detects an update and rebuilds the Docker image (Docker
layer cache → ~20-30s).

Without an explicit check, the chain has three silent-failure modes:

1. ``refresh_dev_addon_source_in_qcow2`` runs but the source-write
   inside the qcow2 silently no-ops (e.g. wrong path, libguestfs
   mount failure ignored upstream).
2. The version bump succeeds but Supervisor's update doesn't fire
   (cache-key collision, scanner skipped a tick, etc.).
3. Supervisor reports update success but the addon keeps running the
   old container (``boot_fail`` recovery path takes the previous
   image — happened pre-#1361, fixed with the explicit ``/start``
   after ``/update``).

Querying ``ha_get_addon`` for the dev addon and asserting its version
contains the current commit's ``-pr-<sha>`` tag closes all three holes
in one assertion — every inaddon CI run, every PR commit.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest

from ..utilities.assertions import parse_mcp_result

LOG = logging.getLogger(__name__)

DEV_ADDON_NAME = "Home Assistant MCP Server (Dev)"


@pytest.mark.inaddon_only
async def test_dev_addon_version_reflects_pr_commit(mcp_client: Any) -> None:
    """The running dev addon's version must be tagged with this CI run's SHA.

    Proof-of-life that refresh → version-bump → Supervisor update →
    container restart all fired for THIS commit (not a stale bake image).
    """
    expected_sha = (os.environ.get("GITHUB_SHA", "") or "local")[:7] or "local"
    expected_suffix = f"-pr-{expected_sha}"

    raw = await mcp_client.call_tool("ha_get_addon", {})
    data = parse_mcp_result(raw)

    # ha_get_addon returns ``{"addons": [{"name": str, "version": str, ...}], ...}``.
    # Match on display name from homeassistant-addon-dev/config.yaml.
    addons = data.get("addons") or []
    dev_addon = next(
        (a for a in addons if a.get("name") == DEV_ADDON_NAME),
        None,
    )
    assert dev_addon is not None, (
        f"Dev addon {DEV_ADDON_NAME!r} not in installed addons returned by "
        f"ha_get_addon. Installed names: "
        f"{[a.get('name') for a in addons]}. Either the bake's "
        f"install_ha_mcp_dev_addon failed or the addon was uninstalled "
        f"mid-session."
    )

    version = dev_addon.get("version", "")
    assert expected_suffix in version, (
        f"Dev addon version {version!r} does not contain {expected_suffix!r} — "
        f"refresh_dev_addon_source_in_qcow2 + Supervisor /addons/update + /start "
        f"did NOT apply this commit's source. The addon is still running the "
        f"bake-time or prior-commit image; new src/ha_mcp/ code in this PR is "
        f"NOT being exercised by the inaddon suite."
    )
    LOG.info(
        "Dev addon running version %r contains expected tag %r — refresh chain verified",
        version,
        expected_suffix,
    )
