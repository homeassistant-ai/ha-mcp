"""Status prompts — system overview, health checks, and area-level status reports."""

from __future__ import annotations

from fastmcp import FastMCP


def register_status_prompts(mcp: FastMCP) -> None:
    """Register system and area status prompts."""

    @mcp.prompt()
    async def system_status() -> str:
        """Full system health overview: entities → updates → errors → recommendations."""
        text = (
            "**Home Assistant System Status Check**\n\n"
            "Run these steps in order and compile a summary report:\n\n"
            "**1. Overview**\n"
            "Call `ha_get_system_info` — record HA version, uptime, location name.\n\n"
            "**2. Unavailable Entities**\n"
            "Call `ha_search_entities` with query='unavailable' or use `ha_get_state` in bulk.\n"
            "List all entities with state=`unavailable` or `unknown` grouped by integration.\n\n"
            "**3. Pending Updates**\n"
            "Call `ha_get_updates` — list HA core, OS, supervisor, and HACS updates pending.\n\n"
            "**4. Recent Errors**\n"
            "Call `ha_get_logs` with level=ERROR, limit=20.\n"
            "Group by source component. Highlight anything firing >10 times.\n\n"
            "**5. Report**\n"
            "Present a structured summary:\n"
            "- System: version / uptime\n"
            "- Unavailable: N entities (list top offenders)\n"
            "- Updates: N pending\n"
            "- Errors: top 3 recurring issues with suggested action\n"
            "- Overall health: ✅ Green / ⚠️ Yellow / ❌ Red"
        )
        return text

    @mcp.prompt()
    async def area_status(area_name: str) -> str:
        """Deep-dive status for a single area: all devices, entities, and active automations."""
        text = (
            f"**Area Status: {area_name}**\n\n"
            "**Step 1 — Entity Inventory**\n"
            f"Call `ha_search_entities` with query=`{area_name}` or use area registry tools.\n"
            f"List all entities in `{area_name}` grouped by domain "
            "(lights, sensors, switches, climate, media_player).\n\n"
            "**Step 2 — Current States**\n"
            "For each entity found: call `ha_get_state` and note current value.\n"
            "Flag any that are `unavailable` or `unknown`.\n\n"
            "**Step 3 — Active Automations**\n"
            f"Search for automations referencing `{area_name.lower()}` via `ha_deep_search`.\n"
            "List which are enabled vs disabled.\n\n"
            "**Step 4 — Summary**\n"
            f"Report for {area_name}:\n"
            "- Lights: on/off count\n"
            "- Climate: current temp / setpoint\n"
            "- Sensors: motion / occupancy state\n"
            "- Issues: unavailable devices\n"
            "- Automations: active count"
        )
        return text
