"""
Integration management tools for Home Assistant MCP server.

This module provides tools to list and query Home Assistant integrations
(config entries) via the REST API.
"""

import logging
from typing import Any

from .helpers import log_tool_usage

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
