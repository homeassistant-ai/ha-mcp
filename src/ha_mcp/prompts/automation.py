"""Automation workflow prompts — guided creation, review, and validation flows."""

from __future__ import annotations

from fastmcp import FastMCP


def register_automation_prompts(mcp: FastMCP) -> None:
    """Register automation creation and review prompts."""

    @mcp.prompt()
    async def create_automation(description: str) -> str:
        """Full automation creation workflow: discover entities → draft YAML → validate → reload."""
        text = (
            f"Creating automation: **{description}**\n\n"
            "**Step 1 — Entity Discovery**\n"
            "Before writing any YAML, find all relevant entities:\n"
            "- Use `ha_search_entities` to find entities by room or device type\n"
            "- Confirm entity_ids exist and are not `unavailable`\n"
            "- Prefer `entity_id` over `device_id` in all triggers and actions\n\n"
            "**Step 2 — YAML Draft (conventions)**\n"
            "Write the automation YAML following these rules:\n"
            "- `mode`: restart for motion/timeout, queued for sequential, single for one-shot\n"
            "- `continue_on_error: true` on all non-critical action steps\n"
            "- Native constructs first (numeric_state, time conditions) before Jinja2 templates\n"
            "- Use `script.smart_announcement_universal_notifier` for any announcements\n"
            "- Ask if a room-hold boolean should gate the automation\n\n"
            "**Step 3 — Show YAML and Get Confirmation**\n"
            "Present the full YAML to the user. Wait for explicit approval before writing.\n\n"
            "**Step 4 — Apply and Validate**\n"
            "Call `ha_config_set_automation` with the approved YAML.\n"
            "Then call `ha_check_config` — abort and show errors if validation fails.\n\n"
            "**Step 5 — Reload**\n"
            "Call `ha_reload_config` (automations domain). Confirm the automation appears in HA."
        )
        return text

    @mcp.prompt()
    async def review_automation(automation_id: str) -> str:
        """Review an existing automation: read config + traces → check against conventions."""
        text = (
            f"Reviewing automation: `{automation_id}`\n\n"
            "**Step 1 — Read Current Config**\n"
            f"Call `ha_config_get_automation` with automation_id=`{automation_id}`.\n"
            "Store the full YAML for analysis.\n\n"
            "**Step 2 — Check Recent Traces**\n"
            f"Call `ha_get_automation_traces` with automation_id=`{automation_id}`, limit=3.\n"
            "Note: did it run as expected? Any errors?\n\n"
            "**Step 3 — Convention Checklist**\n"
            "Review the YAML against these rules:\n"
            "- [ ] `mode` is explicitly set and appropriate\n"
            "- [ ] `continue_on_error: true` on non-critical steps\n"
            "- [ ] No `device_id` in triggers/actions (except Z2M device triggers)\n"
            "- [ ] No `wait_template` where `wait_for_trigger` would be better\n"
            "- [ ] TTS routed through `script.smart_announcement_universal_notifier`\n"
            "- [ ] Tuya entities use `tuya_local` domain, not `tuya` cloud\n"
            "- [ ] Motion lights use `mode: restart` not `mode: single`\n\n"
            "**Step 4 — Report**\n"
            "List: issues found / conventions violated / suggested improvements. "
            "Show the corrected YAML only if changes are needed."
        )
        return text
