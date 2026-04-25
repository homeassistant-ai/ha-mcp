"""
Integration management tools for Home Assistant MCP server.

This module provides tools to list, enable, disable, and delete Home Assistant
integrations (config entries) via the REST and WebSocket APIs.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tools_config_entry_flow import FLOW_HELPER_TYPES
from .tools_config_helpers import (
    SIMPLE_HELPER_TYPES,
    _get_entities_for_config_entry,
)
from .util_helpers import (
    build_pagination_metadata,
    coerce_bool_param,
    coerce_int_param,
    wait_for_entity_removed,
)

logger = logging.getLogger(__name__)


async def _get_entry_id_for_flow_helper(
    client: Any,
    helper_type: str,
    target: str,
    warnings: list[str] | None = None,
) -> str | None:
    """Resolve a flow-helper target to its config_entry_id via entity_registry.

    Used by ha_delete_helpers_integrations when target is an entity_id
    (contains a '.') and helper_type is a known flow-helper type.

    Args:
        client: HomeAssistantClient instance.
        helper_type: Flow-helper type (must be in FLOW_HELPER_TYPES).
        target: Full entity_id, e.g. "sensor.my_meter". Bare IDs not
            supported for flow helpers (caller must provide entity_id).
        warnings: Optional list — appended to on WebSocket failure.

    Returns:
        config_entry_id string on success, or None when the entity is not
        in the registry, has no config_entry_id, or the lookup failed.
    """
    if helper_type not in FLOW_HELPER_TYPES:
        return None

    if "." not in target:
        return None
    entity_id = target

    try:
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
    except Exception as e:
        logger.debug(f"entity_registry/get failed for {entity_id}: {e}")
        if warnings is not None:
            warnings.append(
                f"entity_registry/get failed for {entity_id}: {e}"
            )
        return None

    if not isinstance(result, dict) or not result.get("success"):
        return None

    entry = result.get("result") or {}
    if not isinstance(entry, dict):
        return None

    return entry.get("config_entry_id")


class IntegrationTools:
    """Integration management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_integration",
        tags={"Integrations"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Integration",
        },
    )
    @log_tool_usage
    async def ha_get_integration(
        self,
        entry_id: Annotated[
            str | None,
            Field(
                description="Config entry ID to get details for. "
                "If omitted, lists all integrations.",
                default=None,
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="When listing, search by domain or title. "
                "Uses exact substring matching by default; set exact_match=False for fuzzy.",
                default=None,
            ),
        ] = None,
        domain: Annotated[
            str | None,
            Field(
                description="Filter by integration domain (e.g. 'template', 'group'). "
                "When set, includes the full options/configuration for each entry.",
                default=None,
            ),
        ] = None,
        include_options: Annotated[
            bool | str,
            Field(
                description="Include the options object for each entry. "
                "Automatically enabled when domain filter is set. "
                "Useful for auditing template definitions and helper configurations.",
                default=False,
            ),
        ] = False,
        include_schema: Annotated[
            bool | str,
            Field(
                description="When entry_id is set, also return the options flow schema "
                "(available fields and their types). Use before ha_config_set_helper "
                "to understand what can be updated. Only applies when supports_options=true.",
                default=False,
            ),
        ] = False,
        exact_match: Annotated[
            bool | str,
            Field(
                description=(
                    "Use exact substring matching for query filter (default: True). "
                    "Set to False for fuzzy matching when the query may contain typos."
                ),
                default=True,
            ),
        ] = True,
        limit: Annotated[
            int | str,
            Field(
                default=50,
                description="Max entries to return per page in list mode (default: 50)",
            ),
        ] = 50,
        offset: Annotated[
            int | str,
            Field(
                default=0,
                description="Number of entries to skip for pagination (default: 0)",
            ),
        ] = 0,
    ) -> dict[str, Any]:
        """Get integration (config entry) information with pagination.

        Without an entry_id: Lists all configured integrations with optional filters.
        With an entry_id: Returns detailed information including full options/configuration.

        EXAMPLES:
        - List all integrations: ha_get_integration()
        - Paginate: ha_get_integration(offset=50)
        - Search: ha_get_integration(query="zigbee")
        - Get specific entry: ha_get_integration(entry_id="abc123")
        - Get entry with editable fields: ha_get_integration(entry_id="abc123", include_schema=True)
        - List template entries: ha_get_integration(domain="template")

        STATES: 'loaded', 'setup_error', 'setup_retry', 'not_loaded',
        'failed_unload', 'migration_error'.
        """
        try:
            include_opts = coerce_bool_param(
                include_options, "include_options", default=False
            )
            include_schema_bool = coerce_bool_param(
                include_schema, "include_schema", default=False
            )
            exact_match_bool = coerce_bool_param(
                exact_match, "exact_match", default=True
            )
            limit_int = coerce_int_param(
                limit, "limit", default=50, min_value=1, max_value=200
            )
            offset_int = coerce_int_param(offset, "offset", default=0, min_value=0)
            # Auto-enable options when domain filter is set
            if domain is not None:
                include_opts = True

            # If entry_id provided, get specific config entry
            if entry_id is not None:
                return await self._get_single_entry(entry_id, include_schema_bool)

            # List mode - get all config entries
            return await self._list_entries(
                domain, query, include_opts, exact_match_bool, limit_int, offset_int
            )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Failed to get integrations: {e}")
            exception_to_structured_error(
                e,
                suggestions=[
                    "Verify Home Assistant connection is working",
                    "Check that the API is accessible",
                    "Ensure your token has sufficient permissions",
                ],
            )

    async def _get_single_entry(
        self, entry_id: str, include_schema: bool | None
    ) -> dict[str, Any]:
        """Fetch a single config entry by ID, optionally including its options schema."""
        try:
            result = await self._client.get_config_entry(entry_id)
            resp: dict[str, Any] = {
                "success": True,
                "entry_id": entry_id,
                "entry": result,
            }

            # Optionally fetch options flow schema (logically read-only: start+abort)
            if include_schema and result.get("supports_options"):
                await self._fetch_options_schema(entry_id, resp)

            return resp
        except ToolError:
            raise
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg or "not found" in error_msg.lower():
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Config entry not found: {entry_id}",
                        context={"entry_id": entry_id},
                        suggestions=[
                            "Use ha_get_integration() without entry_id to see all config entries",
                        ],
                    )
                )
            raise

    async def _fetch_options_schema(
        self, entry_id: str, resp: dict[str, Any]
    ) -> None:
        """Start an options flow to read the schema, then abort it."""
        flow_id = None
        try:
            flow_result = await self._client.start_options_flow(entry_id)
            flow_id = flow_result.get("flow_id")
            flow_type = flow_result.get("type")
            if flow_type == "form":
                resp["options_schema"] = {
                    "flow_type": "form",
                    "step_id": flow_result.get("step_id"),
                    "data_schema": flow_result.get("data_schema", []),
                }
            elif flow_type == "menu":
                resp["options_schema"] = {
                    "flow_type": "menu",
                    "step_id": flow_result.get("step_id"),
                    "menu_options": flow_result.get("menu_options", []),
                }
        except Exception as schema_err:
            logger.debug(
                f"Failed to fetch options schema for {entry_id}: {schema_err}"
            )
        finally:
            if flow_id:
                try:
                    await self._client.abort_options_flow(flow_id)
                except Exception as abort_err:
                    logger.debug(
                        f"Failed to abort options flow {flow_id}: {abort_err}"
                    )

    async def _list_entries(
        self,
        domain: str | None,
        query: str | None,
        include_opts: bool | None,
        exact_match: bool | None,
        limit_int: int,
        offset_int: int,
    ) -> dict[str, Any]:
        """List config entries with optional domain/query filtering and pagination."""
        # Use REST API endpoint for config entries
        response = await self._client._request("GET", "/config/config_entries/entry")

        if not isinstance(response, list):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from Home Assistant",
                    context={"response_type": type(response).__name__},
                )
            )

        entries = response

        # Apply domain filter before formatting
        if domain:
            domain_lower = domain.strip().lower()
            entries = [
                e for e in entries if e.get("domain", "").lower() == domain_lower
            ]

        # Format entries for response
        formatted_entries = [
            self._format_entry(entry, include_opts) for entry in entries
        ]

        # Apply search filter if query provided
        if query and query.strip():
            formatted_entries = self._filter_by_query(
                formatted_entries, query, exact_match
            )

        # Group by state for summary (computed before pagination for full picture)
        state_summary: dict[str, int] = {}
        for entry in formatted_entries:
            state = entry.get("state", "unknown")
            state_summary[state] = state_summary.get(state, 0) + 1

        # Apply pagination
        total_entries = len(formatted_entries)
        paginated_entries = formatted_entries[offset_int : offset_int + limit_int]

        result_data: dict[str, Any] = {
            "success": True,
            **build_pagination_metadata(
                total_entries, offset_int, limit_int, len(paginated_entries)
            ),
            "entries": paginated_entries,
            "state_summary": state_summary,
            "query": query if query else None,
        }
        if domain:
            result_data["domain_filter"] = domain.strip().lower()
        return result_data

    @staticmethod
    def _format_entry(entry: dict[str, Any], include_opts: bool | None) -> dict[str, Any]:
        """Format a raw config entry into the response shape."""
        formatted_entry: dict[str, Any] = {
            "entry_id": entry.get("entry_id"),
            "domain": entry.get("domain"),
            "title": entry.get("title"),
            "state": entry.get("state"),
            "source": entry.get("source"),
            "supports_options": entry.get("supports_options", False),
            "supports_unload": entry.get("supports_unload", False),
            "disabled_by": entry.get("disabled_by"),
        }

        # Include options when requested (for auditing template definitions, etc.)
        if include_opts:
            formatted_entry["options"] = entry.get("options", {})

        # Include pref_disable_new_entities and pref_disable_polling if present
        if "pref_disable_new_entities" in entry:
            formatted_entry["pref_disable_new_entities"] = entry[
                "pref_disable_new_entities"
            ]
        if "pref_disable_polling" in entry:
            formatted_entry["pref_disable_polling"] = entry[
                "pref_disable_polling"
            ]

        return formatted_entry

    @staticmethod
    def _filter_by_query(
        entries: list[dict[str, Any]], query: str, exact_match: bool | None
    ) -> list[dict[str, Any]]:
        """Filter formatted entries by query string with exact or fuzzy matching."""
        matches: list[tuple[int, dict[str, Any]]] = []
        query_lower = query.strip().lower()

        for entry in entries:
            domain_lower = (entry.get("domain") or "").lower()
            title_lower = (entry.get("title") or "").lower()

            # Check for exact substring matches first (highest priority)
            if query_lower in domain_lower or query_lower in title_lower:
                matches.append((100, entry))
            elif not exact_match:
                # Fuzzy matching only when exact_match is disabled
                from ..utils.fuzzy_search import calculate_ratio

                domain_score = calculate_ratio(query_lower, domain_lower)
                title_score = calculate_ratio(query_lower, title_lower)
                best_score = max(domain_score, title_score)

                if best_score >= 70:  # threshold for fuzzy matches
                    matches.append((best_score, entry))

        # Sort by score descending
        matches.sort(key=lambda x: x[0], reverse=True)
        return [match[1] for match in matches]

    @tool(
        name="ha_set_integration_enabled",
        tags={"Integrations"},
        annotations={"destructiveHint": True, "title": "Set Integration Enabled"},
    )
    @log_tool_usage
    async def ha_set_integration_enabled(
        self,
        entry_id: Annotated[str, Field(description="Config entry ID")],
        enabled: Annotated[
            bool | str, Field(description="True to enable, False to disable")
        ],
    ) -> dict[str, Any]:
        """Enable/disable integration (config entry).

        Use ha_get_integration() to find entry IDs.
        """
        try:
            enabled_bool = coerce_bool_param(enabled, "enabled")

            message = {
                "type": "config_entries/disable",
                "entry_id": entry_id,
                "disabled_by": None if enabled_bool else "user",
            }

            result = await self._client.send_websocket_message(message)

            if not result.get("success"):
                error_msg = result.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to {'enable' if enabled_bool else 'disable'} integration: {error_msg}",
                        context={"entry_id": entry_id},
                    )
                )

            # Get updated entry info
            require_restart = result.get("result", {}).get("require_restart", False)

            if require_restart:
                note = "Home Assistant restart required for changes to take effect."
            else:
                note = (
                    "Integration has been loaded."
                    if enabled_bool
                    else "Integration has been unloaded."
                )

            return {
                "success": True,
                "message": f"Integration {'enabled' if enabled_bool else 'disabled'} successfully",
                "entry_id": entry_id,
                "require_restart": require_restart,
                "note": note,
            }

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Failed to set integration enabled: {e}")
            exception_to_structured_error(e, context={"entry_id": entry_id})

    @tool(
        name="ha_delete_config_entry",
        tags={"Integrations"},
        annotations={"destructiveHint": True, "title": "Delete Config Entry"},
    )
    @log_tool_usage
    async def ha_delete_config_entry(
        self,
        entry_id: Annotated[str, Field(description="Config entry ID")],
        confirm: Annotated[
            bool | str, Field(description="Must be True to confirm deletion")
        ] = False,
    ) -> dict[str, Any]:
        """Delete config entry permanently. Requires confirm=True.

        Use ha_get_integration() to find entry IDs.
        """
        try:
            confirm_bool = coerce_bool_param(confirm, "confirm", default=False)

            if not confirm_bool:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Deletion not confirmed. Set confirm=True to proceed.",
                        context={
                            "entry_id": entry_id,
                            "warning": "This will permanently delete the config entry. This cannot be undone.",
                        },
                    )
                )

            result = await self._client.delete_config_entry(entry_id)
            require_restart = result.get("require_restart", False)

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

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Failed to delete config entry: {e}")
            exception_to_structured_error(e, context={"entry_id": entry_id})



    @tool(
        name="ha_delete_helpers_integrations",
        tags={"Helpers", "Integrations"},
        annotations={
            "destructiveHint": True,
            "title": "Delete Helper or Integration",
        },
    )
    @log_tool_usage
    async def ha_delete_helpers_integrations(
        self,
        target: Annotated[
            str,
            Field(
                description=(
                    "What to delete. One of: "
                    "(a) bare helper_id for SIMPLE helpers (requires helper_type), "
                    "e.g. 'my_button'; "
                    "(b) full entity_id (requires helper_type), "
                    "e.g. 'input_button.my_button' or 'sensor.my_meter'; "
                    "(c) config entry_id for any integration (helper_type=None), "
                    "e.g. value from ha_get_integration()."
                )
            ),
        ],
        helper_type: Annotated[
            Literal[
                # 12 SIMPLE
                "input_button", "input_boolean", "input_select", "input_number",
                "input_text", "input_datetime", "counter", "timer", "schedule",
                "zone", "person", "tag",
                # 15 FLOW
                "template", "group", "utility_meter", "derivative", "min_max",
                "threshold", "integration", "statistics", "trend", "random",
                "filter", "tod", "generic_thermostat", "switch_as_x",
                "generic_hygrostat",
            ]
            | None,
            Field(
                description=(
                    "Helper type. Required when target is a helper_id (bare) "
                    "or entity_id. Set to None when target is a config entry_id "
                    "to delete any integration."
                ),
                default=None,
            ),
        ] = None,
        confirm: Annotated[
            bool | str,
            Field(
                description="Must be True to confirm deletion.",
                default=False,
            ),
        ] = False,
        wait: Annotated[
            bool | str,
            Field(
                description=(
                    "Wait for entity removal. Default: True. "
                    "Ignored when helper_type=None (no entity poll, "
                    "require_restart returned)."
                ),
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Delete a Home Assistant helper or integration config entry.

        Unifies the previous ha_config_remove_helper (12 SIMPLE helper types)
        and ha_delete_config_entry (config entries) into a single tool with
        three routing paths driven by helper_type.

        SUPPORTED HELPER TYPES:
        - SIMPLE (12, websocket-delete): input_button, input_boolean,
          input_select, input_number, input_text, input_datetime, counter,
          timer, schedule, zone, person, tag.
        - FLOW (15, config-entry-delete via entity lookup): template, group,
          utility_meter, derivative, min_max, threshold, integration,
          statistics, trend, random, filter, tod, generic_thermostat,
          switch_as_x, generic_hygrostat.

        ROUTING:
        - SIMPLE helper_type + bare helper_id or entity_id → websocket delete.
        - FLOW helper_type + entity_id → resolve entity_id to config_entry_id
          via entity_registry, then delete the config entry. All sub-entities
          (e.g. utility_meter tariffs) are removed together.
        - helper_type=None + entry_id → direct config entry delete (any
          integration). Same outcome as the previous ha_delete_config_entry.

        EXAMPLES:
        - Delete SIMPLE button:
          ha_delete_helpers_integrations(
              target="my_button", helper_type="input_button", confirm=True
          )
        - Delete FLOW utility_meter (any sub-entity works):
          ha_delete_helpers_integrations(
              target="sensor.energy_peak",
              helper_type="utility_meter",
              confirm=True,
          )
        - Delete any integration by entry_id:
          ha_delete_helpers_integrations(
              target="01HXYZ...", confirm=True
          )

        **WARNING:** Deleting a helper or integration that is referenced by
        automations, scripts, or other integrations may cause those to fail.
        Use ha_search_entities() / ha_get_integration() to verify before
        deletion. Cannot be undone.

        NOTE: YAML-configured helpers cannot be deleted via this tool — they
        have no storage backend. Edit the YAML file and reload instead.
        """
        # === Confirm gate (uniform for all three paths) ===
        confirm_bool = coerce_bool_param(confirm, "confirm", default=False)
        if not confirm_bool:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Deletion not confirmed. Set confirm=True to proceed.",
                    context={
                        "target": target,
                        "helper_type": helper_type,
                        "warning": (
                            "This will permanently delete the helper or "
                            "integration. This cannot be undone."
                        ),
                    },
                )
            )

        wait_bool = coerce_bool_param(wait, "wait", default=True)
        assert wait_bool is not None  # default=True guarantees non-None
        client = self._client
        warnings: list[str] = []

        # === Routing dispatch ===
        if helper_type is None:
            # Path 3: Direct config entry delete (any integration)
            return await self._delete_direct_entry(target)

        if helper_type in SIMPLE_HELPER_TYPES:
            # Path 1: SIMPLE helper via websocket delete
            return await self._delete_simple_helper(
                helper_type, target, wait_bool
            )

        if helper_type in FLOW_HELPER_TYPES:
            # Path 2: FLOW helper via entity_id → config_entry_id lookup
            return await self._delete_flow_helper(
                helper_type, target, wait_bool, warnings
            )

        # Should be unreachable due to Literal type — defensive fallback
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Unknown helper_type: {helper_type!r}",
                context={"target": target, "helper_type": helper_type},
            )
        )

    # === Path 3: Direct config entry delete (1:1 from ha_delete_config_entry) ===
    async def _delete_direct_entry(self, entry_id: str) -> dict[str, Any]:
        """Delete a config entry directly. Mirrors ha_delete_config_entry."""
        try:
            result = await self._client.delete_config_entry(entry_id)
            require_restart = result.get("require_restart", False)
            return {
                "success": True,
                "action": "delete",
                "target": entry_id,
                "helper_type": "config_entry",
                "method": "config_entry_delete",
                "entry_id": entry_id,
                "entity_ids": [],
                "require_restart": require_restart,
                "message": (
                    "Config entry deleted successfully."
                    if not require_restart
                    else "Config entry deleted; Home Assistant restart required."
                ),
            }
        except ToolError:
            raise
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg or "not found" in error_msg.lower():
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Config entry not found: {entry_id}",
                        context={"entry_id": entry_id},
                        suggestions=[
                            "Use ha_get_integration() without entry_id to "
                            "see all config entries",
                        ],
                    )
                )
            logger.error(f"Failed to delete config entry: {e}")
            exception_to_structured_error(e, context={"entry_id": entry_id})

    # === Path 2: FLOW helper delete via entity_id → entry_id lookup ===
    async def _delete_flow_helper(
        self,
        helper_type: str,
        target: str,
        wait_bool: bool,
        warnings: list[str],
    ) -> dict[str, Any]:
        """Resolve target entity_id to config_entry_id, then delete entry.

        Multi-entity helpers (e.g. utility_meter with tariffs) are handled
        naturally — any sub-entity resolves to the same entry_id, and all
        sub-entities are waited for in parallel via asyncio.gather.
        """
        client = self._client
        try:
            # Step 1: resolve target → entry_id
            entry_id = await _get_entry_id_for_flow_helper(
                client, helper_type, target, warnings
            )
            if entry_id is None:
                # Distinguish two failure modes for accurate error reporting
                # (Z.1860 vs Z.192 convention in this codebase):
                # - entity not in registry → ENTITY_NOT_FOUND
                # - entity exists but no config_entry_id (YAML-configured)
                #   → RESOURCE_NOT_FOUND
                #
                # Re-query directly to disambiguate. This is one extra
                # WebSocket call only on the error path; the happy path
                # is unaffected.
                entity_id = (
                    target
                    if "." in target
                    else f"{helper_type}.{target}"
                )
                disambiguation = await client.send_websocket_message({
                    "type": "config/entity_registry/get",
                    "entity_id": entity_id,
                })
                in_registry = (
                    isinstance(disambiguation, dict)
                    and disambiguation.get("success")
                )
                if in_registry:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.RESOURCE_NOT_FOUND,
                            (
                                f"Helper {target} is not a storage-based "
                                "helper (no config entry). YAML-configured "
                                "helpers must be removed by editing the "
                                "configuration file."
                            ),
                            context={
                                "target": target,
                                "helper_type": helper_type,
                                "entity_id": entity_id,
                            },
                            suggestions=[
                                "Edit the YAML file and reload the relevant "
                                "integration.",
                            ],
                        )
                    )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.ENTITY_NOT_FOUND,
                        (
                            f"Helper {target} not found in entity registry "
                            f"(looked up as {entity_id})."
                        ),
                        context={
                            "target": target,
                            "helper_type": helper_type,
                            "entity_id": entity_id,
                        },
                        suggestions=[
                            "If unsure about the correct entity_id, use "
                            "ha_search_entities() — flow helper types often "
                            "expose entities under a different domain than "
                            "the helper_type itself (e.g. utility_meter → "
                            "sensor.*, switch_as_x → switch.* / light.*).",
                        ],
                    )
                )

            # Step 2: collect sub-entity IDs for the wait phase
            sub_entities = await _get_entities_for_config_entry(
                client, entry_id, warnings
            )
            entity_ids = [e["entity_id"] for e in sub_entities if "entity_id" in e]

            # Step 3: delete the config entry
            try:
                delete_result = await client.delete_config_entry(entry_id)
            except Exception as e:
                error_msg = str(e)
                if "404" in error_msg or "not found" in error_msg.lower():
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.RESOURCE_NOT_FOUND,
                            f"Config entry not found: {entry_id}",
                            context={
                                "entry_id": entry_id,
                                "target": target,
                                "helper_type": helper_type,
                            },
                        )
                    )
                raise

            require_restart = bool(
                isinstance(delete_result, dict)
                and delete_result.get("require_restart", False)
            )

            # Step 4: wait for all sub-entities to be removed in parallel
            response: dict[str, Any] = {
                "success": True,
                "action": "delete",
                "target": target,
                "helper_type": helper_type,
                "method": "config_flow_delete",
                "entry_id": entry_id,
                "entity_ids": entity_ids,
                "require_restart": require_restart,
                "message": (
                    f"Successfully deleted {helper_type} (entry: {entry_id}, "
                    f"{len(entity_ids)} sub-entities)."
                ),
            }
            if wait_bool and entity_ids:
                try:
                    results = await asyncio.gather(
                        *[
                            wait_for_entity_removed(client, eid)
                            for eid in entity_ids
                        ],
                        return_exceptions=True,
                    )
                    not_removed = [
                        eid
                        for eid, res in zip(entity_ids, results, strict=True)
                        if res is not True
                    ]
                    if not_removed:
                        response["warning"] = (
                            f"Deletion confirmed but the following entities "
                            f"may still appear briefly: {not_removed}"
                        )
                except Exception as e:
                    response["warning"] = (
                        f"Deletion confirmed but removal verification "
                        f"failed: {e}"
                    )
            if warnings:
                response["warnings"] = warnings
            return response

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "helper_type": helper_type,
                    "target": target,
                },
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify the target exists using ha_search_entities() "
                    "or ha_get_integration()",
                ],
            )

    # === Path 1: SIMPLE helper delete (1:1 from ha_config_remove_helper) ===
    async def _delete_simple_helper(
        self,
        helper_type: str,
        target: str,
        wait_bool: bool,
    ) -> dict[str, Any]:
        """Delete a SIMPLE helper via websocket. Mirrors ha_config_remove_helper.

        Preserves the 3-retry registry lookup with exponential backoff and
        the two fallback strategies (direct-id-delete, already-deleted check).
        """
        client = self._client
        # Convert to entity_id form
        entity_id = (
            target if target.startswith(f"{helper_type}.")
            else f"{helper_type}.{target}"
        )
        # Bare helper_id (without prefix) form for fallback strategies
        helper_id = (
            target.split(".", 1)[1]
            if target.startswith(f"{helper_type}.")
            else target
        )

        try:
            # Try to get unique_id with retry logic (race-condition guard)
            unique_id = None
            registry_result: dict[str, Any] | None = None
            max_retries = 3

            for attempt in range(max_retries):
                logger.info(
                    f"Getting entity registry for: {entity_id} "
                    f"(attempt {attempt + 1}/{max_retries})"
                )

                # Fast state check first
                try:
                    state_check = await client.get_entity_state(entity_id)
                    if not state_check:
                        if attempt < max_retries - 1:
                            wait_time = 0.5 * (2**attempt)
                            logger.debug(
                                f"Entity {entity_id} not in state, waiting "
                                f"{wait_time}s before retry..."
                            )
                            await asyncio.sleep(wait_time)
                            continue
                except Exception as e:
                    logger.debug(f"State check failed for {entity_id}: {e}")

                # Registry lookup
                registry_msg: dict[str, Any] = {
                    "type": "config/entity_registry/get",
                    "entity_id": entity_id,
                }
                try:
                    registry_result = await client.send_websocket_message(
                        registry_msg
                    )
                    if registry_result.get("success"):
                        entity_entry = registry_result.get("result", {})
                        unique_id = entity_entry.get("unique_id")
                        if unique_id:
                            logger.info(
                                f"Found unique_id: {unique_id} for {entity_id}"
                            )
                            break
                    if attempt < max_retries - 1:
                        wait_time = 0.5 * (2**attempt)
                        logger.debug(
                            f"Registry lookup failed for {entity_id}, "
                            f"waiting {wait_time}s before retry..."
                        )
                        await asyncio.sleep(wait_time)
                except Exception as e:
                    logger.warning(
                        f"Registry lookup attempt {attempt + 1} failed: {e}"
                    )
                    if attempt < max_retries - 1:
                        wait_time = 0.5 * (2**attempt)
                        await asyncio.sleep(wait_time)

            # Fallback strategy 1: direct-ID delete if unique_id not found
            if not unique_id:
                logger.info(
                    f"Could not find unique_id for {entity_id}, "
                    "trying direct deletion with helper_id"
                )
                delete_msg: dict[str, Any] = {
                    "type": f"{helper_type}/delete",
                    f"{helper_type}_id": helper_id,
                }
                logger.info(f"Sending fallback WebSocket delete: {delete_msg}")
                result = await client.send_websocket_message(delete_msg)

                if result.get("success"):
                    response: dict[str, Any] = {
                        "success": True,
                        "action": "delete",
                        "target": target,
                        "helper_type": helper_type,
                        "method": "websocket_delete",
                        "entry_id": None,
                        "entity_ids": [entity_id],
                        "require_restart": False,
                        "message": (
                            f"Successfully deleted {helper_type}: {target} "
                            f"using direct ID (entity: {entity_id})."
                        ),
                        "fallback_used": "direct_id",
                    }
                    if wait_bool:
                        try:
                            removed = await wait_for_entity_removed(
                                client, entity_id
                            )
                            if not removed:
                                response["warning"] = (
                                    f"Deletion confirmed but {entity_id} "
                                    "may still appear briefly."
                                )
                        except Exception as e:
                            response["warning"] = (
                                "Deletion confirmed but removal verification "
                                f"failed: {e}"
                            )
                    return response

                # Fallback strategy 2: already-deleted check
                try:
                    final_state_check = await client.get_entity_state(entity_id)
                    if not final_state_check:
                        logger.info(
                            f"Entity {entity_id} no longer exists; "
                            "treating as already deleted"
                        )
                        return {
                            "success": True,
                            "action": "delete",
                            "target": target,
                            "helper_type": helper_type,
                            "method": "websocket_delete",
                            "entry_id": None,
                            "entity_ids": [entity_id],
                            "require_restart": False,
                            "message": (
                                f"Helper {target} was already deleted or "
                                "never properly registered."
                            ),
                            "fallback_used": "already_deleted",
                        }
                except Exception:
                    pass

                # All fallbacks exhausted
                err_detail = (
                    registry_result.get("error", "Unknown error")
                    if registry_result
                    else "No registry response"
                )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.ENTITY_NOT_FOUND,
                        (
                            f"Helper not found in entity registry after "
                            f"{max_retries} attempts: {err_detail}"
                        ),
                        suggestions=[
                            "Helper may not be properly registered or was "
                            "already deleted. Use ha_search_entities() to "
                            "verify.",
                        ],
                        context={"target": target, "entity_id": entity_id},
                    )
                )

            # Standard path: delete using unique_id
            delete_message: dict[str, Any] = {
                "type": f"{helper_type}/delete",
                f"{helper_type}_id": unique_id,
            }
            logger.info(f"Sending WebSocket delete: {delete_message}")
            result = await client.send_websocket_message(delete_message)
            logger.info(f"WebSocket delete response: {result}")

            if result.get("success"):
                response = {
                    "success": True,
                    "action": "delete",
                    "target": target,
                    "helper_type": helper_type,
                    "method": "websocket_delete",
                    "entry_id": None,
                    "entity_ids": [entity_id],
                    "require_restart": False,
                    "unique_id": unique_id,
                    "message": (
                        f"Successfully deleted {helper_type}: {target} "
                        f"(entity: {entity_id})."
                    ),
                }
                if wait_bool:
                    try:
                        removed = await wait_for_entity_removed(
                            client, entity_id
                        )
                        if not removed:
                            response["warning"] = (
                                f"Deletion confirmed but {entity_id} "
                                "may still appear briefly."
                            )
                    except Exception as e:
                        response["warning"] = (
                            "Deletion confirmed but removal verification "
                            f"failed: {e}"
                        )
                return response

            # Standard path delete failed → SERVICE_CALL_FAILED
            error_msg = result.get("error", "Unknown error")
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to delete helper: {error_msg}",
                    suggestions=[
                        "Make sure the helper exists and is not being used "
                        "by automations or scripts",
                    ],
                    context={
                        "target": target,
                        "entity_id": entity_id,
                        "unique_id": unique_id,
                    },
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"helper_type": helper_type, "target": target},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify target exists using ha_search_entities()",
                    "Ensure helper is not used by automations or scripts",
                ],
            )

def register_integration_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register integration management tools with the MCP server."""
    register_tool_methods(mcp, IntegrationTools(client))
