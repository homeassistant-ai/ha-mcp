"""
Bug report tool for Home Assistant MCP Server.

This module provides a tool to collect diagnostic information and guide users
on how to create effective bug reports.
"""

import logging
from typing import Any

from ha_mcp import __version__

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
    async def ha_bug_report() -> dict[str, Any]:
        """
        Get diagnostic information for bug reports.

        Collects version information, connection status, and entity counts
        to help users create effective bug reports.

        **No parameters required** - just call this tool to get diagnostic output.

        **What this tool provides:**
        - Home Assistant version
        - ha-mcp server version
        - Connection status
        - Entity count
        - Instructions for reporting bugs

        **Example usage:**
        ```python
        # Get bug report info
        info = ha_bug_report()
        # Copy the output and include it in your GitHub issue
        ```
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

        report_lines.extend(
            [
                "",
                "=== How to Report a Bug ===",
                "",
                "1. Copy the information above",
                "2. Go to: https://github.com/homeassistant-ai/ha-mcp/issues/new",
                "3. Select 'Bug Report' template",
                "4. Describe what you were trying to do",
                "5. Describe what happened vs what you expected",
                "6. Include any error messages you received",
                "7. Paste the diagnostic info above in the Environment section",
            ]
        )

        formatted_report = "\n".join(report_lines)

        return {
            "success": True,
            "diagnostic_info": diagnostic_info,
            "formatted_report": formatted_report,
            "issue_url": "https://github.com/homeassistant-ai/ha-mcp/issues/new",
        }
