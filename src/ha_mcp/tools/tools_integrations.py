"""
Integration management tools for Home Assistant MCP server.

This module provides tools to list, enable, disable, and delete Home Assistant
integrations (config entries) via the REST and WebSocket APIs.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import exception_to_structured_error, log_tool_usage
from .util_helpers import coerce_bool_param

logger = logging.getLogger(__name__)


def register_integration_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register integration management tools with the MCP server."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["integration"], "title": "List Integrations"})
    @log_tool_usage
    async def ha_list_integrations(
        query: str | None = None,
    ) -> dict[str, Any]:
        """
        List configured Home Assistant integrations (config entries).

        Returns integration details including domain, title, state, and capabilities.
        Use the optional query parameter to fuzzy search by domain or title.

        States: 'loaded' (running), 'setup_error', 'setup_retry', 'not_loaded',
        'failed_unload', 'migration_error'.
        """
        try:
            # Use REST API endpoint for config entries
            # Note: Using _request() directly as there's no public wrapper method
            # for the config_entries endpoint in the client API
            response = await client._request(
                "GET", "/config/config_entries/entry"
            )

            if not isinstance(response, list):
                return {
                    "success": False,
                    "error": "Unexpected response format from Home Assistant",
                    "response_type": type(response).__name__,
                }

            entries = response

            # Format entries for response
            formatted_entries = []
            for entry in entries:
                formatted_entry = {
                    "entry_id": entry.get("entry_id"),
                    "domain": entry.get("domain"),
                    "title": entry.get("title"),
                    "state": entry.get("state"),
                    "source": entry.get("source"),
                    "supports_options": entry.get("supports_options", False),
                    "supports_unload": entry.get("supports_unload", False),
                    "disabled_by": entry.get("disabled_by"),
                }

                # Include pref_disable_new_entities and pref_disable_polling if present
                if "pref_disable_new_entities" in entry:
                    formatted_entry["pref_disable_new_entities"] = entry[
                        "pref_disable_new_entities"
                    ]
                if "pref_disable_polling" in entry:
                    formatted_entry["pref_disable_polling"] = entry[
                        "pref_disable_polling"
                    ]

                formatted_entries.append(formatted_entry)

            # Apply fuzzy search filter if query provided
            if query and query.strip():
                from ..utils.fuzzy_search import calculate_ratio

                # Perform fuzzy search with both exact and fuzzy matching
                matches = []
                query_lower = query.strip().lower()

                for entry in formatted_entries:
                    domain_lower = entry['domain'].lower()
                    title_lower = entry['title'].lower()

                    # Check for exact substring matches first (highest priority)
                    if query_lower in domain_lower or query_lower in title_lower:
                        # Exact substring match gets score of 100
                        matches.append((100, entry))
                    else:
                        # Try fuzzy matching on domain and title separately
                        domain_score = calculate_ratio(query_lower, domain_lower)
                        title_score = calculate_ratio(query_lower, title_lower)
                        best_score = max(domain_score, title_score)

                        if best_score >= 70:  # threshold for fuzzy matches
                            matches.append((best_score, entry))

                # Sort by score descending
                matches.sort(key=lambda x: x[0], reverse=True)
                formatted_entries = [match[1] for match in matches]

            # Group by state for summary
            state_summary: dict[str, int] = {}
            for entry in formatted_entries:
                state = entry.get("state", "unknown")
                state_summary[state] = state_summary.get(state, 0) + 1

            return {
                "success": True,
                "total": len(formatted_entries),
                "entries": formatted_entries,
                "state_summary": state_summary,
                "query": query if query else None,
            }

        except Exception as e:
            logger.error(f"Failed to list integrations: {e}")
            return {
                "success": False,
                "error": f"Failed to list integrations: {str(e)}",
                "suggestions": [
                    "Verify Home Assistant connection is working",
                    "Check that the API is accessible",
                    "Ensure your token has sufficient permissions",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["integration"],
            "title": "Set Integration Enabled",
        }
    )
    @log_tool_usage
    async def ha_set_integration_enabled(
        entry_id: Annotated[str, Field(description="Config entry ID")],
        enabled: Annotated[
            bool | str, Field(description="True to enable, False to disable")
        ],
    ) -> dict[str, Any]:
        """Enable/disable integration (config entry).

        Use ha_list_integrations() to find entry IDs.
        """
        try:
            enabled_bool = coerce_bool_param(enabled, "enabled")

            message = {
                "type": "config_entries/disable",
                "entry_id": entry_id,
                "disabled_by": None if enabled_bool else "user",
            }

            result = await client.send_websocket_message(message)

            if not result.get("success"):
                error_msg = result.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                return {
                    "success": False,
                    "error": f"Failed to {'enable' if enabled_bool else 'disable'} integration: {error_msg}",
                    "entry_id": entry_id,
                }

            # Get updated entry info
            require_restart = result.get("result", {}).get("require_restart", False)

            return {
                "success": True,
                "message": f"Integration {'enabled' if enabled_bool else 'disabled'} successfully",
                "entry_id": entry_id,
                "require_restart": require_restart,
                "note": (
                    "Integration has been loaded."
                    if enabled_bool and not require_restart
                    else "Integration has been unloaded."
                    if not enabled_bool and not require_restart
                    else "Home Assistant restart required for changes to take effect."
                ),
            }

        except Exception as e:
            logger.error(f"Failed to set integration enabled: {e}")
            return exception_to_structured_error(e, context={"entry_id": entry_id})

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["integration"],
            "title": "Delete Config Entry",
        }
    )
    @log_tool_usage
    async def ha_delete_config_entry(
        entry_id: Annotated[str, Field(description="Config entry ID")],
        confirm: Annotated[
            bool | str, Field(description="Must be True to confirm deletion")
        ] = False,
    ) -> dict[str, Any]:
        """Delete config entry permanently. Requires confirm=True.

        Use ha_list_integrations() to find entry IDs.
        """
        try:
            confirm_bool = coerce_bool_param(confirm, "confirm", default=False)

            if not confirm_bool:
                return {
                    "success": False,
                    "error": "Deletion not confirmed. Set confirm=True to proceed.",
                    "entry_id": entry_id,
                    "warning": "This will permanently delete the config entry. This cannot be undone.",
                }

            message = {
                "type": "config_entries/delete",
                "entry_id": entry_id,
            }

            result = await client.send_websocket_message(message)

            if not result.get("success"):
                error_msg = result.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                return {
                    "success": False,
                    "error": f"Failed to delete config entry: {error_msg}",
                    "entry_id": entry_id,
                }

            # Get result info
            require_restart = result.get("result", {}).get("require_restart", False)

            return {
                "success": True,
                "message": "Config entry deleted successfully",
                "entry_id": entry_id,
                "require_restart": require_restart,
                "note": (
                    "The integration has been permanently removed."
                    if not require_restart
                    else "Home Assistant restart required to complete removal."
                ),
            }

        except Exception as e:
            logger.error(f"Failed to delete config entry: {e}")
            return exception_to_structured_error(e, context={"entry_id": entry_id})
