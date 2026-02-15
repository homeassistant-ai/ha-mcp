"""
Integration management tools for Home Assistant MCP server.

This module provides tools to list, enable, disable, and delete Home Assistant
integrations (config entries) via the REST and WebSocket APIs.
"""

import asyncio
import logging
import time
from copy import deepcopy
from typing import Annotated, Any

from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage
from .util_helpers import coerce_bool_param

logger = logging.getLogger(__name__)

# Phase-1 VT allowlist: single-step presence controls only
VT_OPTIONS_PHASE1_KEYS: dict[str, dict[str, Any]] = {
    "presence_sensor_entity_id": {
        "entry_type": "central",
        "type": "string",
        "step_id": "presence",
    },
    "use_presence_central_config": {
        "entry_type": "room",
        "type": "boolean",
        "step_id": "presence",
    },
}


def _integration_error(
    code: ErrorCode,
    message: str,
    *,
    details: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return create_error_response(code, message, details=details, context=context)


def _diff_options(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    keys = sorted(set(before.keys()) | set(after.keys()))
    for key in keys:
        b = before.get(key)
        a = after.get(key)
        if b != a:
            diffs.append({"key": key, "before": b, "after": a})
    return diffs


def _entry_type(entry: dict[str, Any]) -> str:
    title = str(entry.get("title", "")).strip().lower()
    if title == "central configuration":
        return "central"
    return "room"


def _coerce_patch_value(key: str, value: Any) -> tuple[bool, Any, str | None]:
    cfg = VT_OPTIONS_PHASE1_KEYS.get(key)
    if not cfg:
        return False, None, "unknown key"
    expected = cfg.get("type")
    if expected == "string":
        if isinstance(value, str) and value.strip():
            return True, value, None
        return False, None, "expected non-empty string"
    if expected == "boolean":
        if isinstance(value, bool):
            return True, value, None
        if isinstance(value, str):
            val = value.strip().lower()
            if val in ("true", "on", "1", "yes"):
                return True, True, None
            if val in ("false", "off", "0", "no"):
                return True, False, None
        return False, None, "expected boolean"
    return False, None, "unsupported expected type"


def _schema_suggested_value(
    data_schema: list[dict[str, Any]] | None, field_name: str
) -> Any | None:
    if not data_schema:
        return None
    for item in data_schema:
        if not isinstance(item, dict):
            continue
        if item.get("name") != field_name:
            continue
        desc = item.get("description")
        if isinstance(desc, dict) and "suggested_value" in desc:
            return desc.get("suggested_value")
        if "default" in item:
            return item.get("default")
        return None
    return None


def _parse_options_patch(options_patch: dict[str, Any] | str) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(options_patch, str):
        import json

        try:
            parsed = json.loads(options_patch)
        except json.JSONDecodeError as exc:
            return None, f"Invalid JSON for options_patch: {exc}"
        return parsed, None
    return options_patch, None


def _validate_vt_patch(
    *,
    entry_kind: str,
    options_patch_obj: dict[str, Any],
    strict_keys: bool,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    normalized_patch: dict[str, Any] = {}
    key_steps: set[str] = set()
    key_errors: list[dict[str, Any]] = []

    for key, value in options_patch_obj.items():
        cfg = VT_OPTIONS_PHASE1_KEYS.get(key)
        if not cfg:
            if strict_keys:
                key_errors.append(
                    {"key": key, "reason": "unknown key", "expected": "known VT phase-1 key"}
                )
            continue

        if cfg.get("entry_type") != entry_kind:
            key_errors.append(
                {
                    "key": key,
                    "reason": "key not valid for this entry type",
                    "entry_type": entry_kind,
                    "expected_entry_type": cfg.get("entry_type"),
                }
            )
            continue

        ok, coerced, err = _coerce_patch_value(key, value)
        if not ok:
            key_errors.append({"key": key, "reason": err, "value": value})
            continue
        normalized_patch[key] = coerced
        key_steps.add(str(cfg.get("step_id")))

    if (
        entry_kind == "room"
        and normalized_patch.get("use_presence_central_config") is False
        and "presence_sensor_entity_id" not in normalized_patch
    ):
        key_errors.append(
            {
                "key": "presence_sensor_entity_id",
                "reason": "required when use_presence_central_config=false",
            }
        )

    target_step = next(iter(key_steps), "")
    return normalized_patch, target_step, key_errors


async def _verify_presence_sensor_with_timeout(
    *,
    client: Any,
    entry_id: str,
    expected: str,
    timeout_seconds: float = 2.0,
    poll_interval_seconds: float = 0.35,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        verify_flow = await client.start_options_flow(entry_id)
        verify_flow_id = verify_flow.get("flow_id")
        verify_presence = None
        if verify_flow.get("type") == "menu" and verify_flow_id:
            verify_presence = await client.submit_options_flow_step(
                verify_flow_id, {"next_step_id": "presence"}
            )
        elif verify_flow.get("type") == "form":
            verify_presence = verify_flow

        suggested = _schema_suggested_value(
            (verify_presence or {}).get("data_schema"),
            "presence_sensor_entity_id",
        )
        if suggested == expected:
            return True
        await asyncio.sleep(poll_interval_seconds)
    return False


def register_integration_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register integration management tools with the MCP server."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["integration"], "title": "Get Integration"})
    @log_tool_usage
    async def ha_get_integration(
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
                description="When listing, fuzzy search by domain or title.",
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
    ) -> dict[str, Any]:
        """
        Get integration (config entry) information - list all or get a specific one.

        Without an entry_id: Lists all configured integrations with optional filters.
        With an entry_id: Returns detailed information including full options/configuration.

        Use this to audit existing configurations (e.g. template sensor Jinja code).
        When creating new functionality, prefer UI-based helpers over templates when possible.

        EXAMPLES:
        - List all integrations: ha_get_integration()
        - Search integrations: ha_get_integration(query="zigbee")
        - Get specific entry: ha_get_integration(entry_id="abc123")
        - List template entries with definitions: ha_get_integration(domain="template")
        - List all with options: ha_get_integration(include_options=True)

        STATES: 'loaded' (running), 'setup_error', 'setup_retry', 'not_loaded',
        'failed_unload', 'migration_error'.

        RETURNS (when listing):
        - entries: List of integrations with domain, title, state, capabilities
        - state_summary: Count of entries in each state
        - When domain filter or include_options is set, each entry includes the 'options' object

        RETURNS (when getting specific entry):
        - entry: Full config entry details including options/configuration
        """
        try:
            include_opts = coerce_bool_param(include_options, "include_options", default=False)
            # Auto-enable options when domain filter is set
            if domain is not None:
                include_opts = True

            # If entry_id provided, get specific config entry
            if entry_id is not None:
                try:
                    result = await client.get_config_entry(entry_id)
                    return {"success": True, "entry_id": entry_id, "entry": result}
                except Exception as e:
                    error_msg = str(e)
                    if "404" in error_msg or "not found" in error_msg.lower():
                        return {
                            "success": False,
                            "error": f"Config entry not found: {entry_id}",
                            "entry_id": entry_id,
                            "suggestion": "Use ha_get_integration() without entry_id to see all config entries",
                        }
                    raise

            # List mode - get all config entries
            # Use REST API endpoint for config entries
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

            # Apply domain filter before formatting
            if domain:
                domain_lower = domain.strip().lower()
                entries = [e for e in entries if e.get("domain", "").lower() == domain_lower]

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

            result_data: dict[str, Any] = {
                "success": True,
                "total": len(formatted_entries),
                "entries": formatted_entries,
                "state_summary": state_summary,
                "query": query if query else None,
            }
            if domain:
                result_data["domain_filter"] = domain.strip().lower()
            return result_data

        except Exception as e:
            logger.error(f"Failed to get integrations: {e}")
            return {
                "success": False,
                "error": f"Failed to get integrations: {str(e)}",
                "suggestions": [
                    "Verify Home Assistant connection is working",
                    "Check that the API is accessible",
                    "Ensure your token has sufficient permissions",
                ],
            }

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["integration"],
            "title": "Get Integration Options",
        }
    )
    @log_tool_usage
    async def ha_get_integration_options(
        entry_id: Annotated[
            str,
            Field(description="Config entry ID for which to retrieve current options and validation hints."),
        ],
        include_validation_hints: Annotated[
            bool | str,
            Field(
                description="Include options-flow schema hints (data_schema) for supported steps.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Get current persisted integration options and optional options-flow validation hints.

        Returns entry metadata, current options from config-entry resource, and
        options-flow data_schema hints (currently phase-1 focused on VT presence step).
        """
        try:
            include_hints = coerce_bool_param(
                include_validation_hints, "include_validation_hints", default=True
            )
            entry = await client.get_config_entry(entry_id)
            result: dict[str, Any] = {
                "success": True,
                "entry_id": entry_id,
                "domain": entry.get("domain"),
                "title": entry.get("title"),
                "state": entry.get("state"),
                "options": entry.get("options", {}),
            }
            if not include_hints:
                return result

            validation_hints: dict[str, Any] = {}
            try:
                flow = await client.start_options_flow(entry_id)
                flow_id = flow.get("flow_id")
                if flow.get("type") == "menu" and flow_id:
                    # Phase-1: presence step discovery
                    presence = await client.submit_options_flow_step(
                        flow_id, {"next_step_id": "presence"}
                    )
                    validation_hints["presence"] = {
                        "type": presence.get("type"),
                        "step_id": presence.get("step_id"),
                        "data_schema": presence.get("data_schema"),
                        "errors": presence.get("errors"),
                    }
            except Exception as e:
                validation_hints["error"] = str(e)

            result["validation_hints"] = validation_hints
            return result
        except Exception as e:
            logger.error(f"Failed to get integration options: {e}")
            return _integration_error(
                ErrorCode.CONFIG_NOT_FOUND,
                "Failed to retrieve integration options.",
                details=str(e),
                context={"entry_id": entry_id},
            )

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["integration"],
            "title": "Set Integration Options",
        }
    )
    @log_tool_usage
    async def ha_set_integration_options(
        entry_id: Annotated[str, Field(description="Config entry ID")],
        options_patch: Annotated[
            dict[str, Any] | str,
            Field(description="Partial options object to patch."),
        ],
        confirm: Annotated[
            bool | str,
            Field(description="Must be true for non-dry-run apply.", default=False),
        ] = False,
        dry_run: Annotated[
            bool | str,
            Field(description="If true, validate and return diff only.", default=True),
        ] = True,
        auto_backup: Annotated[
            bool | str,
            Field(description="If true, attempt backup before apply.", default=True),
        ] = True,
        strict_keys: Annotated[
            bool | str,
            Field(description="If true, reject unknown patch keys.", default=True),
        ] = True,
        request_id: Annotated[
            str | None,
            Field(description="Optional request ID for correlation.", default=None),
        ] = None,
    ) -> dict[str, Any]:
        """
        Safely patch config-entry options via Home Assistant options flow.

        Phase-1 constraints:
        - Supports versatile_thermostat domain only.
        - Supports presence-step keys only:
          - presence_sensor_entity_id (central entries)
          - use_presence_central_config (room entries)
        - Single-step patch enforcement.
        """
        try:
            dry_run_bool = coerce_bool_param(dry_run, "dry_run", default=True)
            confirm_bool = coerce_bool_param(confirm, "confirm", default=False)
            auto_backup_bool = coerce_bool_param(auto_backup, "auto_backup", default=True)
            strict_keys_bool = coerce_bool_param(strict_keys, "strict_keys", default=True)

            options_patch_obj, parse_error = _parse_options_patch(options_patch)
            if parse_error:
                return _integration_error(
                    ErrorCode.VALIDATION_INVALID_JSON,
                    "options_patch must be valid JSON.",
                    details=parse_error,
                    context={"entry_id": entry_id},
                )

            if not isinstance(options_patch_obj, dict) or not options_patch_obj:
                return _integration_error(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "options_patch must be a non-empty object.",
                    context={"entry_id": entry_id},
                )

            entry = await client.get_config_entry(entry_id)
            domain = entry.get("domain")
            title = entry.get("title")
            if domain != "versatile_thermostat":
                return _integration_error(
                    ErrorCode.CONFIG_INVALID,
                    "Only versatile_thermostat is supported in phase-1.",
                    context={"entry_id": entry_id, "domain": domain},
                )

            entry_kind = _entry_type(entry)
            normalized_patch, target_step, key_errors = _validate_vt_patch(
                entry_kind=entry_kind,
                options_patch_obj=options_patch_obj,
                strict_keys=strict_keys_bool,
            )

            if key_errors:
                return _integration_error(
                    ErrorCode.CONFIG_VALIDATION_FAILED,
                    "Patch validation failed.",
                    details=str(key_errors),
                    context={"entry_id": entry_id},
                )

            if not normalized_patch:
                return _integration_error(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "No valid patch keys remained after validation.",
                    context={"entry_id": entry_id},
                )
            if not target_step:
                return _integration_error(
                    ErrorCode.CONFIG_VALIDATION_FAILED,
                    "Patch keys must belong to a single options-flow step in phase-1.",
                    context={"entry_id": entry_id},
                )
            before_options = deepcopy(entry.get("options", {}) or {})
            candidate = deepcopy(before_options)
            candidate.update(normalized_patch)
            diff = _diff_options(before_options, candidate)

            base_response: dict[str, Any] = {
                "success": True,
                "applied": False,
                "verified": False,
                "verification_method": "none",
                "entry_id": entry_id,
                "domain": domain,
                "title": title,
                "before_options": before_options,
                "diff": diff,
                "meta": {
                    "dry_run": dry_run_bool,
                    "request_id": request_id,
                    "target_step": target_step,
                    "strict_keys": strict_keys_bool,
                },
            }

            if not diff:
                base_response["warnings"] = ["No-op patch; options already match requested values."]
                base_response["verified"] = True
                base_response["verification_method"] = "none"
                return base_response

            if dry_run_bool:
                return base_response

            if not confirm_bool:
                return _integration_error(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "confirm=true is required for non-dry-run apply.",
                    context={"entry_id": entry_id},
                )

            backup_info: dict[str, Any] | None = None
            if auto_backup_bool:
                try:
                    backup_resp = await client._request(
                        "POST", "/services/backup/create_automatic", json={}
                    )
                    backup_info = {
                        "attempted": True,
                        "status": "started",
                        "result": backup_resp,
                    }
                except Exception as e:
                    msg = str(e).lower()
                    if "404" in msg or "not found" in msg:
                        return _integration_error(
                            ErrorCode.RESOURCE_NOT_FOUND,
                            "Backup service is unavailable in this Home Assistant environment.",
                            details=str(e),
                            context={"entry_id": entry_id},
                        )
                    return _integration_error(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "Backup attempt failed before applying options.",
                        details=str(e),
                        context={"entry_id": entry_id},
                    )

            flow = await client.start_options_flow(entry_id)
            flow_id = flow.get("flow_id")
            if not flow_id:
                return _integration_error(
                    ErrorCode.CONFIG_NOT_FOUND,
                    "Options flow did not return a flow_id.",
                    context={"entry_id": entry_id},
                )

            # Navigate from menu to target step (phase-1 only)
            current = flow
            if current.get("type") == "menu":
                current = await client.submit_options_flow_step(
                    flow_id, {"next_step_id": target_step}
                )

            if current.get("type") != "form":
                return _integration_error(
                    ErrorCode.CONFIG_INVALID,
                    "Expected a form step for options submission.",
                    context={"entry_id": entry_id, "target_step": target_step, "flow_type": current.get("type")},
                )

            submit_payload = {k: candidate.get(k) for k in normalized_patch}
            apply_result = await client.submit_options_flow_step(flow_id, submit_payload)

            # Room presence branch: if user disables central presence config,
            # VT requires an additional presence_sensor_entity_id form step.
            if apply_result.get("type") == "form":
                step_id = apply_result.get("step_id")
                schema = apply_result.get("data_schema", []) or []
                required_fields = {
                    f.get("name")
                    for f in schema
                    if isinstance(f, dict) and f.get("required") is True and f.get("name")
                }
                field_names = {
                    f.get("name")
                    for f in schema
                    if isinstance(f, dict) and f.get("name")
                }
                if (
                    step_id == "presence"
                    and "presence_sensor_entity_id" in field_names
                    and set(normalized_patch.keys()) == {"use_presence_central_config"}
                    and normalized_patch.get("use_presence_central_config") is False
                ):
                    sensor_val = None
                    suggested = _schema_suggested_value(
                        schema, "presence_sensor_entity_id"
                    )
                    if isinstance(suggested, str) and suggested.strip():
                        sensor_val = suggested
                    elif "presence_sensor_entity_id" in before_options:
                        prev = before_options.get("presence_sensor_entity_id")
                        if isinstance(prev, str) and prev.strip():
                            sensor_val = prev
                    if not sensor_val and "presence_sensor_entity_id" in required_fields:
                        return _integration_error(
                            ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS,
                            "Options flow requires presence_sensor_entity_id when disabling central presence config.",
                            context={"entry_id": entry_id, "step_id": step_id},
                        )
                    if sensor_val:
                        apply_result = await client.submit_options_flow_step(
                            flow_id, {"presence_sensor_entity_id": sensor_val}
                        )

            # Some integrations return to menu and need explicit finalize.
            if apply_result.get("type") == "menu":
                menu_options = apply_result.get("menu_options", []) or []
                if "finalize" in menu_options:
                    apply_result = await client.submit_options_flow_step(
                        flow_id, {"next_step_id": "finalize"}
                    )

            # If we are still in a form step, the write is incomplete.
            if apply_result.get("type") == "form":
                form_schema = apply_result.get("data_schema", [])
                return _integration_error(
                    ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS,
                    "Options flow requires additional form input; single-step apply is incomplete.",
                    details=str([f.get("name") for f in form_schema if isinstance(f, dict)]),
                    context={"entry_id": entry_id, "step_id": apply_result.get("step_id")},
                )

            # Read back persisted options
            updated_entry = await client.get_config_entry(entry_id)
            after_options = deepcopy(updated_entry.get("options", {}) or {})
            verify_diff = _diff_options(before_options, after_options)

            mismatched: list[dict[str, Any]] = []
            for key, expected_val in normalized_patch.items():
                actual_val = after_options.get(key)
                if actual_val != expected_val:
                    mismatched.append(
                        {"key": key, "expected": expected_val, "actual": actual_val}
                    )

            # Fallback verification for environments where config-entry options/data
            # are masked by HA API. For phase-1 central presence key, we can verify
            # persisted value through options-flow suggested_value.
            if mismatched and set(normalized_patch.keys()) == {"presence_sensor_entity_id"}:
                try:
                    expected = normalized_patch["presence_sensor_entity_id"]
                    verified = await _verify_presence_sensor_with_timeout(
                        client=client,
                        entry_id=entry_id,
                        expected=expected,
                    )
                    if verified:
                        mismatched = []
                        verify_diff = _diff_options(before_options, candidate)
                        after_options = deepcopy(candidate)
                except Exception:
                    # Keep original mismatch behavior on verification failure.
                    pass
            if mismatched:
                # Room presence toggle can be persisted through options flow, but in
                # some HA/VT combinations there is no reliable readback surface
                # (config_entry data/options and flow suggested/default remain static).
                # In that case, if flow completed with create_entry, return applied
                # with explicit unverifiable warning.
                if (
                    set(normalized_patch.keys()) == {"use_presence_central_config"}
                    and apply_result.get("type") == "create_entry"
                ):
                    response_unverified: dict[str, Any] = {
                        "success": True,
                        "applied": True,
                        "verified": False,
                        "verification_method": "none",
                        "entry_id": entry_id,
                        "domain": domain,
                        "title": title,
                        "before_options": before_options,
                        "after_options": deepcopy(candidate),
                        "diff": _diff_options(before_options, candidate),
                        "warnings": [
                            "Applied via options flow, but persistence could not be directly verified from exposed HA APIs."
                        ],
                        "meta": {
                            "request_id": request_id,
                            "target_step": target_step,
                            "apply_flow_result_type": apply_result.get("type"),
                            "apply_flow_step_id": apply_result.get("step_id"),
                            "verification": "unverified",
                        },
                    }
                    if backup_info:
                        response_unverified["backup_info"] = backup_info
                    return response_unverified

                return _integration_error(
                    ErrorCode.RESOURCE_LOCKED,
                    "Options write could not be verified from persisted config-entry options.",
                    details=str(mismatched),
                    context={"entry_id": entry_id},
                )

            response: dict[str, Any] = {
                "success": True,
                "applied": True,
                "verified": True,
                "verification_method": (
                    "flow_suggested"
                    if set(normalized_patch.keys()) == {"presence_sensor_entity_id"}
                    else "config_entry"
                ),
                "entry_id": entry_id,
                "domain": domain,
                "title": title,
                "before_options": before_options,
                "after_options": after_options,
                "diff": verify_diff,
                "meta": {
                    "request_id": request_id,
                    "target_step": target_step,
                    "apply_flow_result_type": apply_result.get("type"),
                    "apply_flow_step_id": apply_result.get("step_id"),
                },
            }
            if backup_info:
                response["backup_info"] = backup_info
            return response
        except Exception as e:
            logger.error(f"Failed to set integration options: {e}")
            return _integration_error(
                ErrorCode.INTERNAL_ERROR,
                "Failed to set integration options.",
                details=str(e),
                context={"entry_id": entry_id},
            )

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

        Use ha_get_integration() to find entry IDs.
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

            if require_restart:
                note = "Home Assistant restart required for changes to take effect."
            else:
                note = "Integration has been loaded." if enabled_bool else "Integration has been unloaded."

            return {
                "success": True,
                "message": f"Integration {'enabled' if enabled_bool else 'disabled'} successfully",
                "entry_id": entry_id,
                "require_restart": require_restart,
                "note": note,
            }

        except Exception as e:
            logger.error(f"Failed to set integration enabled: {e}")
            exception_to_structured_error(e, context={"entry_id": entry_id}, raise_error=True)

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

        Use ha_get_integration() to find entry IDs.
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
            exception_to_structured_error(e, context={"entry_id": entry_id}, raise_error=True)
