"""Security prompts — lock and alarm workflows with explicit confirmation steps."""

from __future__ import annotations

from fastmcp import FastMCP


def register_security_prompts(mcp: FastMCP) -> None:
    """Register lock and alarm security workflow prompts."""

    @mcp.prompt()
    async def lock_workflow(entity_id: str, action: str) -> str:
        """Safe lock/unlock workflow with state verification and explicit confirmation."""
        text = (
            f"**Lock Workflow: {action} on `{entity_id}`**\n\n"
            "**Step 1 — Verify Entity**\n"
            f"Call `ha_get_entity` with entity_id=`{entity_id}`.\n"
            "Confirm: domain is `lock`, entity is not `unavailable`.\n\n"
            "**Step 2 — Read Current State**\n"
            "Record current state: `locked` / `unlocked` / `jammed` / `locking` / `unlocking`.\n"
            f"If already in target state: report 'already {action}ed, no action needed.'\n\n"
            "**Step 3 — Confirm with User**\n"
            f"Present: 'Lock `{entity_id}` is currently [state]. Confirm: {action} it?'\n"
            "Wait for explicit user confirmation before proceeding.\n\n"
            "**Step 4 — Execute**\n"
            f"Call `lock.{action}` service with entity_id=`{entity_id}`.\n\n"
            "**Step 5 — Verify**\n"
            "Wait 3 seconds, then call `ha_get_entity` again.\n"
            f"Confirm state changed to `{action}ed`. If not, report failure and check error logs."
        )
        return text

    @mcp.prompt()
    async def alarm_workflow(
        entity_id: str, action: str, code: str = ""
    ) -> str:
        """Safe alarm control panel workflow with code validation and explicit confirmation."""
        code_note = (
            f"Code `{code}` provided. Verify it matches `code_arm_required` attribute before use."
            if code
            else "**No code provided.** Check `code_arm_required` attribute — if True, a code is mandatory."
        )
        text = (
            f"**Alarm Workflow: {action} on `{entity_id}`**\n\n"
            "**Step 1 — Verify Panel**\n"
            f"Call `ha_get_entity` with entity_id=`{entity_id}`.\n"
            "Confirm: domain is `alarm_control_panel`, entity is not `unavailable`.\n"
            "Record attributes: `code_arm_required`, `changed_by`, current state.\n\n"
            "**Step 2 — Code Check**\n"
            f"{code_note}\n\n"
            "**Step 3 — Validate Action**\n"
            "Valid actions: `arm_home`, `arm_away`, `arm_night`, `arm_vacation`, `disarm`.\n"
            f"Check if `{action}` is valid from the current panel state.\n\n"
            "**Step 4 — Explicit User Confirmation**\n"
            f"Present: 'Alarm `{entity_id}` is [state]. Confirm: {action}?'\n"
            "For `disarm`: also ask 'Is this intentional? The system will be unprotected.'\n"
            "Wait for user confirmation.\n\n"
            "**Step 5 — Execute**\n"
            f"Call `alarm_control_panel.{action}` with entity_id=`{entity_id}`"
            + (f" and code=`{code}`" if code else "") + ".\n\n"
            "**Step 6 — Verify**\n"
            "Wait 5 seconds, confirm panel transitioned to expected state. Report result."
        )
        return text
