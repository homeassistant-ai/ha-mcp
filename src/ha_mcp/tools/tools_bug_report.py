"""
Bug report tool for Home Assistant MCP Server.

This module provides a tool to collect diagnostic information and guide users
on how to create effective bug reports.
"""

import logging
import os
import platform
import sys
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from ha_mcp import __version__

from ..utils.usage_logger import AVG_LOG_ENTRIES_PER_TOOL, get_recent_logs, get_startup_logs
from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def _detect_installation_method() -> str:
    """
    Detect how ha-mcp was installed.

    Returns one of: pyinstaller, addon, docker, git, pypi, unknown
    """
    # 1. PyInstaller binary
    if getattr(sys, "frozen", False):
        return "pyinstaller"

    # 2. Home Assistant Add-on (has supervisor token)
    if os.environ.get("SUPERVISOR_TOKEN"):
        return "addon"

    # 3. Docker container (non-addon)
    if Path("/.dockerenv").exists():
        return "docker"

    # 4. Git clone - check for .git directory relative to package
    try:
        # Go up from tools_bug_report.py -> tools -> ha_mcp -> src -> project_root
        project_root = Path(__file__).parent.parent.parent.parent
        if (project_root / ".git").exists():
            return "git"
    except Exception:
        pass

    # 5. PyPI install - marker file exists in package
    try:
        marker_path = Path(__file__).parent.parent / "_pypi_marker"
        if marker_path.exists():
            return "pypi"
    except Exception:
        pass

    # 6. Default - unknown
    return "unknown"


