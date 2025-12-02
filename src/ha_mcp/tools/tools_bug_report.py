"""
Bug report tool for Home Assistant MCP Server.

This module provides a tool to collect diagnostic information and guide users
on how to create effective bug reports.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from ha_mcp import __version__

from ..utils.usage_logger import AVG_LOG_ENTRIES_PER_TOOL, get_recent_logs
from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_bug_report_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register bug report tools with the MCP server."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["system", "diagnostics"],
            "title": "Bug Report Info",
        }
    )
    @log_tool_usage
    async def ha_bug_report(
        tool_call_count: Annotated[
            int,
            Field(
                default=3,
                ge=1,
                le=50,
                description=(
                    "Number of tool calls made since the bug started. "
                    "This determines how many log entries to include. "
                    "The AI agent should count how many ha_* tools it called "
                    "from when the issue began. Default: 3"
                ),
            ),
        ] = 3,
    ) -> dict[str, Any]:
        """
        Collect diagnostic information for filing bug reports against ha-mcp.

        **WHEN TO USE THIS TOOL:**
        Use this tool when the user says something like:
        - "I want to file a bug for: <reason>"
        - "This isn't working, I need to report this"
        - "How do I report this issue?"

        **BEFORE CALLING THIS TOOL:**
        If the bug details are unclear, guide the user to provide:
        1. What they were trying to do (the goal)
        2. What actually happened (the result)
        3. What they expected to happen instead
        4. Any error messages they saw
        5. Steps to reproduce the issue

        **PARAMETERS:**
        - tool_call_count: Count how many ha_* tools you called since the bug started.
          This helps include the right amount of logs. Default is 3.

        **OUTPUT:**
        Returns diagnostic info, recent logs, and a bug report template.
        The template guides privacy-conscious reporting while preserving
        enough detail to diagnose the issue.
        """
        diagnostic_info: dict[str, Any] = {
            "ha_mcp_version": __version__,
            "connection_status": "Unknown",
            "home_assistant_version": "Unknown",
            "entity_count": 0,
        }

        # Try to get Home Assistant config and connection status
        try:
            config = await client.get_config()
            diagnostic_info["connection_status"] = "Connected"
            diagnostic_info["home_assistant_version"] = config.get(
                "version", "Unknown"
            )
            diagnostic_info["location_name"] = config.get("location_name", "Unknown")
            diagnostic_info["time_zone"] = config.get("time_zone", "Unknown")
        except Exception as e:
            logger.warning(f"Failed to get Home Assistant config: {e}")
            diagnostic_info["connection_status"] = f"Connection Error: {str(e)}"

        # Try to get entity count
        try:
            states = await client.get_states()
            if states:
                diagnostic_info["entity_count"] = len(states)
        except Exception as e:
            logger.warning(f"Failed to get entity count: {e}")

        # Calculate how many log entries to retrieve
        # Formula: AVG_LOG_ENTRIES_PER_TOOL * 2 * tool_call_count
        max_log_entries = AVG_LOG_ENTRIES_PER_TOOL * 2 * tool_call_count
        recent_logs = get_recent_logs(max_entries=max_log_entries)

        # Format logs for inclusion (sanitized summary)
        log_summary = _format_logs_for_report(recent_logs)

        # Build the formatted report
        report_lines = [
            "=== ha-mcp Bug Report Info ===",
            "",
            f"Home Assistant Version: {diagnostic_info['home_assistant_version']}",
            f"ha-mcp Version: {diagnostic_info['ha_mcp_version']}",
            f"Connection Status: {diagnostic_info['connection_status']}",
            f"Entity Count: {diagnostic_info['entity_count']}",
        ]

        # Add optional fields if available
        if "location_name" in diagnostic_info:
            report_lines.append(f"Location Name: {diagnostic_info['location_name']}")
        if "time_zone" in diagnostic_info:
            report_lines.append(f"Time Zone: {diagnostic_info['time_zone']}")

        if recent_logs:
            report_lines.extend([
                "",
                f"=== Recent Tool Calls ({len(recent_logs)} entries) ===",
                log_summary,
            ])

        formatted_report = "\n".join(report_lines)

        # Bug report template for the AI to present to the user
        bug_report_template = _generate_bug_report_template(
            diagnostic_info, log_summary
        )

        # Anonymization instructions
        anonymization_guide = _generate_anonymization_guide()

        return {
            "success": True,
            "diagnostic_info": diagnostic_info,
            "recent_logs": recent_logs,
            "log_count": len(recent_logs),
            "formatted_report": formatted_report,
            "bug_report_template": bug_report_template,
            "anonymization_guide": anonymization_guide,
            "issue_url": "https://github.com/homeassistant-ai/ha-mcp/issues/new",
            "instructions": (
                "Present the bug_report_template to the user. "
                "Ask them to review and fill in the [DESCRIBE...] sections. "
                "Remind them to follow the anonymization_guide to protect their privacy. "
                "The user should copy the completed template and submit it at the issue_url."
            ),
        }


def _format_logs_for_report(logs: list[dict[str, Any]]) -> str:
    """Format log entries for inclusion in a bug report."""
    if not logs:
        return "(No recent logs available)"

    lines = []
    for log in logs:
        timestamp = log.get("timestamp", "?")[:19]  # Trim to seconds
        tool_name = log.get("tool_name", "unknown")
        success = "OK" if log.get("success") else "FAIL"
        exec_time = log.get("execution_time_ms", 0)
        error = log.get("error_message", "")

        line = f"  {timestamp} | {tool_name} | {success} | {exec_time:.0f}ms"
        if error:
            # Truncate error to avoid leaking sensitive info
            error_short = str(error)[:100]
            line += f" | Error: {error_short}"
        lines.append(line)

    return "\n".join(lines)


def _generate_bug_report_template(
    diagnostic_info: dict[str, Any], log_summary: str
) -> str:
    """Generate a bug report template for users to fill out."""
    return f"""## Bug Report Template

