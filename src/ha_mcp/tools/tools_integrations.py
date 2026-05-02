"""
Integration management tools for Home Assistant MCP server.

This module provides tools to list, enable, disable, and delete Home Assistant
integrations (config entries) via the REST and WebSocket APIs.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal, cast, get_args

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
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


FlowLookupReason = Literal[
    "ok",
    "wrong_helper_type",
    "bare_id_not_supported",
    "not_in_registry",
    "no_config_entry",
    "lookup_failed",
]


# Tool parameter type for ha_delete_helpers_integrations.helper_type.
# Must match SIMPLE_HELPER_TYPES | FLOW_HELPER_TYPES exactly — the drift
# assertion below catches accidental divergence at import time.
HelperTypeLiteral = Literal[
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
assert set(get_args(HelperTypeLiteral)) == (
    SIMPLE_HELPER_TYPES | FLOW_HELPER_TYPES
), (
    "HelperTypeLiteral drifted from SIMPLE_HELPER_TYPES | FLOW_HELPER_TYPES — "
    "update the inline list to match."
)


async def _get_entry_id_for_flow_helper(
    client: Any,
    helper_type: str,
    target: str,
    warnings: list[str] | None = None,
) -> tuple[str | None, FlowLookupReason]:
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
        Tuple of (config_entry_id, reason). On success: (entry_id, "ok").
        On failure: (None, reason) where reason discriminates the cause so
        the caller can produce an accurate error response without an extra
        WebSocket round-trip. HomeAssistantConnectionError and
        HomeAssistantAuthError propagate; the caller's outer except chain
        converts them to structured errors.
    """
    if helper_type not in FLOW_HELPER_TYPES:
        return None, "wrong_helper_type"

    if "." not in target:
        return None, "bare_id_not_supported"
    entity_id = target

    try:
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
    except (HomeAssistantConnectionError, HomeAssistantAuthError):
        # Typed errors must reach the outer handler — do not swallow.
        raise
    except Exception as e:
        logger.debug(f"entity_registry/get failed for {entity_id}: {e}")
        if warnings is not None:
            warnings.append(
                f"entity_registry/get failed for {entity_id}: {e}"
            )
        return None, "lookup_failed"

    if not isinstance(result, dict) or not result.get("success"):
        return None, "not_in_registry"

    entry = result.get("result") or {}
    if not isinstance(entry, dict):
        return None, "not_in_registry"

    config_entry_id = entry.get("config_entry_id")
    if not config_entry_id:
        return None, "no_config_entry"
    return config_entry_id, "ok"


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
            exception_to_structured_error(
                e,
                context={"entry_id": entry_id},
                suggestions=[
                    "Use ha_get_integration() without entry_id to see all "
                    "config entries",
                ],
            )

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
        name="ha_delete_helpers_integrations",
        tags={"Helper Entities", "Integrations"},
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
            HelperTypeLiteral | None,
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
                description=(
                    "Must be True to confirm deletion. Accepts bool or "
                    "string ('true'/'false'/'1'/'0'/'yes'/'no'/'on'/'off', "
                    "case-insensitive) for transport ergonomics."
                ),
                default=False,
            ),
        ] = False,
        wait: Annotated[
            bool | str,
            Field(
                description=(
                    "Wait for entity removal. Default: True. "
                    "Ignored when helper_type=None (no entity poll, "
                    "require_restart returned). Accepts bool or string "
                    "('true'/'false'/'1'/'0'/'yes'/'no'/'on'/'off', "
                    "case-insensitive)."
                ),
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Delete a Home Assistant helper or integration config entry.

        Combines simple-helper websocket deletion and config-entry deletion
        under one entry point with three routing paths driven by helper_type.

        WHEN NOT TO USE:
        - Removing only an entity (without deleting its underlying helper or
          config entry) — use `ha_remove_entity` instead.
        - YAML-configured helpers — they have no storage backend. Edit the
          YAML file and reload the relevant integration.

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
          integration).

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

        # default=True guarantees a non-None return; cast for mypy.
        wait_bool = cast(bool, coerce_bool_param(wait, "wait", default=True))
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

    # === Path 3: Direct config entry delete (any integration) ===
    async def _delete_direct_entry(self, entry_id: str) -> dict[str, Any]:
        """Delete a config entry directly via the websocket delete API."""
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
            logger.error(f"Failed to delete config entry: {e}")
            exception_to_structured_error(
                e,
                context={"entry_id": entry_id},
                suggestions=[
                    "Use ha_get_integration() without entry_id to "
                    "see all config entries",
                ],
            )

    # === Path 2: FLOW helper delete via entity_id → entry_id lookup ===
    async def _delete_flow_helper(
        self,
        helper_type: HelperTypeLiteral,
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
            # Step 1: resolve target → entry_id (typed reason on failure)
            entry_id, reason = await _get_entry_id_for_flow_helper(
                client, helper_type, target, warnings
            )
            if entry_id is None:
                # Reason discriminates the failure mode without a second
                # WebSocket round-trip. The lookup helper already queried
                # the registry; the response told us everything we need.
                entity_id = (
                    target
                    if "." in target
                    else f"{helper_type}.{target}"
                )
                if reason == "no_config_entry":
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
                if reason == "lookup_failed":
                    # Registry WebSocket call failed transiently. Surface as
                    # a connectivity error so the caller knows to retry,
                    # rather than chasing a non-existent entity_id.
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.WEBSOCKET_DISCONNECTED,
                            (
                                f"Registry lookup for {entity_id} failed "
                                "due to a WebSocket error."
                            ),
                            context={
                                "target": target,
                                "helper_type": helper_type,
                                "entity_id": entity_id,
                            },
                        )
                    )
                # Remaining reasons (not_in_registry, bare_id_not_supported,
                # wrong_helper_type) → entity not found from the caller's
                # perspective. wrong_helper_type cannot occur here because
                # the dispatcher already checked SIMPLE_HELPER_TYPES /
                # FLOW_HELPER_TYPES; the assertion below enforces that
                # contract at runtime.
                assert reason != "wrong_helper_type"
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
                exception_to_structured_error(
                    e,
                    context={
                        "entry_id": entry_id,
                        "target": target,
                        "helper_type": helper_type,
                    },
                )

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
                results = await asyncio.gather(
                    *[
                        wait_for_entity_removed(client, eid)
                        for eid in entity_ids
                    ],
                    return_exceptions=True,
                )
                # Auth/connection errors during polling must surface as
                # tool errors — wait_for_entity_removed re-raises these
                # deliberately. Re-raise the first one we find so the
                # outer except chain converts it to a structured error.
                for res in results:
                    if isinstance(
                        res, HomeAssistantConnectionError | HomeAssistantAuthError
                    ):
                        raise res
                not_removed = [
                    eid
                    for eid, res in zip(entity_ids, results, strict=True)
                    if res is not True
                ]
                if not_removed:
                    response["warning"] = (
                        f"Deletion confirmed but the following entities "
                        f"are still present after the wait window: "
                        f"{not_removed}"
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

    # === Path 1: SIMPLE helper delete via websocket ===
    async def _delete_simple_helper(
        self,
        helper_type: HelperTypeLiteral,
        target: str,
        wait_bool: bool,
    ) -> dict[str, Any]:
        """Delete a SIMPLE helper via the websocket {type}/delete API.

        Uses a 3-retry registry lookup with exponential backoff to find the
        helper's unique_id, then falls back to direct-id-delete and an
        already-deleted check if the registry has no record.
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
                except HomeAssistantAPIError as e:
                    # State check is best-effort here; an APIError (e.g. 404)
                    # is informational. Auth/connection errors must propagate
                    # so they're not re-reported as ENTITY_NOT_FOUND below.
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
                except HomeAssistantAPIError as e:
                    # APIError (e.g. 404) is informational and worth a retry.
                    # Auth/connection errors must propagate so they're not
                    # re-reported as ENTITY_NOT_FOUND in the fallback below.
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
                        removed = await wait_for_entity_removed(
                            client, entity_id
                        )
                        if not removed:
                            response["warning"] = (
                                f"Deletion confirmed but {entity_id} "
                                "is still present after the wait window."
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
                except HomeAssistantAPIError as e:
                    # 404 here means the state-check itself confirmed the
                    # entity is gone — treat as a soft signal and continue
                    # to the "all fallbacks exhausted" path. Auth/connection
                    # errors must propagate (handled by outer except).
                    logger.debug(
                        f"State check for {entity_id} raised APIError: {e}"
                    )

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
                    removed = await wait_for_entity_removed(
                        client, entity_id
                    )
                    if not removed:
                        response["warning"] = (
                            f"Deletion confirmed but {entity_id} "
                            "is still present after the wait window."
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
