"""Troubleshooting prompts — structured diagnostic chains for broken automations and entities.

Chains ha_get_automation_traces → ha_get_logs → ha_get_state for root-cause analysis.
"""

from __future__ import annotations

from fastmcp import FastMCP


def register_troubleshooting_prompts(mcp: FastMCP) -> None:
    """Register troubleshooting diagnostic prompts."""

    @mcp.prompt()
    async def diagnose_automation(automation_id: str) -> str:
        """Structured diagnostic chain: traces → logs → entity states → root cause."""
        text = (
            f"Diagnosing automation: `{automation_id}`\n\n"
            "**Step 1 — Execution Traces**\n"
            f"Call `ha_get_automation_traces` with automation_id=`{automation_id}`, limit=5.\n"
            "Look for:\n"
            "- Last run timestamp and trigger that fired\n"
            "- Which step failed (error message, condition that blocked)\n"
            "- Whether it ran at all (if not: trigger never fired)\n\n"
            "**Step 2 — Error Logs**\n"
            f"Call `ha_get_logs` with search=`{automation_id}`, level=ERROR, limit=20.\n"
            "Also search for any entity_ids referenced in the automation.\n\n"
            "**Step 3 — Entity States**\n"
            "For every entity referenced in the automation:\n"
            "- Call `ha_get_entity` — check if state is `unavailable` or `unknown`\n"
            "- Note last_changed vs expected trigger time\n\n"
            "**Step 4 — Root Cause Summary**\n"
            "Based on steps 1–3, identify: trigger issue / condition blocking / "
            "entity unavailable / service call failure / YAML error. "
            "Report the specific cause and the fix needed."
        )
        return text

    @mcp.prompt()
    async def diagnose_entity(entity_id: str) -> str:
        """Diagnose why an entity is unavailable, unknown, or behaving unexpectedly."""
        text = (
            f"Diagnosing entity: `{entity_id}`\n\n"
            "**Step 1 — Current State**\n"
            f"Call `ha_get_entity` with entity_id=`{entity_id}`.\n"
            "Record: state, attributes, last_changed, last_updated, device_class, integration.\n\n"
            "**Step 2 — Integration Logs**\n"
            "Extract the integration name from the entity_id prefix (e.g. `tuya_local`, `zigbee2mqtt`).\n"
            f"Call `ha_get_logs` with search=`{entity_id}`, level=WARNING, limit=30.\n"
            "Also search by integration name.\n\n"
            "**Step 3 — Device Registry**\n"
            "If unavailable: check if the parent device is reachable.\n"
            "For Tuya devices: prefer `tuya_local` entity over `tuya` cloud entity.\n"
            "For Zigbee: check Z2M bridge state (`sensor.zigbee2mqtt_bridge_state`).\n\n"
            "**Step 4 — Fix Path**\n"
            "- `unavailable`: device offline → check network/power\n"
            "- `unknown`: integration restarting → wait or reload integration\n"
            "- Wrong value: calibration or unit issue → check device attributes\n"
            "- Missing entity: check entity registry, may need re-pairing"
        )
        return text