**Copy this template, fill in the sections, and submit at:**
https://github.com/homeassistant-ai/ha-mcp/issues/new

---

### I want to file a bug for: [DESCRIBE THE BUG IN ONE SENTENCE]

### What I was trying to do
[DESCRIBE YOUR GOAL - What were you asking the AI to do?]

### What happened
[DESCRIBE THE RESULT - What did the AI do or say?]

### What I expected
[DESCRIBE EXPECTED BEHAVIOR - What should have happened instead?]

### Steps to reproduce
1. [First step]
2. [Second step]
3. [...]

### Error messages (if any)
```
[PASTE ANY ERROR MESSAGES HERE]
```

### Environment
- Home Assistant Version: {diagnostic_info.get('home_assistant_version', 'Unknown')}
- ha-mcp Version: {diagnostic_info.get('ha_mcp_version', 'Unknown')}
- Connection Status: {diagnostic_info.get('connection_status', 'Unknown')}
- Entity Count: {diagnostic_info.get('entity_count', 0)}
- Time Zone: {diagnostic_info.get('time_zone', 'Unknown')}

### Recent tool calls
```
{log_summary}
```

### Additional context
[ADD ANY OTHER RELEVANT INFORMATION]

---
**Privacy note:** Please review and anonymize any sensitive information before submitting.
See the anonymization guide in the tool output for details.
"""


def _generate_anonymization_guide() -> str:
    """Generate privacy/anonymization instructions."""
    return """## Anonymization Guide

Before submitting your bug report, please review and anonymize:

### MUST ANONYMIZE (security-sensitive):
- API tokens, passwords, secrets -> Replace with "[REDACTED]"
- IP addresses (internal/external) -> Replace with "192.168.x.x" or "[IP]"
- MAC addresses -> Replace with "[MAC]"
- Email addresses -> Replace with "user@example.com"
- Phone numbers -> Replace with "[PHONE]"

### CONSIDER ANONYMIZING (privacy-sensitive):
- Location names (city, address) -> Replace with generic names like "Home" or "[LOCATION]"
- Device names that reveal personal info -> Replace with "Device 1", "Light 1", etc.
- Person names in entity IDs -> Replace with "person.user1"
- Calendar/todo items with personal details -> Summarize without specifics

### KEEP AS-IS (helpful for debugging):
- Entity domains (light, switch, sensor, etc.)
- Device types and capabilities
- Automation/script structure (triggers, conditions, actions)
- Error messages (but check for secrets in them)
- Timestamps and durations
- State values (on/off, numeric values, etc.)
- Home Assistant and ha-mcp versions

### Example anonymization:
BEFORE: "light.juliens_bedroom" with token "eyJhbG..."
AFTER:  "light.bedroom_1" with token "[REDACTED]"

The goal is to preserve enough detail to reproduce and fix the bug
while protecting your personal information and security.
"""
