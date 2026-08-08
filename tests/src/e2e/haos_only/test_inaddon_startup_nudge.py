"""Add-on-lane regression test for the HACS startup nudge scheduling.

The launcher gap this branch fixes was SPECIFIC to the add-on: `start.py`
calls ``mcp.run()`` directly, so the nudge scheduled from ``__main__``'s
``_run_with_shutdown`` never ran there, and no CI lane noticed. This test
closes that lane: the inaddon tier runs the real dev add-on built from this
PR's source, and a due startup pass emits one unconditional INFO line
*before* any WebSocket work (``hacs_auto_refresh.py``), so the line appears
even though the HAOS VM ships no HACS (where the pass then retries quietly
and ends silently). Grepping the add-on's own logs for that line proves the
real add-on launcher entered the server lifespan and started the nudge —
exactly what was silently missing before.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from ..utilities.assertions import parse_mcp_result
from ..utilities.wait_helpers import wait_for_condition

LOG = logging.getLogger(__name__)

# Slug of the dev add-on the inaddon tier boots (see test_inaddon_source_refresh).
DEV_ADDON_NAME = "Home Assistant MCP Server (Dev)"

NUDGE_LOG_SIGNATURE = "HACS auto-refresh: startup pass due"


@pytest.mark.inaddon_only
async def test_addon_launcher_schedules_the_startup_nudge(mcp_client: Any) -> None:
    """The running dev add-on's logs must carry the nudge's startup line.

    A fresh bake has no marker, so the pass is always due on the add-on's
    first boot; the INFO line lands before any WebSocket work, so HACS
    being absent from the VM cannot suppress it.
    """
    LOG.info("Looking up the dev add-on slug...")
    raw = await mcp_client.call_tool("ha_get_addon", {})
    data = parse_mcp_result(raw)
    addons = data.get("addons") or []
    dev_addon = next((a for a in addons if a.get("name") == DEV_ADDON_NAME), None)
    assert dev_addon, f"Dev add-on {DEV_ADDON_NAME!r} not installed: {addons}"
    slug = dev_addon["slug"]

    async def nudge_line_logged() -> bool:
        raw_logs = await mcp_client.call_tool(
            "ha_get_logs",
            {"source": "supervisor", "slug": slug, "search": NUDGE_LOG_SIGNATURE},
        )
        payload = parse_mcp_result(raw_logs)
        return bool(payload.get("success")) and payload.get("returned_lines", 0) > 0

    found = await wait_for_condition(
        nudge_line_logged,
        timeout=60,
        condition_name="startup nudge INFO line in the dev add-on logs",
    )
    assert found, (
        f"The dev add-on's logs never showed {NUDGE_LOG_SIGNATURE!r} — the "
        "add-on launcher did not schedule the startup nudge. This is the "
        "exact regression the lifespan wiring exists to prevent: start.py "
        "runs mcp.run() directly, so only a server-attached lifespan reaches "
        "the add-on."
    )
    LOG.info("Add-on launcher scheduled the startup nudge")
