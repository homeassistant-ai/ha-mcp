"""Launcher-lane regression test for the HACS startup nudge (the add-on gap).

The nudge that asks HACS to refresh the paired component's release data used to
be scheduled by ``__main__``'s ``_run_with_shutdown``, so any launcher that
calls ``mcp.run()`` directly — the add-on's ``start.py`` above all — never got
it. The fix moves the scheduling into the server's FastMCP ``lifespan``
(``hacs_refresh_lifespan`` in ``src/ha_mcp/hacs_auto_refresh.py``), which every
launcher runs because it is attached to the server itself, not to one entry
point.

The unit suite pins that wiring (the lifespan is attached, the task is created
and cancelled). What it cannot show is the runtime chain completing in a real
process, which is precisely what the add-on gap broke — and why CI stayed green
through it. So this test boots the real ``ha-mcp`` stdio binary (the same
``main()`` → ``run_async`` path real launchers hit) and watches for the
observable end of the chain: process start → lifespan → task → admin WebSocket
→ HACS repository list → marker file in the ha-mcp data dir.

Stdio stands in for every launcher here because the lifespan is transport-level:
one real launcher proving the chain runs end to end, plus the unit-level wiring
pin, covers the others. The e2e container ships HACS in ``custom_components``
but has neither candidate repository downloaded, so the pass completes on the
first attempt with no GitHub traffic and writes the marker immediately.
"""

import json
import logging
import os

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from test_constants import TEST_TOKEN

from ha_mcp.hacs_auto_refresh import MARKER_FILENAME_PREFIX

from ...utilities.wait_helpers import wait_for_condition

logger = logging.getLogger(__name__)


@pytest.mark.hacs
async def test_stdio_launcher_runs_the_startup_nudge(
    ha_container_with_fresh_config, tmp_path
):
    """A real ha-mcp subprocess must complete the nudge and write its marker."""
    logger.info("Testing the HACS startup nudge through the stdio launcher...")

    container_info = ha_container_with_fresh_config

    # Mirrors the ``stdio_mcp_client`` fixture's env — that fixture is
    # session-scoped and shares one subprocess, so this test spawns its own —
    # plus HA_MCP_CONFIG_DIR, which puts the subprocess's data dir (and so the
    # marker the nudge writes) inside this test's tmp dir. The subprocess
    # inherits nothing, so HA_MCP_DISABLE_UPDATE_CHECK — which would return the
    # nudge early — is deliberately absent.
    env = {
        "HOMEASSISTANT_URL": container_info["base_url"],
        "HOMEASSISTANT_TOKEN": TEST_TOKEN,
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "HA_MAX_RETRIES": "1",
        "ENABLE_STRICT_MANDATORY_BPS": "false",
        "HA_MCP_CONFIG_DIR": str(tmp_path),
    }

    transport = StdioTransport(command="ha-mcp", args=[], env=env)

    def marker_files():
        return list(tmp_path.glob(f"{MARKER_FILENAME_PREFIX}_*.json"))

    async with Client(transport):
        # Poll INSIDE the client context: leaving it terminates the subprocess,
        # and the lifespan cancels the nudge task on the way out.
        found = await wait_for_condition(
            marker_files,
            timeout=30,
            condition_name="HACS refresh marker written by the stdio launcher",
        )
        markers = marker_files()

    assert found, (
        "No HACS refresh marker appeared in the subprocess data dir within 30s "
        "— the startup nudge never completed a pass, so the server lifespan is "
        "likely not scheduling maybe_refresh_hacs_after_update. Data dir "
        f"contents: {sorted(p.name for p in tmp_path.iterdir())}"
    )
    assert len(markers) == 1, (
        "The nudge writes one marker per HA target and this run had exactly "
        f"one, got: {[p.name for p in markers]}"
    )

    marker = json.loads(markers[0].read_text(encoding="utf-8"))
    logger.info(f"Marker written by the stdio launcher: {marker}")

    # A completed pass against this container must have SEEN HACS. "absent" is
    # written only after the retry schedule runs out, which would mean the
    # WebSocket reached HA but HACS never answered.
    assert marker["hacs"] == "present", (
        f"Marker reports hacs={marker['hacs']!r}, but the e2e container ships "
        "HACS in custom_components — the pass did not reach a loaded HACS."
    )
    # ``latest`` is intentionally not asserted: it is None or a version string
    # depending on whether PyPI is reachable from the runner, and both are fine.
    server_version = marker["server_version"]
    assert isinstance(server_version, str) and server_version, (
        "Marker must record the running server version so the next startup can "
        f"detect an update, got {server_version!r}"
    )

    logger.info("Stdio launcher startup nudge test passed")