def _detect_platform() -> dict[str, str]:
    """Detect platform information."""
    return {
        "os": platform.system(),  # Windows, Darwin, Linux
        "os_release": platform.release(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
    }


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
                default=10,
                ge=1,
                le=100,
                description=(
                    "Number of tool calls made since the bug started. "
                    "This determines how many log entries to include. "
                    "The AI agent should count how many ha_* tools it called "
                    "from when the issue began. Default: 10 (increased for better context)"
                ),
            ),
        ] = 10,
    ) -> dict[str, Any]:
        """
        Collect diagnostic information for filing bug reports against ha-mcp.

        **IMPORTANT FOR AI AGENTS:**
        When creating a bug report, you MUST only report FACTS that you directly
        observed during the conversation. Do NOT make assumptions or guesses.
        - Report exact error messages you received
        - Report exact tool names and parameters you used
        - Report exact responses from tools
        - Do NOT speculate about causes or solutions
        - Do NOT fill in template sections you cannot answer from the conversation

        **WHEN TO USE THIS TOOL:**
        Use this tool when the user says something like:
        - "I want to file a bug for: <reason>"
        - "This isn't working, I need to report this"
        - "How do I report this issue?"

        **OUTPUT:**
        Returns diagnostic info (auto-populated), recent logs, startup logs,
        and a bug report template. All environment info is automatically filled.
        """
        # Detect installation method and platform
        install_method = _detect_installation_method()
        platform_info = _detect_platform()

        diagnostic_info: dict[str, Any] = {
            "ha_mcp_version": __version__,
            "installation_method": install_method,
            "platform": platform_info,
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
        # Formula: AVG_LOG_ENTRIES_PER_TOOL * 4 * tool_call_count (doubled from 2x to 4x)
        max_log_entries = AVG_LOG_ENTRIES_PER_TOOL * 4 * tool_call_count
        recent_logs = get_recent_logs(max_entries=max_log_entries)

        # Get startup logs (first minute of server operation)
        startup_logs = get_startup_logs()

        # Format logs for inclusion (sanitized summary)
        log_summary = _format_logs_for_report(recent_logs)
        startup_log_summary = _format_startup_logs(startup_logs)

        # Build the formatted report
        report_lines = [
            "=== ha-mcp Bug Report Info ===",
            "",
            f"ha-mcp Version: {diagnostic_info['ha_mcp_version']}",
            f"Installation Method: {diagnostic_info['installation_method']}",
            f"Platform: {platform_info['os']} {platform_info['os_release']} ({platform_info['architecture']})",
            f"Python Version: {platform_info['python_version']}",
            f"Home Assistant Version: {diagnostic_info['home_assistant_version']}",
            f"Connection Status: {diagnostic_info['connection_status']}",
            f"Entity Count: {diagnostic_info['entity_count']}",
        ]

        # Add optional fields if available
        if "location_name" in diagnostic_info:
            report_lines.append(f"Location Name: {diagnostic_info['location_name']}")
        if "time_zone" in diagnostic_info:
            report_lines.append(f"Time Zone: {diagnostic_info['time_zone']}")

        if startup_logs:
            report_lines.extend([
                "",
                f"=== Startup Logs ({len(startup_logs)} entries) ===",
                startup_log_summary,
            ])

        if recent_logs:
            report_lines.extend([
                "",
                f"=== Recent Tool Calls ({len(recent_logs)} entries) ===",
                log_summary,
            ])

        formatted_report = "\n".join(report_lines)

        # Bug report template for the AI to present to the user
        bug_report_template = _generate_bug_report_template(
            diagnostic_info, log_summary, startup_log_summary, recent_logs
        )

        # Anonymization instructions
        anonymization_guide = _generate_anonymization_guide()

        return {
            "success": True,
            "diagnostic_info": diagnostic_info,
            "recent_logs": recent_logs,
            "startup_logs": startup_logs,
            "log_count": len(recent_logs),
            "startup_log_count": len(startup_logs),
            "formatted_report": formatted_report,
            "bug_report_template": bug_report_template,
            "anonymization_guide": anonymization_guide,
            "issue_url": "https://github.com/homeassistant-ai/ha-mcp/issues/new",
            "instructions": (
                "Present the bug_report_template to the user. "
                "The Environment section is already filled with accurate data. "
                "Ask the user to describe the bug, what happened, and what they expected. "
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


def _format_startup_logs(logs: list[dict[str, Any]]) -> str:
    """Format startup log entries for inclusion in a bug report."""
    if not logs:
        return "(No startup logs available)"

    lines = []
    for log in logs:
        elapsed = log.get("elapsed_seconds", 0)
        level = log.get("level", "INFO")
        logger_name = log.get("logger", "")
        message = log.get("message", "")

        # Truncate long messages
        if len(message) > 200:
            message = message[:200] + "..."

        line = f"  +{elapsed:05.2f}s | {level:5} | {logger_name}: {message}"
        lines.append(line)

    return "\n".join(lines)


def _extract_error_messages(logs: list[dict[str, Any]]) -> list[str]:
    """
    Extract error messages from tool call logs.

    Returns a list of error messages with context (tool name, timestamp).
    """
    if not logs:
        return []

    error_messages = []
    for log in logs:
        error = log.get("error_message")
        if error:
            timestamp = log.get("timestamp", "?")[:19]  # Trim to seconds
            tool_name = log.get("tool_name", "unknown")
            # Format: [timestamp] tool_name: error_message
            error_messages.append(f"[{timestamp}] {tool_name}: {error}")

    return error_messages


def _generate_bug_report_template(
    diagnostic_info: dict[str, Any],
    log_summary: str,
    startup_log_summary: str,
    recent_logs: list[dict[str, Any]],
) -> str:
    """
    Generate a bug report template matching runtime_bug.md format.

    This template matches the GitHub issue template EXACTLY so users can
    copy-paste without format conflicts.
    """
    platform_info = diagnostic_info.get("platform", {})

    # Extract error messages from recent logs
    error_messages = _extract_error_messages(recent_logs)
    error_section = "\n".join(error_messages) if error_messages else "<!-- No errors detected in recent logs -->"

    # Show startup logs section only if they exist and are non-empty
    startup_section = ""
    if startup_log_summary and startup_log_summary != "(No startup logs available)":
        startup_section = f"""
---

## 🚀 Startup Logs (if relevant)

<details>
<summary>Click to expand startup logs</summary>

```
{startup_log_summary}
```

</details>
"""

    return f"""## 🚨 Use the `ha_bug_report` Tool First!

> This template was auto-generated by the ha_bug_report tool.
> All environment info and logs below were collected automatically.

**Submit this report at:**
https://github.com/homeassistant-ai/ha-mcp/issues/new?template=runtime_bug.md

---

## 📋 Bug Description
<!-- ONE clear sentence: What went wrong? -->


## 🔄 Steps to Reproduce
1.
2.
3.

## ✅ Expected vs ❌ Actual Behavior

**Expected:**
<!-- What should have happened? -->


**Actual:**
<!-- What actually happened? -->


---

## 🔧 Environment

- **ha-mcp Version:** {diagnostic_info.get('ha_mcp_version', 'Unknown')}
- **Installation Method:** {diagnostic_info.get('installation_method', 'Unknown')}
- **Platform:** {platform_info.get('os', 'Unknown')} {platform_info.get('os_release', '')} ({platform_info.get('architecture', 'Unknown')})
- **Python Version:** {platform_info.get('python_version', 'Unknown')}
- **Home Assistant Version:** {diagnostic_info.get('home_assistant_version', 'Unknown')}
- **Connection Status:** {diagnostic_info.get('connection_status', 'Unknown')}
- **Entity Count:** {diagnostic_info.get('entity_count', 0)}

---

## 🚨 Error Messages

```
{error_section}
```

---

## 📊 Recent Tool Calls

<details>
<summary>Click to expand recent tool calls (auto-filled by ha_bug_report)</summary>

```
{log_summary}
```

</details>
{startup_section}
---

## 💡 Additional Context

<!-- Any other relevant information: -->
<!-- - Suggested fixes -->
<!-- - Workarounds you found -->
<!-- - Related issues -->
<!-- - Configuration snippets -->


---

**Privacy reminder:** Please review and anonymize sensitive information (tokens, IPs, personal names) before submitting.
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
