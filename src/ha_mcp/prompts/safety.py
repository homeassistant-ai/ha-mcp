"""Safety prompts — guard rails before executing dangerous, bulk, or irreversible actions.

Enforces read-before-write, quiet-hours awareness, and bulk-action confirmation
to prevent accidental HA state changes.
"""

from __future__ import annotations

from fastmcp import FastMCP


def register_safety_prompts(mcp: FastMCP) -> None:
    """Register safety-guard prompts."""

    @mcp.prompt()
    async def confirm_action(action: str, entity_id: str) -> str:
        """Read current state and confirm before executing any write or delete action."""
        text = (
            f"You are about to execute **{action}** on `{entity_id}`.\n\n"
            "**SAFETY PROTOCOL — complete every step in order:**\n\n"
            f"1. **Read current state** — call `ha_get_entity` with entity_id=`{entity_id}` "
            "and note: state, friendly_name, available/unavailable.\n"
            f"2. **Verify** — does `{action}` make sense given the current state?\n"
            "3. **Side-effects** — will this affect other entities or automations? "
            "(e.g. turning off a light: is someone in the room?)\n"
            "4. **Destructive check** — if this action deletes or resets data, "
            "present a one-sentence summary to the user and wait for explicit confirmation.\n"
            f"5. **Execute** — only after steps 1–4 pass, proceed with `{action}` on `{entity_id}`.\n\n"
            "Do NOT skip the state-read step. Skipping it has caused unintended overrides before."
        )
        return text

    @mcp.prompt()
    async def quiet_hours_check(entity_id: str, purpose: str) -> str:
        """Before triggering TTS, announcements, or lights — check quiet hours and occupancy."""
        text = (
            f"Before triggering `{entity_id}` for **{purpose}**:\n\n"
            "**Quiet-Hours Protocol:**\n\n"
            "1. **Check time** — get `sensor.time` or evaluate `now().hour`.\n"
            "   - Quiet hours: **22:00–07:00**. If active, skip to step 3.\n"
            "2. **Check occupancy** — are any family members sleeping in nearby rooms?\n"
            "   - Check: `binary_sensor.*_occupancy` or `person.*` states.\n"
            "3. **Route accordingly:**\n"
            "   - Normal hours + no sleeping: proceed with `script.smart_announcement_universal_notifier`\n"
            "   - Quiet hours + non-urgent: use silent mobile push only "
            "(`notify.mobile_app_sharon_mobile` or `notify.parentsmobile`)\n"
            "   - Emergency (fire, intrusion, flood): use `script.emergency_alert_all_channels` "
            "(bypasses DND, max volume)\n\n"
            "Never call TTS directly. Always route through the Universal Notifier unless emergency."
        )
        return text

    @mcp.prompt()
    async def read_before_write(entity_id: str) -> str:
        """Read current entity state and config before modifying anything."""
        text = (
            f"Before modifying `{entity_id}`:\n\n"
            "**Read-Before-Write Checklist:**\n\n"
            f"1. Call `ha_get_entity` with entity_id=`{entity_id}` — record: state, attributes, last_changed.\n"
            "2. If modifying automation/script config: call `ha_config_get_automation` or "
            "`ha_config_get_script` to get the current YAML.\n"
            "3. Show the current state/config to the user before presenting your proposed change.\n"
            "4. Only after user confirms the diff, apply the change.\n\n"
            "This prevents overwriting recent manual edits made directly in the HA UI."
        )
        return text

    @mcp.prompt()
    async def bulk_action_review(entities: str, action: str) -> str:
        """Guard rail before executing the same action across multiple entities."""
        text = (
            f"You are about to execute **{action}** on multiple entities:\n`{entities}`\n\n"
            "**Bulk Action Review Protocol:**\n\n"
            "1. **Count** — how many entities are targeted? List them explicitly.\n"
            "2. **Sample-check** — read the current state of the first 3 entities with `ha_get_state`.\n"
            "3. **Confirm intent** — present the full entity list and proposed action to the user.\n"
            f"   Ask: *'Confirm: run {action} on all N entities above?'*\n"
            "4. **Execute sequentially** — do NOT fire all service calls in parallel; "
            "use one call at a time and log any failures.\n"
            "5. **Report** — after completion, summarize: succeeded / failed / skipped.\n\n"
            "Bulk actions on the wrong entity set have caused widespread unintended state changes."
        )
        return text
