"""
Integration management tools for Home Assistant MCP server.

This module provides tools to list, enable, and disable Home Assistant integrations
(config entries) via the REST and WebSocket APIs.
"""

import logging
from typing import Any

from .helpers import get_connected_ws_client, log_tool_usage

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

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["integration"], "title": "Enable Integration"})
    @log_tool_usage
    async def ha_enable_integration(
        entry_id: str,
    ) -> dict[str, Any]:
        """
        Enable a disabled Home Assistant integration (config entry).

        Re-enables an integration that was previously disabled. The integration
        will be loaded and start functioning again.

        **Parameters:**
        - entry_id: The config entry ID of the integration to enable.
                   Use ha_list_integrations() to find entry IDs.

        **Example Usage:**
        ```python
        # First, find the integration you want to enable
        integrations = ha_list_integrations(query="browser_mod")
        # Look for the entry_id in the results

        # Then enable it
        ha_enable_integration(entry_id="abc123def456")
        ```

        **Note:** After enabling, the integration will be loaded automatically.
        Check the 'state' field in ha_list_integrations() to verify it loaded successfully.
        """
        ws_client = None
        try:
            # Connect to WebSocket
            ws_client, error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if error or ws_client is None:
                return error or {
                    "success": False,
                    "error": "Failed to establish WebSocket connection",
                }

            # Call config_entries/disable with disabled_by=None to enable
            result = await ws_client.send_command(
                "config_entries/disable",
                entry_id=entry_id,
                disabled_by=None,
            )

            if not result.get("success"):
                error_msg = result.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                return {
                    "success": False,
                    "error": f"Failed to enable integration: {error_msg}",
                    "entry_id": entry_id,
                }

            # Get updated entry info
            require_restart = result.get("result", {}).get("require_restart", False)

            return {
                "success": True,
                "message": f"Integration enabled successfully",
                "entry_id": entry_id,
                "require_restart": require_restart,
                "note": "The integration has been loaded." if not require_restart else "Home Assistant restart required for changes to take effect.",
            }

        except Exception as e:
            logger.error(f"Failed to enable integration: {e}")
            return {
                "success": False,
                "error": f"Failed to enable integration: {str(e)}",
                "entry_id": entry_id,
            }
        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["integration"], "title": "Disable Integration"})
    @log_tool_usage
    async def ha_disable_integration(
        entry_id: str,
    ) -> dict[str, Any]:
        """
        Disable a Home Assistant integration (config entry).

        Disables an integration, stopping it from running. The integration
        configuration is preserved and can be re-enabled later.

        **Parameters:**
        - entry_id: The config entry ID of the integration to disable.
                   Use ha_list_integrations() to find entry IDs.

        **Example Usage:**
        ```python
        # First, find the integration you want to disable
        integrations = ha_list_integrations(query="browser_mod")
        # Look for the entry_id in the results

        # Then disable it
        ha_disable_integration(entry_id="abc123def456")
        ```

        **Note:** Disabling an integration will:
        - Unload the integration
        - Stop all entities from updating
        - Preserve the configuration for later re-enabling

        Use ha_enable_integration() to re-enable the integration.
        """
        ws_client = None
        try:
            # Connect to WebSocket
            ws_client, error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if error or ws_client is None:
                return error or {
                    "success": False,
                    "error": "Failed to establish WebSocket connection",
                }

            # Call config_entries/disable with disabled_by="user" to disable
            result = await ws_client.send_command(
                "config_entries/disable",
                entry_id=entry_id,
                disabled_by="user",
            )

            if not result.get("success"):
                error_msg = result.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                return {
                    "success": False,
                    "error": f"Failed to disable integration: {error_msg}",
                    "entry_id": entry_id,
                }

            # Get updated entry info
            require_restart = result.get("result", {}).get("require_restart", False)

            return {
                "success": True,
                "message": f"Integration disabled successfully",
                "entry_id": entry_id,
                "require_restart": require_restart,
                "note": "The integration has been unloaded." if not require_restart else "Home Assistant restart required for changes to take effect.",
            }

        except Exception as e:
            logger.error(f"Failed to disable integration: {e}")
            return {
                "success": False,
                "error": f"Failed to disable integration: {str(e)}",
                "entry_id": entry_id,
            }
        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["integration"], "title": "Delete Config Entry"})
    @log_tool_usage
    async def ha_delete_config_entry(
        entry_id: str,
        confirm: bool | str = False,
    ) -> dict[str, Any]:
        """
        Permanently delete a Home Assistant config entry (integration).

        This removes an integration completely from Home Assistant, including
        all its configuration. Use this for orphaned entries from dead hardware
        or integrations you no longer need.

        **WARNING:** This is a destructive operation that cannot be undone.
        The integration will need to be set up again from scratch if needed later.

        **Parameters:**
        - entry_id: The config entry ID to delete.
                   Use ha_list_integrations() to find entry IDs.
        - confirm: Must be True to confirm deletion (safety measure).

        **Example Usage:**
        ```python
        # First, find the integration you want to delete
        integrations = ha_list_integrations(query="slzb")
        # Look for the entry_id in the results

        # Then delete it (must confirm)
        ha_delete_config_entry(entry_id="01JBTD7Q1FSFD9WYNCK7T0WT78", confirm=True)
        ```

        **Use Cases:**
        - Remove orphaned entries from dead/replaced hardware
        - Clean up failed integration setup attempts
        - Remove integrations that cannot be unloaded normally
        """
        # Handle string "true"/"false" from some clients
        if isinstance(confirm, str):
            confirm = confirm.lower() == "true"

        if not confirm:
            return {
                "success": False,
                "error": "Deletion not confirmed. Set confirm=True to proceed.",
                "entry_id": entry_id,
                "warning": "This will permanently delete the config entry. This cannot be undone.",
            }

        ws_client = None
        try:
            # Connect to WebSocket
            ws_client, error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if error or ws_client is None:
                return error or {
                    "success": False,
                    "error": "Failed to establish WebSocket connection",
                }

            # Call config_entries/delete to permanently remove the entry
            result = await ws_client.send_command(
                "config_entries/delete",
                entry_id=entry_id,
            )

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
                "note": "The integration has been permanently removed." if not require_restart else "Home Assistant restart required to complete removal.",
            }

        except Exception as e:
            logger.error(f"Failed to delete config entry: {e}")
            return {
                "success": False,
                "error": f"Failed to delete config entry: {str(e)}",
                "entry_id": entry_id,
            }
        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass

