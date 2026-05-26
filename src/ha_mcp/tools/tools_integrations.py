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
from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
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
    fetch_integration_diagnostics,
    get_logger_levels,
    parse_diagnostics_fields,
    wait_for_entity_removed,
    websocket_error_message,
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


# Tool parameter type for ha_remove_helpers_integrations.helper_type.
# Must match SIMPLE_HELPER_TYPES | FLOW_HELPER_TYPES plus config_subentry —
# the drift assertion below catches accidental divergence at import time.
HelperTypeLiteral = Literal[
    # 12 SIMPLE
    "input_button",
    "input_boolean",
    "input_select",
    "input_number",
    "input_text",
    "input_datetime",
    "counter",
    "timer",
    "schedule",
    "zone",
    "person",
    "tag",
    # config-entry subentries
    "config_subentry",
    # 15 FLOW
    "template",
    "group",
    "utility_meter",
    "derivative",
    "min_max",
    "threshold",
    "integration",
    "statistics",
    "trend",
    "random",
    "filter",
    "tod",
    "generic_thermostat",
    "switch_as_x",
    "generic_hygrostat",
]
assert set(get_args(HelperTypeLiteral)) == (
    SIMPLE_HELPER_TYPES | FLOW_HELPER_TYPES | {"config_subentry"}
), (
    "HelperTypeLiteral drifted from SIMPLE_HELPER_TYPES | FLOW_HELPER_TYPES "
    "| {'config_subentry'} — "
    "update the inline list to match."
)


async def _get_entry_id_for_flow_helper(
    client: Any,
    helper_type: str,
    target: str,
    warnings: list[str] | None = None,
) -> tuple[str | None, FlowLookupReason]:
    """Resolve a flow-helper target to its config_entry_id via entity_registry.

    Used by ha_remove_helpers_integrations when target is an entity_id
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
    except (OSError, TimeoutError) as e:
        # Network / transport errors from the WS layer (ConnectionError,
        # BrokenPipeError, TimeoutError, …). Programmer-bug-shape
        # exceptions (KeyError, AttributeError, TypeError) intentionally
        # propagate — the response is shape-checked at the dict guard
        # below, and a raise here would otherwise mask the bug as a
        # transient WEBSOCKET_DISCONNECTED.
        logger.debug(f"entity_registry/get failed for {entity_id}: {e}")
        if warnings is not None:
            warnings.append(f"entity_registry/get failed for {entity_id}: {e}")
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
        include_subentries: Annotated[
            bool | str,
            Field(
                description=(
                    "When entry_id is set, include config subentries for the "
                    "integration entry. Useful for integrations that expose "
                    "conversation agents, devices, or other extension points "
                    "as subentries."
                ),
                default=False,
            ),
        ] = False,
        include_subentry_schema: Annotated[
            bool | str,
            Field(
                description=(
                    "When entry_id is set, return introspection-only config "
                    "subentry schema information; no subentry is created. "
                    "Pair with subentry_type, and optionally subentry_id for "
                    "reconfigure schema."
                ),
                default=False,
            ),
        ] = False,
        subentry_type: Annotated[
            str | None,
            Field(
                description=(
                    "Integration-defined subentry type used with "
                    "include_subentry_schema=True."
                ),
                default=None,
            ),
        ] = None,
        subentry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Existing subentry ID used with include_subentry_schema=True "
                    "to inspect a reconfigure flow."
                ),
                default=None,
            ),
        ] = None,
        show_advanced_options: Annotated[
            bool | str,
            Field(
                description=(
                    "When include_subentry_schema=True, ask Home Assistant to "
                    "expose advanced flow options."
                ),
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
        include_diagnostics: Annotated[
            bool | str,
            Field(
                description=(
                    "When entry_id is set, also fetch the integration's diagnostics "
                    "dump — integration-defined JSON (commonly includes redacted "
                    "config, device list, state snapshots; exact top-level keys "
                    "vary by integration). The canonical artifact users grab via "
                    "Settings → Devices & Services → [integration] → ⋯ → Download "
                    "diagnostics. Use when triaging integration bugs or filing "
                    "ha_report_issue for a specific integration. Payloads can be "
                    "large (Hue ~290 KB, ZHA/MQTT/ESPHome several MB) — pair with "
                    "diagnostics_fields or diagnostics_truncate_at_bytes to fit "
                    "the LLM context budget."
                ),
                default=False,
            ),
        ] = False,
        device_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional. When set with include_diagnostics=True, returns the "
                    "device-scoped diagnostics dump for that specific device under "
                    "the integration (rather than the full integration dump). Some "
                    "integrations only expose config-entry-level dumps; others "
                    "expose both."
                ),
                default=None,
            ),
        ] = None,
        diagnostics_fields: Annotated[
            list[str] | str | None,
            Field(
                description=(
                    "Optional list of top-level keys to keep from the diagnostics "
                    "data payload (e.g. ['home_assistant', 'issues']). Trims the "
                    "payload before it hits the LLM context budget. Accepts a JSON "
                    "list or comma-separated string. Only applies when "
                    "include_diagnostics=True and the data payload is a dict. "
                    "Unknown keys are silently dropped and surfaced via the "
                    "omitted_fields sub-key."
                ),
                default=None,
            ),
        ] = None,
        diagnostics_truncate_at_bytes: Annotated[
            int | str | None,
            Field(
                description=(
                    "Optional byte cap on the serialized diagnostics payload "
                    "(after diagnostics_fields and diagnostics_data_path have "
                    "been applied). On hit, drops data and emits truncated=true, "
                    "bytes_total, byte_cap, plus available_fields (when the "
                    "capped value is a dict). Recommended starting point: "
                    "20000 bytes. Only applies when include_diagnostics=True."
                ),
                default=None,
            ),
        ] = None,
        diagnostics_data_path: Annotated[
            str | None,
            Field(
                description=(
                    "Optional dotted path into the diagnostics data sub-tree "
                    "(e.g. '<list-valued path>' for per-device records, "
                    "'home_assistant.version' for HA core version; the exact "
                    "key path varies by integration version). Walks into the "
                    "post-fields payload. Resolution failures replace data "
                    "with null and surface data_path_error. Use this when the "
                    "interesting payload lives several levels deep — top-level "
                    "diagnostics_fields can't address sub-trees on integrations "
                    "where the bulk lives under one key (ZHA, MQTT, ESPHome). "
                    "Only applies when include_diagnostics=True."
                ),
                default=None,
            ),
        ] = None,
        diagnostics_data_offset: Annotated[
            int | str | None,
            Field(
                description=(
                    "Pagination start index (default 0) for list-valued "
                    "diagnostics_data_path results. Ignored when "
                    "diagnostics_data_path is unset, diagnostics_data_limit is "
                    "unset, or the resolved value is not a list. Only applies "
                    "when include_diagnostics=True."
                ),
                default=0,
            ),
        ] = 0,
        diagnostics_data_limit: Annotated[
            int | str | None,
            Field(
                description=(
                    "Pagination window size for list-valued "
                    "diagnostics_data_path results. When set with a "
                    "list-resolved path, swaps data for a pagination envelope "
                    "{path, items, offset, limit, total, has_more}. Default "
                    "None returns the full resolved value. Workflow: probe "
                    "with a list-valued diagnostics_data_path and "
                    "diagnostics_data_limit=10 to walk a large list one page "
                    "at a time (the exact path varies by integration version). "
                    "Only applies when include_diagnostics=True."
                ),
                default=None,
            ),
        ] = None,
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
        - Get entry with diagnostics dump: ha_get_integration(entry_id="abc123", include_diagnostics=True)
        - Get device-scoped diagnostics: ha_get_integration(entry_id="abc123", include_diagnostics=True, device_id="dev123")
        - Walk a sub-tree: ha_get_integration(entry_id="abc123", include_diagnostics=True, diagnostics_data_path="<dotted-path>")
        - Paginate a large list: ha_get_integration(entry_id="abc123", include_diagnostics=True, diagnostics_data_path="<list-valued path>", diagnostics_data_limit=10, diagnostics_data_offset=20)
        - List config subentries: ha_get_integration(entry_id="abc123", include_subentries=True)
        - Inspect subentry create schema: ha_get_integration(entry_id="abc123", include_subentry_schema=True, subentry_type="conversation")
        - Inspect subentry reconfigure schema: ha_get_integration(entry_id="abc123", include_subentry_schema=True, subentry_type="conversation", subentry_id="sub123")
        - List template entries: ha_get_integration(domain="template")

        STATES: 'loaded', 'setup_error', 'setup_retry', 'not_loaded',
        'failed_unload', 'migration_error'.

        Each entry carries:

        - ``log_level``: the canonical Python logger level name
          (``DEBUG``/``INFO``/``WARNING``/``ERROR``/``CRITICAL``) when the
          integration has a ``logger.set_level`` override, or ``"DEFAULT"``
          (uppercase sentinel) when no override is set.
        - ``log_level_raw``: the original numeric level (e.g. ``10`` for DEBUG)
          when HA returned an int, ``None`` otherwise (no override set, or HA
          provided a level name as a string).

        This is distinct from the add-on side, where ``ha_get_addon`` returns
        Supervisor's lowercase ``"default"`` literal — do not cross-compare.
        """
        try:
            include_opts = coerce_bool_param(
                include_options, "include_options", default=False
            )
            include_schema_bool = coerce_bool_param(
                include_schema, "include_schema", default=False
            )
            include_diagnostics_bool = coerce_bool_param(
                include_diagnostics, "include_diagnostics", default=False
            )
            include_subentries_bool = cast(
                bool,
                coerce_bool_param(
                    include_subentries, "include_subentries", default=False
                ),
            )
            include_subentry_schema_bool = cast(
                bool,
                coerce_bool_param(
                    include_subentry_schema,
                    "include_subentry_schema",
                    default=False,
                ),
            )
            show_advanced_options_bool = cast(
                bool,
                coerce_bool_param(
                    show_advanced_options,
                    "show_advanced_options",
                    default=False,
                ),
            )
            exact_match_bool = coerce_bool_param(
                exact_match, "exact_match", default=True
            )
            limit_int = coerce_int_param(
                limit, "limit", default=50, min_value=1, max_value=200
            )
            offset_int = coerce_int_param(offset, "offset", default=0, min_value=0)
            fields_list = parse_diagnostics_fields(diagnostics_fields)
            truncate_bytes = coerce_int_param(
                diagnostics_truncate_at_bytes,
                "diagnostics_truncate_at_bytes",
                default=None,
                min_value=1,
            )
            data_offset_int = coerce_int_param(
                diagnostics_data_offset,
                "diagnostics_data_offset",
                default=0,
                min_value=0,
            )
            data_limit_int = coerce_int_param(
                diagnostics_data_limit,
                "diagnostics_data_limit",
                default=None,
                min_value=1,
            )
            # Type-guard ``diagnostics_data_path`` here so a bad caller (dict /
            # list) surfaces as ``VALIDATION_INVALID_PARAMETER`` instead of
            # leaking as ``INTERNAL_ERROR`` from the resolver's ``.strip()``
            # downstream. Mirrors the coerce_int_param guards above.
            if diagnostics_data_path is not None and not isinstance(
                diagnostics_data_path, str
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "diagnostics_data_path must be a string, got "
                        f"{type(diagnostics_data_path).__name__}",
                        context={"parameter": "diagnostics_data_path"},
                    )
                )
            # Auto-enable options when domain filter is set
            if domain is not None:
                include_opts = True

            # If entry_id provided, get specific config entry
            if entry_id is not None:
                resp = await self._get_single_entry(
                    entry_id,
                    include_schema_bool,
                    include_subentries=include_subentries_bool
                    or include_subentry_schema_bool,
                    include_subentry_schema=include_subentry_schema_bool,
                    subentry_type=subentry_type,
                    subentry_id=subentry_id,
                    show_advanced_options=show_advanced_options_bool,
                )
                if include_diagnostics_bool:
                    resp["diagnostics"] = await fetch_integration_diagnostics(
                        self._client,
                        entry_id,
                        device_id,
                        fields=fields_list,
                        truncate_at_bytes=truncate_bytes,
                        data_path=diagnostics_data_path,
                        data_offset=data_offset_int,
                        data_limit=data_limit_int,
                    )
                elif device_id is not None:
                    resp.setdefault("warnings", []).append(
                        "device_id was provided but ignored because "
                        "include_diagnostics=False"
                    )
                return resp

            # List mode - get all config entries
            result = await self._list_entries(
                domain, query, include_opts, exact_match_bool, limit_int, offset_int
            )
            ignored_detail_params = []
            if include_diagnostics_bool:
                ignored_detail_params.append("include_diagnostics")
            if device_id is not None:
                ignored_detail_params.append("device_id")
            if fields_list is not None:
                ignored_detail_params.append("diagnostics_fields")
            if truncate_bytes is not None:
                ignored_detail_params.append("diagnostics_truncate_at_bytes")
            if diagnostics_data_path is not None:
                ignored_detail_params.append("diagnostics_data_path")
            if data_offset_int > 0:
                ignored_detail_params.append("diagnostics_data_offset")
            if data_limit_int is not None:
                ignored_detail_params.append("diagnostics_data_limit")
            if include_subentries_bool:
                ignored_detail_params.append("include_subentries")
            if include_subentry_schema_bool:
                ignored_detail_params.append("include_subentry_schema")
            if subentry_type is not None:
                ignored_detail_params.append("subentry_type")
            if subentry_id is not None:
                ignored_detail_params.append("subentry_id")
            if show_advanced_options_bool:
                ignored_detail_params.append("show_advanced_options")
            if ignored_detail_params:
                result.setdefault("warnings", []).append(
                    f"{', '.join(ignored_detail_params)} "
                    "ignored because entry_id was not set (list mode)"
                )
            return result

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
        self,
        entry_id: str,
        include_schema: bool | None,
        *,
        include_subentries: bool,
        include_subentry_schema: bool,
        subentry_type: str | None,
        subentry_id: str | None,
        show_advanced_options: bool,
    ) -> dict[str, Any]:
        """Fetch a single config entry by ID, optionally including its options schema."""
        try:
            result = await self._client.get_config_entry(entry_id)
            entry_domain = result.get("domain") if isinstance(result, dict) else None

            # Surface `options` on every per-entry response (HA's REST endpoint
            # omits the field). For entries with supports_options=True we probe
            # via OptionsFlow — see `_fetch_entry_options`. When include_schema
            # is also requested, `_fetch_options_schema` below populates options
            # from the same flow init so we don't pay for two round-trips.
            if isinstance(result, dict):
                result.setdefault("options", {})
                if result.get("supports_options") and not include_schema:
                    result["options"] = await self._fetch_entry_options(entry_id)

            resp: dict[str, Any] = {
                "success": True,
                "entry_id": entry_id,
                "entry": result,
            }

            # Surface the effective Python logger level for this integration
            # so users can confirm logger.set_level changes took effect.
            # Emit unconditionally for symmetry with the list path (_format_entry).
            logger_levels = await get_logger_levels(self._client)
            level_info = logger_levels.get(entry_domain or "")
            resp["log_level"] = level_info["name"] if level_info else "DEFAULT"
            resp["log_level_raw"] = level_info["raw"] if level_info else None

            # Optionally fetch options flow schema (logically read-only: start+abort)
            if include_schema and result.get("supports_options"):
                await self._fetch_options_schema(entry_id, resp)

            if include_subentries:
                subentries = await self._fetch_config_subentries(entry_id)
                resp["subentry_count"] = len(subentries)
                resp["subentries"] = subentries

            if include_subentry_schema:
                await self._fetch_config_subentry_schema(
                    entry_id,
                    resp,
                    subentry_type=subentry_type,
                    subentry_id=subentry_id,
                    show_advanced_options=show_advanced_options,
                )

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

    async def _fetch_config_subentries(self, entry_id: str) -> list[dict[str, Any]]:
        """Fetch config subentries for a detailed entry response."""
        result = await self._client.list_config_subentries(entry_id)
        if not isinstance(result, dict) or not result.get("success"):
            error_msg = websocket_error_message(result.get("error", "Operation failed"))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list config subentries: {error_msg}",
                    context={"entry_id": entry_id},
                )
            )

        subentries = result.get("result")
        if not isinstance(subentries, list):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected config subentry list response",
                    context={"entry_id": entry_id, "details": result},
                )
            )

        return [subentry for subentry in subentries if isinstance(subentry, dict)]

    async def _fetch_config_subentry_schema(
        self,
        entry_id: str,
        resp: dict[str, Any],
        *,
        subentry_type: str | None,
        subentry_id: str | None,
        show_advanced_options: bool,
    ) -> None:
        """Start a config subentry flow to read its schema, then abort it."""
        if not subentry_type:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "subentry_type is required when include_subentry_schema=True",
                    suggestions=[
                        "Call ha_get_integration(entry_id=..., "
                        "include_subentries=True) to inspect existing subentry "
                        "types, then retry with subentry_type.",
                    ],
                    context={"entry_id": entry_id},
                )
            )

        flow_id = None
        try:
            flow_result = await self._client.start_config_subentry_flow(
                entry_id,
                subentry_type,
                subentry_id=subentry_id,
                show_advanced_options=show_advanced_options,
            )
            flow_id = flow_result.get("flow_id")
            flow_type = flow_result.get("type")
            schema: dict[str, Any] = {
                "subentry_type": subentry_type,
                "subentry_id": subentry_id,
                "flow_type": flow_type,
                "step_id": flow_result.get("step_id"),
                "description_placeholders": flow_result.get(
                    "description_placeholders", {}
                ),
            }
            if flow_type == "form":
                schema["data_schema"] = flow_result.get("data_schema", [])
            elif flow_type == "menu":
                schema["menu_options"] = flow_result.get("menu_options", [])
            else:
                schema["details"] = flow_result
            resp["subentry_schema"] = schema
        finally:
            if flow_id:
                try:
                    await asyncio.wait_for(
                        self._client.abort_config_subentry_flow(flow_id),
                        timeout=5.0,
                    )
                except Exception as abort_err:
                    logger.warning(
                        "Failed to abort config subentry introspection flow %s: %s",
                        flow_id,
                        abort_err,
                    )

    @staticmethod
    def _options_from_form_flow(flow: dict[str, Any]) -> dict[str, Any]:
        """Extract ``{field_name: current_value}`` from a form-type OptionsFlow.

        Reads each ``data_schema`` entry's ``default`` key, falling back to
        ``value`` only when the ``default`` key is absent (constant-type
        fields ship ``value`` instead of ``default``). Fields with a missing
        or ``None`` value are skipped.
        """
        out: dict[str, Any] = {}
        for field in flow.get("data_schema") or []:
            name = field.get("name")
            if name is None:
                continue
            value = field.get("default", field.get("value"))
            if value is not None:
                out[name] = value
        return out

    async def _fetch_entry_options(self, entry_id: str) -> dict[str, Any]:
        """Read the current ``options`` for a config entry via its OptionsFlow.

        Home Assistant does not expose ``ConfigEntry.options`` through any
        read-only REST or WebSocket endpoint — ``/api/config/config_entries/entry``
        deliberately omits the field. The closest approximation that the HA UI
        itself uses is the ``default`` values populated into the OptionsFlow's
        first-step ``data_schema``: integrations build that schema from the
        existing options dict, so the defaults match the persisted state.

        Starts the flow, harvests ``{name: default}`` from the first step,
        and aborts the flow in ``finally`` so it doesn't sit half-open.

        Returns ``{}`` on any failure (unsupported entry, non-form first step
        such as a menu, init/abort errors) so callers can treat the return as
        the canonical "options" field without further checks. Unexpected
        exception types are logged at ``warning`` so probe breakage is
        discoverable.
        """
        flow_id: str | None = None
        try:
            flow = await self._client.start_options_flow(entry_id)
            flow_id = flow.get("flow_id")
            flow_type = flow.get("type")
            if flow_type != "form":
                logger.debug(
                    f"OptionsFlow for {entry_id} returned type={flow_type!r}, "
                    f"not a form — cannot extract option defaults"
                )
                return {}
            return self._options_from_form_flow(flow)
        except Exception as exc:
            logger.warning(
                f"Failed to fetch options for {entry_id}: {type(exc).__name__}: {exc}"
            )
            return {}
        finally:
            if flow_id:
                try:
                    await self._client.abort_options_flow(flow_id)
                except Exception as abort_err:
                    logger.warning(
                        f"Failed to abort options flow {flow_id}: "
                        f"{type(abort_err).__name__}: {abort_err}"
                    )

    async def _fetch_options_schema(self, entry_id: str, resp: dict[str, Any]) -> None:
        """Start an options flow to read the schema, then abort it.

        Also populates ``resp["entry"]["options"]`` for form-type flows from
        the same flow result so callers requesting both schema and options
        don't pay for two round-trips.
        """
        flow_id = None
        try:
            flow_result = await self._client.start_options_flow(entry_id)
            flow_id = flow_result.get("flow_id")
            flow_type = flow_result.get("type")
            entry = resp.get("entry") if isinstance(resp.get("entry"), dict) else None
            if flow_type == "form":
                resp["options_schema"] = {
                    "flow_type": "form",
                    "step_id": flow_result.get("step_id"),
                    "data_schema": flow_result.get("data_schema", []),
                }
                if entry is not None:
                    entry["options"] = self._options_from_form_flow(flow_result)
            elif flow_type == "menu":
                resp["options_schema"] = {
                    "flow_type": "menu",
                    "step_id": flow_result.get("step_id"),
                    "menu_options": flow_result.get("menu_options", []),
                }
        except Exception as schema_err:
            logger.warning(
                f"Failed to fetch options schema for {entry_id}: "
                f"{type(schema_err).__name__}: {schema_err}"
            )
        finally:
            if flow_id:
                try:
                    await self._client.abort_options_flow(flow_id)
                except Exception as abort_err:
                    logger.warning(
                        f"Failed to abort options flow {flow_id}: "
                        f"{type(abort_err).__name__}: {abort_err}"
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

        # Fetch current logger levels once; enrich each entry with its effective level.
        logger_levels = await get_logger_levels(self._client)

        # `_format_entry` is sync and cannot probe the OptionsFlow; options
        # are filled in by a second async pass below for entries that
        # advertise supports_options=True. See `_fetch_entry_options`.
        formatted_entries = [
            self._format_entry(entry, include_opts, logger_levels) for entry in entries
        ]

        if include_opts:
            options_targets = [
                e for e in formatted_entries if e.get("supports_options")
            ]
            if options_targets:
                fetched = await asyncio.gather(
                    *(
                        self._fetch_entry_options(e["entry_id"])
                        for e in options_targets
                    ),
                    return_exceptions=False,
                )
                for entry, opts in zip(options_targets, fetched, strict=True):
                    entry["options"] = opts

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
    def _format_entry(
        entry: dict[str, Any],
        include_opts: bool | None,
        logger_levels: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
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

        # Surface the effective Python logger level for this integration
        # ("DEFAULT" = no override; falls back to the root logger level).
        # `log_level_raw` is the original numeric level (None when no override
        # exists or HA returned a string instead of an int).
        if logger_levels is not None:
            domain = entry.get("domain") or ""
            level_info = logger_levels.get(domain)
            formatted_entry["log_level"] = (
                level_info["name"] if level_info else "DEFAULT"
            )
            formatted_entry["log_level_raw"] = level_info["raw"] if level_info else None

        # Include options when requested (for auditing template definitions, etc.)
        if include_opts:
            formatted_entry["options"] = entry.get("options", {})

        # Include pref_disable_new_entities and pref_disable_polling if present
        if "pref_disable_new_entities" in entry:
            formatted_entry["pref_disable_new_entities"] = entry[
                "pref_disable_new_entities"
            ]
        if "pref_disable_polling" in entry:
            formatted_entry["pref_disable_polling"] = entry["pref_disable_polling"]

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
    @with_auto_backup(domain="integration", id_param="entry_id")
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
            # Empty/whitespace entry_id would surface as a misleading HA
            # "config entry not found" from ``config_entries/disable``.
            validate_identifier_not_empty(
                entry_id,
                "entry_id",
                suggestions=[
                    "Use ha_get_integration() to find valid config entry IDs",
                ],
            )
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
        name="ha_remove_helpers_integrations",
        tags={"Helper Entities", "Integrations"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Helper or Integration",
        },
    )
    @with_auto_backup(
        # ``target`` is one of three shapes: a flow-helper entity_id like
        # ``sensor.my_meter`` (routes through the matching ``helper_<type>``
        # domain when ``helper_type`` is also passed), a bare config-entry
        # id, or a parent.subentry pair. Dispatch to ``helper_<type>``
        # when the kw is supplied so storage-backed helpers (input_*,
        # counter, timer, ...) get a snapshot via the same handler the
        # ``ha_config_set_helper`` decorator uses; otherwise fall back to
        # the integration domain.
        domain_fn=lambda kw: (
            f"helper_{kw['helper_type']}" if kw.get("helper_type") else "integration"
        ),
        id_param="target",
    )
    @log_tool_usage
    async def ha_remove_helpers_integrations(
        self,
        target: Annotated[
            str,
            Field(
                description=(
                    "What to remove. One of: "
                    "(a) bare helper_id for SIMPLE helpers (requires helper_type), "
                    "e.g. 'my_button'; "
                    "(b) full entity_id (requires helper_type), "
                    "e.g. 'input_button.my_button' or 'sensor.my_meter'; "
                    "(c) config entry_id for any integration (helper_type=None), "
                    "e.g. value from ha_get_integration(); "
                    "(d) parent config entry_id for config_subentry "
                    "(requires helper_type='config_subentry' and subentry_id)."
                )
            ),
        ],
        helper_type: Annotated[
            HelperTypeLiteral | None,
            Field(
                description=(
                    "Helper type. Required when target is a helper_id (bare) "
                    "or entity_id. Set to None when target is a config entry_id "
                    "to remove any integration. Use 'config_subentry' to remove "
                    "a config subentry under target."
                ),
                default=None,
            ),
        ] = None,
        subentry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Config subentry ID to remove when helper_type='config_subentry'."
                ),
                default=None,
            ),
        ] = None,
        confirm: Annotated[
            bool | str,
            Field(
                description=(
                    "Must be True to confirm removal. Accepts bool or "
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
                    "Ignored when helper_type=None or "
                    "helper_type='config_subentry' (no entity poll, "
                    "require_restart returned). Accepts bool or string "
                    "('true'/'false'/'1'/'0'/'yes'/'no'/'on'/'off', "
                    "case-insensitive)."
                ),
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Remove a Home Assistant helper or integration config entry.

        Unifies three backend removal mechanisms — simple-helper websocket
        delete, config-entry delete, and config-subentry delete — behind one
        entry point with four routing paths driven by helper_type.

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
        - helper_type="config_subentry" + parent entry_id + subentry_id →
          delete one config subentry.

        MISSING-TARGET CONTRACT:
        A target that is *confirmed absent* raises a structured error
        rather than returning silent success, so a typo'd or stale
        identifier surfaces immediately at the caller layer (the
        ``success`` boolean is what agent wrappers branch on). The
        error code per-path follows the target shape:
        - SIMPLE (bare helper_id or entity_id): state-machine empty AND
          entity registry empty → raises ``ENTITY_NOT_FOUND``.
        - FLOW (entity_id): not in entity registry → raises
          ``ENTITY_NOT_FOUND``. YAML-configured helpers (no config entry
          backing) raise ``RESOURCE_NOT_FOUND``. A bare helper_id (no
          ``.``) on a FLOW target raises ``ENTITY_NOT_FOUND`` — FLOW
          resolution needs a full entity_id. TOCTOU 404 on the
          resolved entry_id raises ``RESOURCE_NOT_FOUND``.
        - Direct config entry (helper_type=None): backend returns HTTP
          404 → raises ``RESOURCE_NOT_FOUND``.
        - Config subentry: backend returns a "not_found" error → raises
          ``RESOURCE_NOT_FOUND``.

        Idempotency at the contract level still holds (call N times =
        same response). Transient connectivity failures (WebSocket
        disconnected, network timeouts) raise their own codes
        (``WEBSOCKET_DISCONNECTED``, ``CONNECTION_FAILED``) so retry
        logic can branch separately.

        EXAMPLES:
        - Remove SIMPLE button:
          ha_remove_helpers_integrations(
              target="my_button", helper_type="input_button", confirm=True
          )
        - Remove FLOW utility_meter (any sub-entity works):
          ha_remove_helpers_integrations(
              target="sensor.energy_peak",
              helper_type="utility_meter",
              confirm=True,
          )
        - Remove any integration by entry_id:
          ha_remove_helpers_integrations(
              target="01HXYZ...", confirm=True
          )
        - Remove a config subentry:
          ha_remove_helpers_integrations(
              target="01HXYZ...", helper_type="config_subentry",
              subentry_id="subentry-123", confirm=True
          )

        **WARNING:** Removing a helper or integration that is referenced by
        automations, scripts, or other integrations may cause those to fail.
        Use ha_search_entities() / ha_get_integration() to verify before
        removal. Cannot be undone.
        """
        # === Confirm gate (uniform for all four paths) ===
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

        # === Empty/whitespace target gate (uniform for all four paths) ===
        # Empty/whitespace ``target`` would reach the destructive backend call
        # on every path: Path 1 (simple-helper websocket delete), Path 2
        # (flow-helper entity-resolution → entry_id delete), Path 3
        # (_delete_direct_entry → client.delete_config_entry("")), Path 4
        # (_delete_config_subentry → ws delete on empty parent entry_id).
        # Each path surfaces a different misleading error from HA. Reject
        # up-front so the caller learns the identifier was unusable before
        # any backend call.
        validate_identifier_not_empty(
            target,
            "target",
            suggestions=[
                "Use ha_get_integration() to find valid entry_ids",
                "For simple helpers, use ha_search_entities() to find the helper_id",
                "For flow helpers, use ha_search_entities() to find an entity_id",
            ],
            context={"helper_type": helper_type},
        )

        wait_bool = coerce_bool_param(wait, "wait", default=True)
        warnings: list[str] = []

        # === Routing dispatch ===
        if helper_type is None:
            # Path 3: Direct config entry delete (any integration)
            return await self._delete_direct_entry(target)

        if helper_type == "config_subentry":
            # Path 4: Delete one subentry under a parent config entry
            subentry_id = validate_identifier_not_empty(
                subentry_id,
                "subentry_id",
                suggestions=[
                    "Use ha_get_integration(entry_id=..., include_subentries=True) "
                    "to find subentry IDs",
                ],
                context={"target": target, "helper_type": helper_type},
            )
            return await self._delete_config_subentry(target, subentry_id)

        if helper_type in SIMPLE_HELPER_TYPES:
            # Path 1: SIMPLE helper via websocket delete
            return await self._delete_simple_helper(helper_type, target, wait_bool)

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

    # Private helpers keep the ``_delete_*`` prefix because they wrap HA's
    # own backend verb — the WebSocket API is ``<type>/delete`` and the
    # REST API is HTTP DELETE. The public tool surface uses ``remove`` to
    # join the ``ha_remove_*`` behavioural family; the prefix asymmetry is
    # intentional and prevents future renames pulled by either side.

    # === Path 3: Direct config entry delete (any integration) ===
    async def _delete_direct_entry(self, entry_id: str) -> dict[str, Any]:
        """Delete a config entry directly via the REST delete API."""
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
        except HomeAssistantAPIError as e:
            # HA returns 404 for missing config entries (see
            # RestClient.delete_config_entry — the REST DELETE on a
            # nonexistent entry surfaces as HomeAssistantAPIError with
            # status_code=404). Surface as RESOURCE_NOT_FOUND so callers
            # can distinguish absent target from real failures; the typo
            # case (agent passed the wrong entry_id) is the failure mode
            # that "absent → success" would silently mask. Non-404 API
            # errors are real failures and bubble through
            # exception_to_structured_error below.
            if e.status_code == 404:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        (
                            f"Config entry {entry_id} not found. May "
                            "indicate it was already removed, never "
                            "existed, or the identifier is a typo. "
                            "Verify with ha_get_integration() before "
                            "retrying."
                        ),
                        context={"entry_id": entry_id},
                        suggestions=[
                            "Use ha_get_integration() without entry_id "
                            "to see all config entries",
                        ],
                    )
                )
            exception_to_structured_error(
                e,
                context={"entry_id": entry_id},
                suggestions=[
                    "Use ha_get_integration() without entry_id to "
                    "see all config entries",
                ],
            )
        except Exception as e:
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
                entity_id = target if "." in target else f"{helper_type}.{target}"
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
                # wrong_helper_type cannot occur here because the dispatcher
                # already checked SIMPLE_HELPER_TYPES / FLOW_HELPER_TYPES; the
                # assertion enforces that contract at runtime.
                assert reason != "wrong_helper_type"
                if reason == "not_in_registry":
                    # Target is absent from the entity registry. Surface
                    # as ENTITY_NOT_FOUND (entity-shaped target) so the
                    # caller learns the identifier is unusable — the typo
                    # case is the failure mode "absent → success" would
                    # silently mask. Matches the bare_id_not_supported
                    # branch below and sibling ha_remove_entity.
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.ENTITY_NOT_FOUND,
                            (
                                f"Helper {target} not found in entity "
                                f"registry (looked up as {entity_id}). "
                                "May indicate it was already removed, "
                                "never existed, or the identifier is a "
                                "typo. Verify with ha_search_entities() "
                                "before retrying."
                            ),
                            context={
                                "target": target,
                                "helper_type": helper_type,
                                "entity_id": entity_id,
                            },
                            suggestions=[
                                "Use ha_search_entities() — flow helper "
                                "types often expose entities under a "
                                "different domain than the helper_type "
                                "itself (e.g. utility_meter → sensor.*, "
                                "switch_as_x → switch.* / light.*).",
                            ],
                        )
                    )
                # bare_id_not_supported → caller passed a bare ID where an
                # entity_id was required. That's a call-shape error, not
                # missing-target; surface as ENTITY_NOT_FOUND with the
                # search suggestion so the caller can self-correct.
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
            except HomeAssistantAPIError as e:
                # TOCTOU window: entry_id resolved at step 1 was deleted
                # before step 3 reached HA. Surface as RESOURCE_NOT_FOUND
                # so the caller knows the config entry is gone — silent
                # success would hide the race from any wrapper that
                # acted on the intermediate state. Non-404 still surfaces.
                if e.status_code == 404:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.RESOURCE_NOT_FOUND,
                            (
                                f"Config entry {entry_id} for {target} "
                                "not found at delete time (resolved by "
                                "registry but absent when DELETE reached "
                                "Home Assistant). May indicate a "
                                "concurrent removal."
                            ),
                            context={
                                "entry_id": entry_id,
                                "target": target,
                                "helper_type": helper_type,
                            },
                        )
                    )
                exception_to_structured_error(
                    e,
                    context={
                        "entry_id": entry_id,
                        "target": target,
                        "helper_type": helper_type,
                    },
                )
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
                    *[wait_for_entity_removed(client, eid) for eid in entity_ids],
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
                    response.setdefault("warnings", []).append(
                        f"Deletion confirmed but the following entities "
                        f"are still present after the wait window: "
                        f"{not_removed}"
                    )
            if warnings:
                response.setdefault("warnings", []).extend(warnings)
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

    async def _delete_config_subentry(
        self, entry_id: str, subentry_id: str
    ) -> dict[str, Any]:
        """Delete one config subentry under a parent config entry."""
        result = await self._client.delete_config_subentry(entry_id, subentry_id)
        if not isinstance(result, dict) or not result.get("success"):
            error = result.get("error", "Operation failed")
            error_msg = websocket_error_message(error)
            # Detect "subentry already absent" by HA's structured
            # ``code="not_found"`` only. A generic ``"not found" in
            # error_msg`` substring match was rejected because it can
            # collide with unrelated HA error messages (e.g.
            # "repository not found", "integration not found") and
            # mis-classify a real failure as a missing target.
            # The HA ``config_entries/subentries/delete`` handler raises
            # with ``code="not_found"`` when entry_id or subentry_id is
            # missing; if a future HA version uses a different code, we
            # raise SERVICE_CALL_FAILED instead — safer than mis-classifying.
            error_code = error.get("code") if isinstance(error, dict) else None
            if error_code == "not_found":
                # Subentry absent under the parent config entry. Surface
                # as RESOURCE_NOT_FOUND so the caller learns the target
                # didn't exist — silent success would mask a typo'd
                # subentry_id (or wrong parent entry_id) until the user
                # noticed nothing was removed.
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        (
                            f"Subentry {subentry_id} not found under "
                            f"config entry {entry_id}. May indicate it "
                            "was already removed, never existed, or one "
                            "of the identifiers is a typo. Verify with "
                            "ha_get_integration(entry_id=..., "
                            "include_subentries=True) before retrying."
                        ),
                        context={
                            "entry_id": entry_id,
                            "subentry_id": subentry_id,
                        },
                    )
                )
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to delete config subentry: {error_msg}",
                    context={"entry_id": entry_id, "subentry_id": subentry_id},
                )
            )
        return {
            "success": True,
            "action": "delete",
            "target": entry_id,
            "helper_type": "config_subentry",
            "subentry_id": subentry_id,
            "method": "config_subentry_delete",
            "message": f"Successfully deleted config subentry: {subentry_id}",
        }

    # === Path 1: SIMPLE helper delete via websocket ===
    async def _delete_simple_helper(
        self,
        helper_type: HelperTypeLiteral,
        target: str,
        wait_bool: bool,
    ) -> dict[str, Any]:
        """Delete a SIMPLE helper via the websocket {type}/delete API.

        Uses a 3-retry registry lookup with exponential backoff to find the
        helper's unique_id, then falls back to direct-id-delete and a
        confirmed-absent classification if the registry has no record.
        """
        client = self._client
        # Convert to entity_id form
        entity_id = (
            target
            if target.startswith(f"{helper_type}.")
            else f"{helper_type}.{target}"
        )
        # Bare helper_id (without prefix) form for fallback strategies
        helper_id = (
            target.split(".", 1)[1] if target.startswith(f"{helper_type}.") else target
        )

        try:
            # Resolve unique_id via the entity registry, with a retry loop
            # for transient registry failures.
            unique_id = None
            registry_result: dict[str, Any] | None = None
            max_retries = 3

            for attempt in range(max_retries):
                logger.info(
                    f"Getting entity registry for: {entity_id} "
                    f"(attempt {attempt + 1}/{max_retries})"
                )

                # State check is informational only — disabled entities are
                # missing from the state machine but resolved via the registry
                # below (issue #1057). Kept as a debug breadcrumb rather than
                # removed; full removal is option 3.2 in #1057, deferred to a
                # separate PR for minimal blast radius here.
                try:
                    state_check = await client.get_entity_state(entity_id)
                    if not state_check:
                        logger.debug(
                            f"Entity {entity_id} not in state; "
                            "proceeding to registry lookup"
                        )
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
                    registry_result = await client.send_websocket_message(registry_msg)
                    if (registry_result or {}).get("success"):
                        entity_entry = (registry_result or {}).get("result") or {}
                        unique_id = entity_entry.get("unique_id")
                        if unique_id:
                            logger.info(f"Found unique_id: {unique_id} for {entity_id}")
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
                    logger.warning(f"Registry lookup attempt {attempt + 1} failed: {e}")
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
                        removed = await wait_for_entity_removed(client, entity_id)
                        if not removed:
                            response.setdefault("warnings", []).append(
                                f"Deletion confirmed but {entity_id} "
                                "is still present after the wait window."
                            )
                    return response

                # Fallback strategy 2: confirmed-absent classification.
                # Confirm via the registry too — a disabled entity is
                # state-absent but still registry-resident, so
                # state-absence alone is not enough to classify as
                # confirmed-absent. The APIError-404 branch routes the
                # never-existed-target case (HA returns 404 on
                # get_entity_state for unknown entity_ids) into the same
                # confirmed-absent path so the resulting ENTITY_NOT_FOUND
                # raise carries the structured "typo or removed" hint
                # message rather than a raw 404.
                state_gone = False
                try:
                    final_state_check = await client.get_entity_state(entity_id)
                    state_gone = not final_state_check
                except HomeAssistantAPIError as e:
                    # Only 404 confirms the entity is absent from the state
                    # machine. Other API failures (500, 401, …) are transient
                    # or auth issues and must propagate so they aren't
                    # mis-classified as a missing target. Mirrors the
                    # status_code == 404 narrow in _delete_direct_entry.
                    if e.status_code != 404:
                        raise
                    logger.debug(
                        f"State check for {entity_id} raised 404 "
                        f"(treating as state-absent): {e}"
                    )
                    state_gone = True

                if state_gone:
                    registry_still_has_entry = False
                    try:
                        verify_result = await client.send_websocket_message(
                            {
                                "type": "config/entity_registry/get",
                                "entity_id": entity_id,
                            }
                        )
                        if (verify_result or {}).get("success"):
                            verify_entry = (verify_result or {}).get("result") or {}
                            if verify_entry.get("entity_id"):
                                registry_still_has_entry = True
                    except HomeAssistantAPIError as verify_err:
                        # On verify failure, conservatively assume the
                        # entry is still there rather than misclassify
                        # a verify failure as confirmed-absent.
                        logger.debug(
                            f"Registry verify for {entity_id} failed: {verify_err}"
                        )
                        registry_still_has_entry = True

                    if not registry_still_has_entry:
                        logger.info(
                            f"Entity {entity_id} absent from state and "
                            "registry; surfacing as ENTITY_NOT_FOUND"
                        )
                        # Entity-shape target confirmed absent from both
                        # the state machine and the entity registry.
                        # Surface as ENTITY_NOT_FOUND — silent success
                        # would mask the typo case (agent passed the
                        # wrong helper_id / entity_id). Matches sibling
                        # ha_remove_entity.
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.ENTITY_NOT_FOUND,
                                (
                                    f"Helper {target} not found (looked "
                                    f"up as {entity_id}). May indicate "
                                    "it was already removed, never "
                                    "existed, or the identifier is a "
                                    "typo. Verify with "
                                    "ha_search_entities() before "
                                    "retrying."
                                ),
                                context={
                                    "target": target,
                                    "helper_type": helper_type,
                                    "entity_id": entity_id,
                                },
                            )
                        )

                    logger.warning(
                        f"Entity {entity_id} absent from state but still "
                        "in registry; treating as SERVICE_CALL_FAILED"
                    )
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            (
                                f"Helper {target} could not be deleted: "
                                "registry entry exists but unique_id was "
                                "absent and the direct-id fallback "
                                "delete failed."
                            ),
                            suggestions=[
                                "Re-enable the entity via "
                                "ha_set_entity(enabled=True), then retry "
                                "deletion.",
                                "Or inspect the entity registry entry "
                                "directly to confirm unique_id presence.",
                            ],
                            context={
                                "target": target,
                                "entity_id": entity_id,
                            },
                        )
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
                    removed = await wait_for_entity_removed(client, entity_id)
                    if not removed:
                        response.setdefault("warnings", []).append(
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
