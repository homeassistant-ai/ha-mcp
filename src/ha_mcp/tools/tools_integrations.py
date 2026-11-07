"""
Integration management tools for Home Assistant MCP server.

This module provides tools to list, enable, disable, and delete Home Assistant
integrations (config entries) via the REST and WebSocket APIs.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backup_manager import _is_flow_helper_domain
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from ..redaction import (
    redaction_enabled,
)
from .auto_backup import with_auto_backup
from .coercion import JSON_STRING_COERCION
from .component_config_entries import (
    SCRUB_DEGRADED_WARNING,
    ComponentEntries,
    fetch_config_entries,
)
from .component_helper_collections import removal_reply, stored_helper_target
from .component_registry_lookup import resolve_entities_via_component
from .config_entry_backup import (
    flow_helper_backup_domain,
    removal_backup_id,
    resolve_config_entry_backup_domain,
    skip_unless_flow_helper,
)
from .config_entry_flow import refuse_component_entry
from .config_entry_flow_introspect import abort_probe_flow
from .config_helpers.registry import _get_entities_for_config_entry
from .config_helpers.schemas import SIMPLE_HELPER_TYPES, StorageHelperType
from .diagnostics_helpers import fetch_integration_diagnostics, parse_diagnostics_fields
from .flow_helper_lookup import (
    get_entry_id_for_flow_helper,
    not_stored_reply,
    raise_flow_helper_lookup_error,
    raise_unregistered_entity_error,
    resolve_helper_entity,
)
from .helper_flows import require_helper_type
from .helpers import (
    exception_to_structured_error,
    get_entity_state_or_none,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
    ws_failure_code,
)
from .ignored_entries import normalize_ignored_entry
from .integration_entry_state import set_entry_enabled, set_log_level
from .integration_flow_steps import run_integration_step
from .integration_schema import (
    describe_flow_form,
    entry_count,
    fetch_config_subentry_schema,
    fetch_options_schema,
    options_from_form_flow,
    options_unread_warning,
    options_withheld_warning,
    withhold_component_options,
)
from .integration_ws_commands import (
    attach_ws_commands,
    reject_ws_command_conflicts,
    send_ws_command,
)
from .response_helpers import build_pagination_metadata
from .tool_hints import read_only_hints, write_hints
from .util_helpers import get_logger_levels, websocket_error_message
from .ws_waiters import verify_entity_removed, wait_for_entity_removed

logger = logging.getLogger(__name__)

# First wait between helper registry lookups; each later retry doubles it.
_REGISTRY_RETRY_BASE_DELAY = 0.5


def _removal_backup_domain(kwargs: dict[str, Any]) -> str:
    """Snapshot under ``helper_<type>``, the handler ha_config_set_helper uses,
    when helper_type is set; an untyped target here is a config entry
    (_skip_removal_capture already skipped an untyped entity_id)."""
    helper_type = kwargs.get("helper_type")
    return f"helper_{helper_type}" if helper_type else "integration"


def _skip_removal_capture(kwargs: dict[str, Any]) -> bool:
    """True when a later layer owns the snapshot: a FLOW type is captured by
    _delete_resolved_flow_helper once Core resolves its entry, a type-less
    entity_id by the resolved call (_remove_resolved_helper, which again
    defers a FLOW type to _delete_resolved_flow_helper)."""
    helper_type = kwargs.get("helper_type")
    if helper_type is None:
        return "." in str(kwargs.get("target", ""))
    return _is_flow_helper_domain(f"helper_{helper_type}")


def _raise_registry_read_failure(
    target: str, entity_id: str, result: dict[str, Any] | None
) -> NoReturn:
    """Report a failed registry read as itself: anything but Core's
    ``not_found`` proves nothing about whether the entity is registered."""
    failure = result or {}
    detail = failure.get("error") or "unknown error" if result else "no response"
    raise_tool_error(
        create_error_response(
            ws_failure_code(failure),
            (
                f"Reading the entity registry for {entity_id} failed: "
                f"{detail}. Nothing was deleted."
            ),
            context={"target": target, "entity_id": entity_id},
            # A proxy-blocked read carries its own guidance.
            suggestions=failure.get("suggestions")
            or [
                "Retry; check the Home Assistant connection if it keeps failing.",
            ],
        )
    )


def _reject_set_integration_mode_conflicts(
    entry_id: str | None,
    domain: str | None,
    enabled: bool | None,
    config: dict[str, Any] | None,
    *,
    flow_args: list[str],
) -> None:
    """Reject ha_set_integration argument combinations that name two modes or none."""
    if domain is not None and entry_id is not None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Pass either 'domain' (add a new integration) or "
                "'entry_id' (modify an existing one), not both",
                context={"entry_id": entry_id, "domain": domain},
            )
        )
    if enabled is not None and (domain is not None or config is not None or flow_args):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "'enabled' is mutually exclusive with 'domain', 'config' and "
                "the flow parameters — enable/disable is a separate call",
                context={"entry_id": entry_id, "domain": domain},
            )
        )
    if domain is None and entry_id is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Nothing to do — provide 'domain' to add an "
                "integration, or 'entry_id' (its options flow, or with "
                "'enabled' to enable/disable it)",
                suggestions=[
                    "Use ha_get_integration() to find valid config entry IDs",
                ],
            )
        )


# ha_remove_helpers_integrations.helper_type: a storage helper type,
# "config_subentry", or one of Core's helper flows, checked at call time.
HelperTypeLiteral = StorageHelperType | Literal["config_subentry"] | str


async def fetch_entry_options_with_status(
    client: Any, entry_id: str, *, quiet: bool = False
) -> tuple[dict[str, Any], bool]:
    """Read a config entry's ``options`` and report whether the probe succeeded.

    Starts the entry's OptionsFlow, harvests ``{name: current_value}`` from
    its first-step form via :func:`options_from_form_flow` (the persisted
    option from ``description.suggested_value``, falling back to the schema
    ``default``), and aborts the flow so it doesn't sit half-open. Returns
    ``(options, ok)`` so callers can tell a probe *failure* apart from a
    genuinely-empty options form — both yield ``{}`` for ``options``, but
    ``ok`` is:

    - ``True`` only when a form first-step was read (even if it harvested no
      fields: a genuinely-empty options form is a successful read).
    - ``False`` when the OptionsFlow could not be read into options: the flow
      raised, or its first step was not a form (a menu / abort / create_entry),
      so no options could be harvested.

    The flag lets the caller surface degraded reads instead of passing ``{}``
    off as real options: ``smart_search`` flips ``partial`` when a probe
    fails mid-search. The abort in ``finally`` is cleanup; a failed abort does
    not flip ``ok`` (the options were already harvested).

    Home Assistant does not expose ``ConfigEntry.options`` through any
    read-only REST or WebSocket endpoint — ``/api/config/config_entries/entry``
    deliberately omits the field. The closest approximation that the HA UI
    itself uses is the OptionsFlow's first-step ``data_schema``: HA injects
    the persisted options into each field's ``description.suggested_value``
    (via ``add_suggested_values_to_schema``), which
    :func:`options_from_form_flow` prefers over the static schema
    ``default`` (issue #1575).

    Probe failures log at ``warning`` unless ``quiet=True``, which demotes
    them to ``debug`` for a bulk fan-out: ``smart_search`` probes one entry
    per flow helper on every ``ha_search``, and a per-entry warning there
    would spam the log on routine searches.

    Exposed at module level (not as a method) so
    ``smart_search._search_flow_helpers`` can probe flow-helper config
    without instantiating ``IntegrationTools``.
    """
    log_probe_failure = logger.debug if quiet else logger.warning
    flow_id: str | None = None
    try:
        flow = await client.start_options_flow(entry_id)
        flow_id = flow.get("flow_id")
        flow_type = flow.get("type")
        if flow_type != "form":
            log_probe_failure(
                f"OptionsFlow for {entry_id} returned type={flow_type!r}, "
                f"not a form — cannot extract options"
            )
            return {}, False
        return options_from_form_flow(flow), True
    except Exception as exc:  # noqa: BLE001
        log_probe_failure(
            f"Failed to fetch options for {entry_id}: {type(exc).__name__}: {exc}"
        )
        return {}, False
    finally:
        if flow_id:
            await abort_probe_flow(client.abort_options_flow, flow_id, quiet=quiet)


def _split_component_entry_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split a component ``config_entries`` row into ``(entry, subentries)``.

    The returned ``entry`` mirrors the legacy REST per-entry shape (plus
    ``unique_id``): the row's Core fields and its already-materialized ``options`` (raw
    persisted, secret-scrubbed), with the nested ``subentries`` list lifted out
    so callers surface subentries at the top level exactly like the legacy
    ``include_subentries`` branch — and never leak them onto ``entry`` when
    subentries were not requested. ``options`` values may be ``"**redacted**"``
    where the component scrubbed a resolved ``!secret``.
    """
    entry = dict(row)
    subentries = entry.pop("subentries", None)
    if not isinstance(subentries, list):
        subentries = []
    return entry, subentries


def _reject_log_persistence_without_level(
    log_persistence: str | None, log_level: str | None
) -> None:
    if log_persistence is not None and log_level is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "log_persistence applies only together with log_level",
                suggestions=[
                    "Example: ha_set_integration(domain='zha', "
                    "log_level='DEBUG', log_persistence='permanent')"
                ],
            )
        )


def _reject_reconfigure_conflicts(
    reconfigure: bool, guards: dict[str, Any], domain: str | None, enabled: bool | None
) -> None:
    """Only reconfigure takes the identity guards, and it takes no domain or enabled."""
    if reconfigure and (domain is not None or enabled is not None):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Reconfigure mode requires entry_id; do not "
                "combine it with domain or enabled",
                context={"domain": domain, "enabled": enabled},
            )
        )
    rejected = sorted(name for name, value in guards.items() if value is not None)
    if rejected and not reconfigure:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "These parameters require reconfigure=True and were rejected: "
                + ", ".join(rejected),
                suggestions=[
                    "Pass reconfigure=True to use the identity safeguards of the "
                    "official reconfigure flow."
                ],
                # Not "ignored": the call was rejected and nothing ran.
                context={"rejected_parameters": rejected},
            )
        )


class IntegrationTools:
    """Integration management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_integration",
        tags={"Integrations"},
        annotations=read_only_hints("Get Integration", open_world=False),
    )
    @log_tool_usage
    async def ha_get_integration(
        self,
        entry_id: Annotated[
            str | None,
            Field(
                description="Config entry ID to get details for.",
                default=None,
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="When listing, search by domain or title.",
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
        integration_type: Annotated[
            str | None,
            Field(
                description="When listing, keep only entries whose integration "
                "has this type, matched by Home Assistant itself (the manifest's "
                "integration_type, e.g. 'device', 'hub', 'service', 'helper'). "
                "Home Assistant does not reject an unknown type: for any type but "
                "'helper' it also keeps entries whose integration it cannot find "
                "(e.g. a removed custom integration), so an unknown type returns "
                "only those.",
                default=None,
            ),
        ] = None,
        include_options: Annotated[
            bool,
            Field(
                description="Include the options object for each entry. "
                "Automatically enabled when domain filter is set. "
                "For UI-created flow-based helpers (template, group, "
                "utility_meter, derivative, ...), the current config — "
                "template body, group members, source entity, etc. — is "
                "here. Read through the ha_mcp_tools custom component; without "
                "it an entry that has an options flow comes back without options "
                "and a warning, and only include_schema on that entry reads them. "
                "With the component, prefer this over include_schema when you "
                "only need the current values; use include_schema when you also "
                "need the field types or selector metadata.",
                default=False,
            ),
        ] = False,
        include_schema: Annotated[
            bool,
            Field(
                description="When entry_id is set, also describe the options flow "
                "form as options_schema.fields: each field's type, current value "
                "and Home Assistant's help text. A flow that opens with a menu "
                "lists menu_options instead (ha_set_integration(entry_id=..., "
                "menu_choice=...) opens one), and a flow Home "
                "Assistant aborts gives aborted, plus reason_text when Home "
                "Assistant has a string for the reason. Only applies "
                "when supports_options=true. The flow is started, described "
                "and aborted; starting it runs the integration's own flow code, "
                "and some integrations act there (Sonoff LAN signs in to its "
                "cloud). For an integration's setup form, use "
                "ha_set_integration(domain=...).",
                default=False,
            ),
        ] = False,
        include_subentries: Annotated[
            bool,
            Field(
                description=(
                    "When entry_id is set, include config subentries for the integration "
                    "entry."
                ),
                default=False,
            ),
        ] = False,
        include_subentry_schema: Annotated[
            bool,
            Field(
                description=(
                    "When entry_id is set, describe a config subentry flow's "
                    "form as subentry_schema.fields (same shape as "
                    "include_schema); the flow is aborted afterwards. Pair with "
                    "subentry_type, and optionally subentry_id for the "
                    "reconfigure form."
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
            bool,
            Field(
                description=(
                    "When include_subentry_schema=True, ask older Home Assistant "
                    "versions to expose advanced flow options. No-op on HA "
                    "2026.6+; pending removal before HA 2027.6."
                ),
                default=False,
            ),
        ] = False,
        exact_match: Annotated[
            bool,
            Field(
                description=(
                    "Use exact substring matching for query filter. Set to False for fuzzy "
                    "matching when the query may contain typos."
                ),
                default=True,
            ),
        ] = True,
        limit: Annotated[
            int,
            Field(
                default=50,
                ge=1,
                le=200,
                description="Max entries to return per page in list mode",
            ),
        ] = 50,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of entries to skip for pagination",
            ),
        ] = 0,
        include_diagnostics: Annotated[
            bool,
            Field(
                description=(
                    "When entry_id is set, also fetch the integration's diagnostics dump — "
                    "integration-defined JSON (commonly includes redacted config, device "
                    "list, state snapshots; exact top-level keys vary by integration), the "
                    "same artifact as the UI's 'Download diagnostics'. Payloads can be "
                    "large (Hue ~290 KB, ZHA/MQTT/ESPHome several MB) — pair with "
                    "diagnostics_fields or diagnostics_truncate_at_bytes."
                ),
                default=False,
            ),
        ] = False,
        include_ws_commands: Annotated[
            bool,
            Field(
                description=(
                    "When entry_id is set, also list the integration's own "
                    "'<domain>/' WebSocket commands (the API its panel uses, e.g. "
                    "KNX's knx/get_knx_project) under ws_commands, each with the "
                    "request schema Home Assistant registered for it. Send one "
                    "with ha_set_integration(entry_id=..., ws_command=...). "
                    "Needs the ha_mcp_tools custom component; without it a "
                    "warning says the list is unavailable."
                ),
                default=False,
            ),
        ] = False,
        device_id: Annotated[
            str | None,
            Field(
                description=(
                    "With include_diagnostics=True, return the device-scoped diagnostics "
                    "dump for this device instead of the full integration dump. Some "
                    "integrations only expose config-entry-level dumps."
                ),
                default=None,
            ),
        ] = None,
        diagnostics_fields: Annotated[
            list[str] | str | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Top-level keys to keep from the diagnostics data payload (e.g. "
                    "['home_assistant', 'issues']). Accepts a JSON list or comma-separated "
                    "string. Only applies when include_diagnostics=True and the data "
                    "payload is a dict. Unknown keys are dropped and listed under "
                    "omitted_fields."
                ),
                default=None,
            ),
        ] = None,
        diagnostics_truncate_at_bytes: Annotated[
            int | None,
            Field(
                description=(
                    "Byte cap on the serialized diagnostics payload (after "
                    "diagnostics_fields and diagnostics_data_path have been applied). On "
                    "hit, drops data and emits truncated=true, bytes_total, byte_cap, plus "
                    "available_fields (when the capped value is a dict). Recommended "
                    "starting point: 20000 bytes. Only applies when "
                    "include_diagnostics=True."
                ),
                default=None,
                ge=1,
            ),
        ] = None,
        diagnostics_data_path: Annotated[
            str | None,
            Field(
                description=(
                    "Dotted path into the diagnostics data sub-tree (e.g. '<list-valued "
                    "path>' for per-device records, 'home_assistant.version' for HA core "
                    "version; the exact key path varies by integration version). Walks into"
                    " the post-fields payload. Resolution failures replace data with null "
                    "and surface data_path_error. Only applies when "
                    "include_diagnostics=True."
                ),
                default=None,
            ),
        ] = None,
        diagnostics_data_offset: Annotated[
            int | None,
            Field(
                description=(
                    "Pagination start index for list-valued diagnostics_data_path results. "
                    "Ignored when diagnostics_data_path is unset, diagnostics_data_limit is"
                    " unset, or the resolved value is not a list. Only applies when "
                    "include_diagnostics=True."
                ),
                default=0,
                ge=0,
            ),
        ] = 0,
        diagnostics_data_limit: Annotated[
            int | None,
            Field(
                description=(
                    "Pagination window size for list-valued diagnostics_data_path results. "
                    "When set with a list-resolved path, swaps data for a pagination "
                    "envelope {path, items, offset, limit, total, has_more}. Default None "
                    "returns the full resolved value. Only applies when "
                    "include_diagnostics=True."
                ),
                default=None,
                ge=1,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get integration (config entry) information with pagination.

        Without an entry_id: Lists all configured integrations with optional filters.
        With an entry_id: Returns detailed information including full options/configuration.

        WHEN NOT TO USE:
        - Changing an entry, or describing a setup form or a form behind a
          menu: use ha_set_integration (without config it returns the form).

        EXAMPLES:
        - List / search: ha_get_integration(query="zigbee")
        - Get an entry with its editable fields: ha_get_integration(entry_id="abc123", include_schema=True)
        - Diagnostics dump, paged along a list-valued path: ha_get_integration(entry_id="abc123", include_diagnostics=True, diagnostics_data_path="<list-valued path>", diagnostics_data_limit=10, diagnostics_data_offset=20)
        - Inspect a subentry reconfigure schema: ha_get_integration(entry_id="abc123", include_subentry_schema=True, subentry_type="conversation", subentry_id="sub123")
        - Options flow that opens with a menu: ha_get_integration(entry_id="abc123", include_schema=True) lists menu_options; ha_set_integration(entry_id="abc123", menu_choice="<option>") opens that form
        - List template entries with their options: ha_get_integration(domain="template")
        - Only service integrations: ha_get_integration(integration_type="service")
        - An entry's panel commands: ha_get_integration(entry_id="abc123", include_ws_commands=True)

        OPTIONS: with the component, ``options`` are the stored mapping as-is,
        so a field never set may be absent and a section's fields stay nested
        under the section key (e.g. a template helper's ``additional_options``);
        values that match a ``secrets.yaml`` entry are returned as
        ``"**redacted**"``. Without it, include_schema=True reads ``options``
        from the options form: unset fields at their schema default, a
        section's fields at the top level. With redact_secrets on, every
        option value of an entry that has an options flow is redacted unless
        include_schema=True reads the form that names the password fields;
        an entry without an options flow has no such form, so its option
        values come back as stored.

        Each entry carries ``log_level``: the canonical Python logger level name
        (``DEBUG``/``INFO``/``WARNING``/``ERROR``/``CRITICAL``) when the
        integration has a log-level override (set one with
        ``ha_set_integration(log_level=...)``), or ``"DEFAULT"``
        (uppercase sentinel) when no override is set; and ``log_level_raw``: the
        original numeric level (e.g. ``10`` for DEBUG) when HA returned an int,
        ``None`` otherwise. This is distinct from the app side, where
        ``ha_get_app`` returns Supervisor's lowercase ``"default"`` literal — do
        not cross-compare.
        """
        try:
            include_opts = include_options
            include_schema_bool = include_schema
            include_diagnostics_bool = include_diagnostics
            include_subentries_bool = include_subentries
            include_subentry_schema_bool = include_subentry_schema
            show_advanced_options_bool = show_advanced_options
            exact_match_bool = exact_match
            limit_int = limit
            offset_int = offset
            fields_list = parse_diagnostics_fields(diagnostics_fields)
            truncate_bytes = diagnostics_truncate_at_bytes
            data_offset_int = (
                diagnostics_data_offset if diagnostics_data_offset is not None else 0
            )
            data_limit_int = diagnostics_data_limit
            # Type-guard ``diagnostics_data_path`` here so a bad caller (dict /
            # list) surfaces as ``VALIDATION_INVALID_PARAMETER`` instead of
            # leaking as ``INTERNAL_ERROR`` from the resolver's ``.strip()``
            # downstream.
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
            # Auto-enable options when domain filter is set. A blank domain is no
            # filter to Core, so it would list and probe every entry.
            if domain is not None:
                validate_identifier_not_empty(domain, "domain")
                include_opts = True

            # If entry_id provided, get specific config entry
            if entry_id is not None:
                resp = await self._get_entry_detail_response(
                    entry_id,
                    include_schema=include_schema_bool,
                    include_subentries=include_subentries_bool,
                    include_subentry_schema=include_subentry_schema_bool,
                    subentry_type=subentry_type,
                    subentry_id=subentry_id,
                    show_advanced_options=show_advanced_options_bool,
                    include_diagnostics=include_diagnostics_bool,
                    include_ws_commands=include_ws_commands,
                    device_id=device_id,
                    fields_list=fields_list,
                    truncate_bytes=truncate_bytes,
                    diagnostics_data_path=diagnostics_data_path,
                    data_offset_int=data_offset_int,
                    data_limit_int=data_limit_int,
                )
                if integration_type is not None:
                    resp.setdefault("warnings", []).append(
                        "integration_type was ignored: it filters the list "
                        "(no entry_id)"
                    )
                return resp

            # List mode - get all config entries
            result = await self._list_entries(
                domain,
                query,
                include_opts,
                exact_match_bool,
                limit_int,
                offset_int,
                integration_type=integration_type,
            )
            ignored_detail_params = self._ignored_diagnostics_detail_params(
                include_diagnostics=include_diagnostics_bool,
                include_ws_commands=include_ws_commands,
                device_id=device_id,
                fields_list=fields_list,
                truncate_bytes=truncate_bytes,
                diagnostics_data_path=diagnostics_data_path,
                data_offset_int=data_offset_int,
                data_limit_int=data_limit_int,
            ) + self._ignored_subentry_detail_params(
                include_subentries=include_subentries_bool,
                include_subentry_schema=include_subentry_schema_bool,
                subentry_type=subentry_type,
                subentry_id=subentry_id,
                show_advanced_options=show_advanced_options_bool,
            )
            if ignored_detail_params:
                result.setdefault("warnings", []).append(
                    f"{', '.join(ignored_detail_params)} "
                    "ignored because entry_id was not set (list mode)"
                )
            if include_schema_bool:
                result.setdefault("warnings", []).append(
                    "include_schema was ignored: it describes an entry's "
                    "options flow and needs entry_id. For an integration's "
                    "setup form, use ha_set_integration(domain=...)"
                )
            return result

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to get integrations: {e}")
            exception_to_structured_error(
                e,
                suggestions=[
                    "Verify Home Assistant connection is working",
                    "Check that the API is accessible",
                    "Ensure your token has sufficient permissions",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises

    async def _get_entry_detail_response(
        self,
        entry_id: str,
        *,
        include_schema: bool,
        include_subentries: bool,
        include_subentry_schema: bool,
        subentry_type: str | None,
        subentry_id: str | None,
        show_advanced_options: bool,
        include_diagnostics: bool,
        include_ws_commands: bool,
        device_id: str | None,
        fields_list: list[str] | None,
        truncate_bytes: int | None,
        diagnostics_data_path: str | None,
        data_offset_int: int,
        data_limit_int: int | None,
    ) -> dict[str, Any]:
        """Build the single-entry response, attaching diagnostics/commands when asked."""
        resp = await self._get_single_entry(
            entry_id,
            include_schema,
            include_subentries=include_subentries or include_subentry_schema,
            include_subentry_schema=include_subentry_schema,
            subentry_type=subentry_type,
            subentry_id=subentry_id,
            show_advanced_options=show_advanced_options,
        )
        if include_diagnostics:
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
                "device_id was provided but ignored because include_diagnostics=False"
            )
        if include_ws_commands:
            await attach_ws_commands(self._client, resp)
        return resp

    @staticmethod
    def _ignored_diagnostics_detail_params(
        *,
        include_diagnostics: bool,
        include_ws_commands: bool,
        device_id: str | None,
        fields_list: list[str] | None,
        truncate_bytes: int | None,
        diagnostics_data_path: str | None,
        data_offset_int: int,
        data_limit_int: int | None,
    ) -> list[str]:
        """Detail-only diagnostics/command params that are ignored in list mode."""
        ignored_detail_params: list[str] = []
        if include_diagnostics:
            ignored_detail_params.append("include_diagnostics")
        if include_ws_commands:
            ignored_detail_params.append("include_ws_commands")
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
        return ignored_detail_params

    @staticmethod
    def _ignored_subentry_detail_params(
        *,
        include_subentries: bool,
        include_subentry_schema: bool,
        subentry_type: str | None,
        subentry_id: str | None,
        show_advanced_options: bool,
    ) -> list[str]:
        """Detail-only subentry params that are ignored in list mode."""
        ignored_detail_params: list[str] = []
        if include_subentries:
            ignored_detail_params.append("include_subentries")
        if include_subentry_schema:
            ignored_detail_params.append("include_subentry_schema")
        if subentry_type is not None:
            ignored_detail_params.append("subentry_type")
        if subentry_id is not None:
            ignored_detail_params.append("subentry_id")
        if show_advanced_options:
            ignored_detail_params.append("show_advanced_options")
        return ignored_detail_params

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
            read = await fetch_config_entries(self._client, entry_id=entry_id)
            if read is not None:
                return await self._single_entry_from_component(
                    entry_id,
                    read,
                    include_schema,
                    include_subentries=include_subentries,
                    include_subentry_schema=include_subentry_schema,
                    subentry_type=subentry_type,
                    subentry_id=subentry_id,
                    show_advanced_options=show_advanced_options,
                )

            result = await self._client.get_config_entry(entry_id)
            entry_domain = result.get("domain") if isinstance(result, dict) else None
            if isinstance(result, dict):
                normalize_ignored_entry(result)

            # Core's entry fragment has no `options`, and only the entry's
            # options flow form shows them. With include_schema,
            # `fetch_options_schema` below fills them from that form.
            options_warnings: list[str] = []
            self._note_unread_legacy_options(
                result, entry_id, include_schema, options_warnings
            )

            resp: dict[str, Any] = {
                "success": True,
                "entry_id": entry_id,
                "entry": result,
            }
            if options_warnings:
                resp["warnings"] = options_warnings

            # Surface the effective Python logger level for this integration
            # so users can confirm log level changes took effect.
            # Emit unconditionally for symmetry with the list path (_format_entry).
            level_warnings: list[str] = []
            logger_levels = await get_logger_levels(self._client, level_warnings)
            level_info = logger_levels.get(entry_domain or "")
            # UNKNOWN, not DEFAULT: an unreadable level is not evidence that the
            # integration runs at the default one (#1947).
            resp["log_level"] = (
                "UNKNOWN"
                if level_warnings
                else (level_info["name"] if level_info else "DEFAULT")
            )
            resp["log_level_raw"] = level_info["raw"] if level_info else None
            if level_warnings:
                resp.setdefault("warnings", []).extend(level_warnings)

            # Describe the options flow's first step: started and aborted.
            if include_schema and result.get("supports_options"):
                await fetch_options_schema(self._client, entry_id, resp)

            if include_subentries:
                subentries = await self._fetch_config_subentries(entry_id)
                resp["subentry_count"] = len(subentries)
                resp["subentries"] = subentries

            if include_subentry_schema:
                await fetch_config_subentry_schema(
                    self._client,
                    entry_id,
                    resp,
                    subentry_type=subentry_type,
                    subentry_id=subentry_id,
                    show_advanced_options=show_advanced_options,
                )

            return resp
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"entry_id": entry_id},
                suggestions=[
                    "Use ha_get_integration() without entry_id to see all "
                    "config entries",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises

    @staticmethod
    def _note_unread_legacy_options(
        result: Any,
        entry_id: str,
        include_schema: bool | None,
        warnings: list[str],
    ) -> None:
        """Warn that a legacy REST entry's ``options`` were not read (#2987).

        Opening the options flow runs the integration's own code, so a read
        does it only for ``include_schema``, which asks for the flow's form
        and fills ``options`` from it. An entry without an options flow gets
        an empty ``options``: Core's fragment has none.
        """
        if not isinstance(result, dict):
            return
        if not result.get("supports_options"):
            result.setdefault("options", {})
        elif not include_schema:
            warnings.append(options_unread_warning(entry_id))

    async def _single_entry_from_component(
        self,
        entry_id: str,
        read: ComponentEntries,
        include_schema: bool | None,
        *,
        include_subentries: bool,
        include_subentry_schema: bool,
        subentry_type: str | None,
        subentry_id: str | None,
        show_advanced_options: bool,
    ) -> dict[str, Any]:
        """Build the single-entry response from a component ``config_entries`` read.

        The component row already carries the entry identity, its raw persisted
        ``options`` (secret-scrubbed), and its ``subentries`` identity rows — so
        this one read replaces the legacy ``get_config_entry`` read + OptionsFlow
        start/abort probe + subentries WS call. The options schema (and the
        subentry schema) still come from the legacy live flow: a schema only
        exists inside an open flow, which the component cannot serialize. When a
        schema is requested the component's ``options`` are kept
        (``populate_options=False``) rather than overwritten by the
        OptionsFlow-derived suggested-value shape.

        ``log_level`` / ``log_level_raw`` come from ``get_logger_levels`` on both
        paths (the component does not carry logger overrides).
        """
        if not read.rows:
            # ``async_get_entry(entry_id)`` found nothing — an authoritative
            # not-found, mapped to the same 404 get_config_entry raises.
            raise HomeAssistantAPIError(
                f"Config entry not found: {entry_id}", status_code=404
            )
        entry, subentries = _split_component_entry_row(read.rows[0])
        entry.setdefault("options", {})
        normalize_ignored_entry(entry)

        resp: dict[str, Any] = {
            "success": True,
            "entry_id": entry_id,
            "entry": entry,
        }
        if read.secret_scrub_degraded:
            resp["warnings"] = [SCRUB_DEGRADED_WARNING]

        # Surface the effective Python logger level for this integration
        # (unconditionally, for symmetry with the legacy path and _format_entry).
        level_warnings: list[str] = []
        logger_levels = await get_logger_levels(self._client, level_warnings)
        level_info = logger_levels.get(entry.get("domain") or "")
        # UNKNOWN, not DEFAULT — see the legacy path in _get_single_entry.
        resp["log_level"] = (
            "UNKNOWN"
            if level_warnings
            else (level_info["name"] if level_info else "DEFAULT")
        )
        resp["log_level_raw"] = level_info["raw"] if level_info else None
        if level_warnings:
            resp.setdefault("warnings", []).extend(level_warnings)

        # Component options are raw persisted values — under redact_secrets
        # they must be redacted on EVERY read (#2157). A read that asks for
        # the options form redacts by that form in fetch_options_schema
        # below, which opens the flow once; any other read opens no flow and
        # withholds every value (#2987).
        schema_read = bool(include_schema and entry.get("supports_options"))
        if (
            redaction_enabled()
            and not schema_read
            and withhold_component_options(entry)
        ):
            resp.setdefault("warnings", []).append(options_withheld_warning(entry_id))

        # Options schema only exists in a live options flow — read it from the
        # legacy flow, but keep the component-provided options (populate_options
        # False) so the raw persisted values win over the flow-derived shape.
        if schema_read:
            await fetch_options_schema(
                self._client,
                entry_id,
                resp,
                populate_options=False,
            )

        if include_subentries:
            resp["subentry_count"] = len(subentries)
            resp["subentries"] = subentries

        if include_subentry_schema:
            await fetch_config_subentry_schema(
                self._client,
                entry_id,
                resp,
                subentry_type=subentry_type,
                subentry_id=subentry_id,
                show_advanced_options=show_advanced_options,
            )

        return resp

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

    async def _list_entries(
        self,
        domain: str | None,
        query: str | None,
        include_opts: bool | None,
        exact_match: bool | None,
        limit_int: int,
        offset_int: int,
        integration_type: str | None = None,
    ) -> dict[str, Any]:
        """List config entries with optional domain/query filtering and pagination."""
        # Component fast path: one in-process read (domain filtered server-side,
        # options materialized on each row) replaces the per-entry OptionsFlow
        # probes; the REST list is read for integration_type only when the
        # component cannot filter by type.
        # Normalize the domain to HA's canonical lowercase: both the component
        # and Core's REST list filter by exact match.
        domain_norm = domain.strip().lower() if domain else None
        params: dict[str, str] = {}
        if domain_norm:
            params["domain"] = domain_norm
        if integration_type is not None:
            # A blank type matches no integration, so the list would come back
            # empty but for entries whose integration Core cannot find.
            params["type"] = validate_identifier_not_empty(
                integration_type, "integration_type"
            )
        read = await fetch_config_entries(
            self._client, domain=domain_norm, integration_type=params.get("type")
        )
        if read is not None:
            rows = read.rows
            if integration_type is not None and not read.type_filtered:
                # A component without the type filter: Core's REST list
                # decides which entries match the type.
                matching = {
                    entry.get("entry_id")
                    for entry in await self._fetch_rest_entries(params)
                }
                rows = [row for row in rows if row.get("entry_id") in matching]
            listed = await self._list_entries_from_component(
                rows,
                domain_norm,
                query,
                include_opts,
                exact_match,
                limit_int,
                offset_int,
            )
            if include_opts and read.secret_scrub_degraded:
                listed.setdefault("warnings", []).append(SCRUB_DEGRADED_WARNING)
            return listed

        response = await self._fetch_rest_entries(params)

        # Fetch current logger levels once; enrich each entry with its effective level.
        level_warnings: list[str] = []
        logger_levels = await get_logger_levels(self._client, level_warnings)

        formatted_entries = [
            self._format_entry(entry, include_opts, logger_levels, bool(level_warnings))
            for entry in response
        ]
        result = self._finalize_entry_list(
            formatted_entries,
            domain_norm,
            query,
            exact_match,
            limit_int,
            offset_int,
            level_warnings,
        )
        # Core's rows have no options, and a list opens no options flow to
        # read them (#2987): an entry that has one comes back without the key.
        if include_opts:
            unread = [e for e in result["entries"] if e.get("supports_options")]
            for entry in unread:
                entry.pop("options", None)
            if unread:
                result.setdefault("warnings", []).append(
                    options_unread_warning(entry_count(len(unread)))
                )
        return result

    async def _fetch_rest_entries(self, params: dict[str, str]) -> list[Any]:
        """Core's REST entry list, filtered by Core with ``params``."""
        response = await self._client._request(
            "GET", "/config/config_entries/entry", params=params or None
        )
        if not isinstance(response, list):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from Home Assistant",
                    context={"response_type": type(response).__name__},
                )
            )
        return response

    async def _list_entries_from_component(
        self,
        rows: list[dict[str, Any]],
        domain: str | None,
        query: str | None,
        include_opts: bool | None,
        exact_match: bool | None,
        limit_int: int,
        offset_int: int,
    ) -> dict[str, Any]:
        """List config entries from a component ``config_entries`` read.

        The component already filtered by ``domain`` (server-side) and
        materialized each entry's ``options`` on the row, so the read itself
        needs no per-entry OptionsFlow probe. ``options`` are raw persisted
        values (a field never set may be absent) and may contain
        ``"**redacted**"`` markers.
        With ``redact_secrets`` on, every option value of a returned entry
        that has an options flow is withheld: only that flow's form tells
        the password fields apart, and a list opens no flow (#2987).
        """
        level_warnings: list[str] = []
        logger_levels = await get_logger_levels(self._client, level_warnings)
        formatted_entries = [
            self._format_entry(row, include_opts, logger_levels, bool(level_warnings))
            for row in rows
        ]
        result = self._finalize_entry_list(
            formatted_entries,
            domain,
            query,
            exact_match,
            limit_int,
            offset_int,
            level_warnings,
        )
        # Component options are raw persisted values — under redact_secrets
        # every RETURNED row must be redacted (#2157).
        if include_opts and redaction_enabled():
            withheld = 0
            for formatted in result["entries"]:
                if withhold_component_options(formatted):
                    withheld += 1
            if withheld:
                result.setdefault("warnings", []).append(
                    options_withheld_warning(entry_count(withheld))
                )
        return result

    def _finalize_entry_list(
        self,
        formatted_entries: list[dict[str, Any]],
        domain: str | None,
        query: str | None,
        exact_match: bool | None,
        limit_int: int,
        offset_int: int,
        extra_warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        """Query-filter, summarize, and paginate formatted entries.

        Shared tail of the component-served and legacy list paths so their
        response shapes stay identical.
        """
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
            result_data["domain_filter"] = domain
        if extra_warnings:
            result_data.setdefault("warnings", []).extend(extra_warnings)
        return result_data

    @staticmethod
    def _format_entry(
        entry: dict[str, Any],
        include_opts: bool | None,
        logger_levels: dict[str, dict[str, Any]] | None = None,
        levels_unknown: bool = False,
    ) -> dict[str, Any]:
        """Copy the entry row minus ``options``/``subentries``; add the tool's fields.

        Every other key passes through (Core's fields, plus ``unique_id`` on the
        component path); ``supports_options`` and ``supports_reconfigure`` are
        forced off for an ignored entry.
        ``options`` comes back when ``include_opts`` is set.
        """
        formatted_entry: dict[str, Any] = {
            key: value
            for key, value in entry.items()
            if key not in ("options", "subentries")
        }
        normalize_ignored_entry(formatted_entry)

        # Surface the effective Python logger level for this integration
        # ("DEFAULT" = no override; falls back to the root logger level).
        # `log_level_raw` is the original numeric level (None when no override
        # exists or HA returned a string instead of an int).
        if logger_levels is not None:
            domain = entry.get("domain") or ""
            level_info = logger_levels.get(domain)
            # UNKNOWN, not DEFAULT: an unreadable level is not evidence that the
            # integration runs at the default one (#1947).
            formatted_entry["log_level"] = (
                "UNKNOWN"
                if levels_unknown
                else (level_info["name"] if level_info else "DEFAULT")
            )
            formatted_entry["log_level_raw"] = level_info["raw"] if level_info else None

        # Include options when requested (for auditing template definitions, etc.)
        if include_opts:
            formatted_entry["options"] = entry.get("options", {})

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
        name="ha_set_integration",
        tags={"Integrations"},
        annotations=write_hints(
            "Set Integration", destructive=True, idempotent=False, open_world=False
        ),
    )
    @with_auto_backup(
        domain="integration",
        id_param="entry_id",
        domain_resolver=resolve_config_entry_backup_domain,
        # A ws_command changes the integration's own storage, not the entry
        # the snapshot holds. Any other flow call can save, since Core runs
        # the step when a flow opens and on a re-read; an abort cannot.
        skip_fn=lambda kwargs: (
            bool(kwargs.get("abort"))
            or kwargs.get("log_level") is not None
            or kwargs.get("ws_command") is not None
        ),
    )
    @log_tool_usage
    async def ha_set_integration(
        self,
        entry_id: Annotated[
            str | None,
            Field(
                description=("Config entry ID of an existing integration."),
                default=None,
            ),
        ] = None,
        enabled: Annotated[
            bool | None,
            Field(
                description=(
                    "True to enable, False to disable the entry. Mutually "
                    "exclusive with 'domain' and 'config'."
                ),
                default=None,
            ),
        ] = None,
        domain: Annotated[
            str | None,
            Field(
                description=(
                    "Integration domain to add (e.g. 'workday', 'local_calendar')."
                ),
                default=None,
            ),
        ] = None,
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "The values for the form the flow is at: one form per "
                    "call. Without flow_id it answers the new flow's first "
                    "form (after menu_choice, when the flow opens with a "
                    "menu). A key the form does not declare is refused before "
                    "anything is sent. On an existing entry (options, "
                    "reconfigure) a field you omit keeps its current value, "
                    "and a field set to null is cleared where the "
                    "integration's schema allows that field to be empty."
                ),
                default=None,
            ),
        ] = None,
        reconfigure: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Use the existing config entry's official reconfigure flow "
                    "(its connection settings) instead of its options flow. "
                    "Every call except an abort checks the entry's identity "
                    "first. Without flow_id and config this opens the flow and "
                    "returns its first step; nothing is submitted. Continue "
                    "with the flow_id this entry's call returned: Home "
                    "Assistant does not say which entry a reconfigure flow "
                    "edits."
                ),
            ),
        ] = False,
        expected_device_id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Requires reconfigure=True. Device registry ID the entry "
                    "must still own."
                ),
            ),
        ] = None,
        expected_unique_id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Requires reconfigure=True, AND the ha_mcp_tools custom "
                    "component: Home Assistant does not expose a config "
                    "entry's unique_id over its API. Without the component "
                    "this is rejected — anchor on expected_device_id, "
                    "expected_mac or expected_entity_ids instead."
                ),
            ),
        ] = None,
        expected_mac: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Requires reconfigure=True. MAC or IEEE the entry's device "
                    "must still report."
                ),
            ),
        ] = None,
        expected_entity_ids: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Requires reconfigure=True. Exact entity IDs that must "
                    "remain associated with the entry."
                ),
            ),
        ] = None,
        log_level: Annotated[
            Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "DEFAULT"] | None,
            Field(
                default=None,
                description=(
                    "Set the integration's log level, like the integration "
                    "page's Enable/Disable debug logging: pass the integration "
                    "with 'domain' (no config flow runs) or 'entry_id', and "
                    "no other mode's parameters (log_persistence may accompany "
                    "it). Lasts through the next Home Assistant "
                    "restart unless log_persistence says otherwise. DEFAULT "
                    "clears the override: the integration then logs at the "
                    "inherited default level, and a level set for it in "
                    "configuration.yaml returns only after a restart."
                ),
            ),
        ] = None,
        log_persistence: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Only with log_level: how long Home Assistant keeps the "
                    "level. 'once' (default) through the next restart, "
                    "'permanent' after every restart until changed, 'none' "
                    "until the next restart. Home Assistant validates the value."
                ),
            ),
        ] = None,
        menu_choice: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "The option to pick at a menu step: with flow_id the "
                    "menu the flow is at, without it the menu a new flow "
                    "opens with. Refused when the step is not a menu or does "
                    "not offer the option."
                ),
            ),
        ] = None,
        subentry_type: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "With entry_id: the integration-defined subentry type "
                    "whose form to describe as subentry_schema; the flow is "
                    "aborted afterwards, unless a menu_choice finished it "
                    "(flow_type create_entry: saved)."
                ),
            ),
        ] = None,
        subentry_id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "With entry_id and subentry_type: the existing subentry "
                    "whose reconfigure form to describe."
                ),
            ),
        ] = None,
        flow_id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Continue an open flow: the flow_id a previous call "
                    "returned, with the same domain or entry_id (and "
                    "reconfigure=True for a reconfigure flow), until ha-mcp "
                    "restarts. With config or "
                    "menu_choice it answers the step; alone it returns the "
                    "step again."
                ),
            ),
        ] = None,
        abort: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "With flow_id: end the open flow without saving. A flow "
                    "Home Assistant no longer has, or one ha-mcp did not open "
                    "(or opened before it restarted), is an error."
                ),
            ),
        ] = False,
        ws_command: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "With entry_id: one of the integration's own '<domain>/' "
                    "WebSocket commands to send, as its panel in the HA UI "
                    "does (e.g. 'knx/get_knx_project'). Its result is "
                    "returned under result. Streaming commands are refused."
                ),
            ),
        ] = None,
        ws_data: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "The ws_command's arguments, without 'type' and 'id'. "
                    "Home Assistant checks them against the command's "
                    "registered schema and returns its error when they do "
                    "not match."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage an integration (config entry): enable/disable, add, update options, reconfigure, or send its own commands.

        Modes (pick one):
        - Enable/disable: entry_id + enabled.
        - Add integration: domain (+ menu_choice, + config) — runs the
          domain's config flow one step per call, as the HA UI does.
        - Update options: entry_id (+ menu_choice, + config) — the entry's
          options flow (what the "Configure" button does in the HA UI), one
          step per call.
        - Reconfigure: entry_id + reconfigure=True — connection settings
          such as host, port, credentials, one step per call. Each call
          except an abort first checks the entry's identity; the first call
          without config returns the flow's first step.
        - Log level: log_level + domain (or entry_id) — sets how much the
          integration logs; read it back with ha_get_integration's log_level.
        - Integration command: entry_id + ws_command (+ ws_data) — what the
          integration's panel sends (KNX project, entities, telegrams).
          Home Assistant marks none of these commands as read-only, so a read
          such as knx/get_knx_project goes through this tool too.

        Flows, one step per call: a call answers the step the flow is at and
        returns Home Assistant's next answer. A form comes back with its
        fields (type, default, current value, Home Assistant's help text) and
        flow_id; a menu with menu_options. Answer with flow_id and config (a
        form) or menu_choice (a menu); flow_id alone returns the step again,
        and flow_id with abort=True ends the flow. Without flow_id a new flow
        starts: its first form gets config, and without config the call
        returns that first step. A form Home Assistant rejects is an error
        with errors and error_text (Core's text), and the flow stays open to
        correct it. A flow stays open until Home Assistant finishes
        it or you abort it; while a setup flow is open, Home Assistant
        refuses another one for the same device as already_in_progress.

        WHEN NOT TO USE:
        - Helpers (template, group, utility_meter, ...): use
          ha_config_set_helper. `otp` is the user's to set up in the HA UI: its
          secret is a credential enrolled in an authenticator app; do not create it.
        - Config subentries: use
          ha_config_set_helper(helper_type='config_subentry'); subentry_type
          here describes their form; a menu_choice that finishes the flow
          saves the subentry.
        - Removing an entry: use ha_remove_helpers_integrations.
        - ha-mcp's own ha_mcp_tools entries are refused: manage them in the HA
          UI, or (server settings, developer mode on) ha_dev_manage_settings /
          ha_dev_manage_server.

        Use ha_get_integration() to find entry IDs, and
        ha_get_integration(entry_id=..., include_schema=True) to inspect the
        options fields. Its supports_reconfigure field tells you whether an
        entry qualifies for reconfigure=True, and
        ha_get_integration(entry_id=..., include_ws_commands=True) lists the
        commands ws_command accepts with their schemas.

        Caveats: adding an integration runs its config flow exactly as the HA
        UI would (may pair devices, scan the network, create entities). Flows
        requiring a browser step (OAuth) are refused at that step and
        aborted. A progress step (Home Assistant working in the background)
        is waited out for up to 120 s; after that the error carries flow_id
        to ask again. When Home Assistant does not answer a submit or a
        progress poll (or, on reconfigure, answers with any 5xx), the error
        carries status applied_but_unverified: the entry may exist or the
        change may be saved, so check it before retrying. An aborted flow
        returns Core's own text next to the key (reason_text). Reconfigure
        edits the settings a live integration connects with: a wrong host or
        credential takes it offline, and there is no automatic rollback. A
        finished reconfigure waits for the reload Home Assistant runs and
        reports the entry's state as entry_state: status applied_and_verified
        once it is loaded, applied_but_unverified otherwise.

        EXAMPLES:
        - Add: ha_set_integration(domain="local_calendar", config={"calendar_name": "Chores"})
        - Add in two steps: ha_set_integration(domain="knx", config={"connection_type": "routing"}) returns the routing form and flow_id, then ha_set_integration(domain="knx", flow_id="...", config={})
        - Answer a menu: ha_set_integration(domain="...", flow_id="...", menu_choice="<one of menu_options>")
        - See the setup form first: ha_set_integration(domain="workday")
        - Update options: ha_set_integration(entry_id="abc123", config={"scan_interval": 30})
        - Reconfigure: ha_set_integration(entry_id="abc123", reconfigure=True), then ha_set_integration(entry_id="abc123", reconfigure=True, flow_id="...", config={"host": "10.0.0.5"})
        - Cancel a flow: ha_set_integration(domain="knx", flow_id="...", abort=True)
        - Debug logging: ha_set_integration(domain="zha", log_level="DEBUG"), then log_level="DEFAULT" to stop
        - KNX project: ha_set_integration(entry_id="abc123", ws_command="knx/get_knx_project")
        """
        # Only reconfigure takes these; every other mode refuses them.
        reconfigure_guards: dict[str, Any] = {
            "expected_device_id": expected_device_id,
            "expected_unique_id": expected_unique_id,
            "expected_mac": expected_mac,
            "expected_entity_ids": expected_entity_ids,
        }
        try:
            _reject_log_persistence_without_level(log_persistence, log_level)
            _reject_reconfigure_conflicts(
                reconfigure, reconfigure_guards, domain, enabled
            )
            flow_args = {
                "flow_id": flow_id,
                "menu_choice": menu_choice,
                "abort": abort or None,
            }
            if ws_command is not None or ws_data is not None:
                reject_ws_command_conflicts(
                    entry_id,
                    ws_command,
                    domain=domain,
                    enabled=enabled,
                    config=config,
                    log_level=log_level,
                    subentry_type=subentry_type,
                    subentry_id=subentry_id,
                    reconfigure=reconfigure or None,
                    **flow_args,
                )
                return await send_ws_command(
                    self._client,
                    validate_identifier_not_empty(entry_id, "entry_id"),
                    validate_identifier_not_empty(ws_command, "ws_command"),
                    ws_data,
                )
            if subentry_type is not None or subentry_id is not None:
                modes = {
                    "enabled": enabled is not None,
                    "config": config is not None,
                    "reconfigure": reconfigure,
                    "log_level": log_level is not None,
                    "flow_id": flow_id is not None,
                    "abort": abort,
                }
                return await describe_flow_form(
                    self._client,
                    domain=domain,
                    entry_id=entry_id,
                    subentry_type=subentry_type,
                    subentry_id=subentry_id,
                    menu_choice=menu_choice,
                    other_modes=[name for name, given in modes.items() if given],
                )
            if log_level is not None:
                return await set_log_level(
                    self._client,
                    entry_id,
                    domain,
                    log_level,
                    other_modes=config is not None
                    or enabled is not None
                    or reconfigure
                    or any(v is not None for v in flow_args.values()),
                    persistence=log_persistence,
                )

            if reconfigure:
                return await run_integration_step(
                    self._client,
                    domain=None,
                    entry_id=entry_id,
                    flow_id=flow_id,
                    config=config,
                    menu_choice=menu_choice,
                    abort=abort,
                    expected_identity={
                        "device_id": expected_device_id,
                        "unique_id": expected_unique_id,
                        "mac": expected_mac,
                        "entity_ids": list(expected_entity_ids or []),
                    },
                )

            _reject_set_integration_mode_conflicts(
                entry_id,
                domain,
                enabled,
                config,
                flow_args=[name for name, value in flow_args.items() if value],
            )

            if domain is not None:
                validate_identifier_not_empty(
                    domain,
                    "domain",
                    suggestions=[
                        "Pass the integration domain, e.g. 'workday' or "
                        "'local_calendar'",
                    ],
                )
                return await run_integration_step(
                    self._client,
                    domain=domain.strip().lower(),
                    entry_id=None,
                    flow_id=flow_id,
                    config=config,
                    menu_choice=menu_choice,
                    abort=abort,
                )

            # Empty/whitespace entry_id would surface as a misleading HA
            # "config entry not found" from the backend call.
            entry_id = validate_identifier_not_empty(
                entry_id,
                "entry_id",
                suggestions=[
                    "Use ha_get_integration() to find valid config entry IDs",
                ],
            )

            if enabled is not None:
                return await set_entry_enabled(self._client, entry_id, enabled)

            # Options mode: one step of the entry's options flow.
            return await run_integration_step(
                self._client,
                domain=None,
                entry_id=entry_id,
                flow_id=flow_id,
                config=config,
                menu_choice=menu_choice,
                abort=abort,
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to set integration: {e}")
            error_context = self._set_integration_error_context(entry_id, domain)
            exception_to_structured_error(
                e,
                context=error_context,
                suggestions=[
                    "Verify the integration domain is spelled correctly and "
                    "is installed (custom integrations must be installed "
                    "before a config flow can start)",
                ]
                if domain is not None
                else [
                    "Use ha_get_integration() to find valid config entry IDs",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises

    @staticmethod
    def _set_integration_error_context(
        entry_id: str | None, domain: str | None
    ) -> dict[str, Any]:
        """Build the error-context dict for ha_set_integration failures."""
        error_context: dict[str, Any] = {}
        if entry_id is not None:
            error_context["entry_id"] = entry_id
        if domain is not None:
            error_context["domain"] = domain
        return error_context

    @tool(
        name="ha_remove_helpers_integrations",
        tags={"Helper Entities", "Integrations"},
        annotations=write_hints(
            "Remove Helper or Integration",
            destructive=True,
            idempotent=True,
            open_world=False,
        ),
    )
    @with_auto_backup(
        domain_fn=_removal_backup_domain,
        id_fn=removal_backup_id,
        domain_resolver=resolve_config_entry_backup_domain,
        skip_fn=_skip_removal_capture,
    )
    @log_tool_usage
    async def ha_remove_helpers_integrations(
        self,
        target: Annotated[
            str,
            Field(
                description=(
                    "What to remove. One of: "
                    "(a) bare helper_id for SIMPLE helpers: the helper's "
                    "storage id, not the name of its entity, e.g. 'my_button'; "
                    "(b) full entity_id, "
                    "e.g. 'input_button.my_button' or 'sensor.my_meter'; "
                    "(c) config entry_id for any integration, with helper_type "
                    "omitted, e.g. value from ha_get_integration(); "
                    "(d) parent config entry_id for a config subentry."
                )
            ),
        ],
        helper_type: Annotated[
            HelperTypeLiteral | None,
            Field(
                description=(
                    "Helper type of target; ROUTING says which value takes "
                    "which removal path."
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
            bool,
            Field(
                description="Must be True to confirm removal.",
                default=False,
            ),
        ] = False,
        wait: Annotated[
            bool,
            Field(
                description=(
                    "Wait for entity removal. Default: True. "
                    "Ignored for a direct config entry delete (an entry_id "
                    "target) or helper_type='config_subentry' (no entity "
                    "poll, require_restart returned)."
                ),
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Remove a Home Assistant helper or integration config entry.

        Unifies three backend removal mechanisms — simple-helper websocket
        delete, config-entry delete, and config-subentry delete — behind one
        entry point; helper_type picks the path, or the entity registry does
        when an entity_id comes without one.

        WHEN NOT TO USE:
        - Removing only an entity (without deleting its underlying helper or
          config entry) — use `ha_remove_entity` instead.
        - YAML-configured helpers — they have no storage backend. Edit the
          YAML file and reload the relevant integration.
        - ha-mcp's own ha_mcp_tools entries are refused: manage them in the HA
          UI, or (server settings, developer mode on) ha_dev_manage_settings /
          ha_dev_manage_server.

        ROUTING:
        - SIMPLE helper_type (input_button, input_boolean, input_select,
          input_number, input_text, input_datetime, counter, timer, schedule,
          zone, person, tag) + bare helper_id (its storage id) or entity_id →
          websocket delete.
        - FLOW helper_type (any helper flow Home Assistant lists: template, group,
          utility_meter, derivative, ...; custom helper integrations too)
          + full entity_id → resolve entity_id to
          config_entry_id via entity_registry, then delete the config entry. All
          sub-entities (e.g. utility_meter tariffs) are removed together. An
          entity registered by another integration is refused with
          VALIDATION_INVALID_PARAMETER and nothing is deleted.
        - helper_type=None + entity_id → the entity's registry entry names its
          helper (storage type or helper flow); an entity of any other
          integration is refused with VALIDATION_INVALID_PARAMETER.
        - helper_type=None + entry_id → direct config entry delete (any
          integration).
        - helper_type="config_subentry" + parent entry_id + subentry_id →
          delete one config subentry.

        A target that cannot be removed raises a structured error rather than
        returning silent success: ENTITY_NOT_FOUND when the entity_id is absent
        from both the state machine and the entity registry, or for a bare
        helper_id no stored helper has (a bare helper_id on a FLOW target also
        raises it — FLOW resolution needs a full entity_id); RESOURCE_NOT_FOUND
        when it exists but is not storage- or registry-managed (a YAML-configured helper, or an entity with a state
        but no registry entry), for a config entry the backend reports as 404,
        or for a missing config subentry.
        Calling N times gives the same response. Transient connectivity failures
        raise their own codes (WEBSOCKET_DISCONNECTED, CONNECTION_FAILED) so
        retry logic can branch separately.

        EXAMPLES:
        - Remove SIMPLE button: ha_remove_helpers_integrations(target="my_button", helper_type="input_button", confirm=True)
        - Remove FLOW utility_meter (any sub-entity works): ha_remove_helpers_integrations(target="sensor.energy_peak", helper_type="utility_meter", confirm=True)
        - Remove whatever helper owns an entity: ha_remove_helpers_integrations(target="sensor.energy_peak", confirm=True)
        - Remove any integration by entry_id: ha_remove_helpers_integrations(target="01HXYZ...", confirm=True)
        - Remove a config subentry: ha_remove_helpers_integrations(target="01HXYZ...", helper_type="config_subentry", subentry_id="subentry-123", confirm=True)

        WARNING: Removing a helper or integration that is referenced by
        automations, scripts, or other integrations may cause those to fail.
        Use ha_search() / ha_get_integration() to verify before removal.
        ha_manage_backup(scope="edits", action="restore") recreates removed FLOW
        helpers whose options could be snapshotted, and config subentries. It
        cannot recreate custom helper integrations (their options are withheld)
        or otp (no stored options); re-add those yourself, otp through the user
        in the HA UI.
        """
        # === Confirm gate (uniform for every path) ===
        if not confirm:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Deletion not confirmed. Set confirm=True to proceed.",
                    context={
                        "target": target,
                        "helper_type": helper_type,
                        "warning": (
                            "This will delete the helper or integration. "
                            "Recovery is not guaranteed."
                        ),
                    },
                )
            )

        # === Empty/whitespace target gate (uniform for every path) ===
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
                "For simple helpers, use ha_search() to find the helper_id",
                "For flow helpers, use ha_search() to find an entity_id",
            ],
            context={"helper_type": helper_type},
        )

        wait_bool = wait
        warnings: list[str] = []

        # === Routing dispatch ===
        if helper_type is None and "." in target:
            # An entity_id alone: its registry entry names the helper, and the
            # resolved call runs the matching path with its own backup.
            resolved_type, resolved_target = await self._resolve_helper_entity(target)
            resolved: dict[str, Any] = await self._remove_resolved_helper(
                target=resolved_target, helper_type=resolved_type, wait=wait_bool
            )
            return resolved

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

        # Path 2: FLOW helper via entity_id → config_entry_id lookup; a type
        # Home Assistant does not list as a helper flow is refused first.
        await require_helper_type(self._client, helper_type)
        return await self._delete_flow_helper(helper_type, target, wait_bool, warnings)

    # Private helpers keep the ``_delete_*`` prefix because they wrap HA's
    # own backend verb — the WebSocket API is ``<type>/delete`` and the
    # REST API is HTTP DELETE. The public tool surface uses ``remove`` to
    # join the ``ha_remove_*`` behavioural family; the prefix asymmetry is
    # intentional and prevents future renames pulled by either side.

    @with_auto_backup(
        domain_fn=_removal_backup_domain,
        id_fn=removal_backup_id,
        domain_resolver=resolve_config_entry_backup_domain,
        skip_fn=_skip_removal_capture,
    )
    async def _remove_resolved_helper(
        self, *, target: str, helper_type: HelperTypeLiteral, wait: bool
    ) -> dict[str, Any]:
        """Remove the helper an entity_id resolved to, under the tool's backup.

        Not the public tool itself, so the removal logs a single tool call.
        """
        if helper_type in SIMPLE_HELPER_TYPES:
            return await self._delete_simple_helper(helper_type, target, wait)
        return await self._delete_flow_helper(helper_type, target, wait, [])

    async def _resolve_helper_entity(
        self, entity_id: str
    ) -> tuple[HelperTypeLiteral, str]:
        """Map an entity_id to the (helper_type, target) that removes its helper."""
        try:
            return await resolve_helper_entity(self._client, entity_id)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            # Keep the classified suggestions (auth, connection) and add the
            # entry_id route, which does not resolve the entity in the registry.
            error = exception_to_structured_error(
                e, context={"target": entity_id}, raise_error=False
            )
            error["error"].setdefault("suggestions", []).append(
                "Or pass the helper's config entry_id as target "
                "(ha_get_integration() lists config entries)."
            )
            raise_tool_error(error)
            return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    # === Path 3: Direct config entry delete (any integration) ===
    async def _delete_direct_entry(self, entry_id: str) -> dict[str, Any]:
        """Delete a config entry directly via the REST delete API.

        Looks the entry up first and refuses ha-mcp's own ``ha_mcp_tools``
        entries before any DELETE is sent.
        """
        try:
            entry = await self._client.get_config_entry(entry_id)
            refuse_component_entry(
                entry.get("domain"),
                action="delete a config entry of",
                entry_id=entry_id,
            )
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
            # A missing entry surfaces as HomeAssistantAPIError with
            # status_code=404 from the own-entry lookup
            # (RestClient.get_config_entry), before any DELETE is sent, or
            # from the DELETE itself when the entry vanished in between
            # (RestClient.delete_config_entry). Surface it as
            # RESOURCE_NOT_FOUND so callers can distinguish an absent target
            # from a real failure; the typo case (agent passed the wrong
            # entry_id) is the failure mode that "absent → success" would
            # silently mask. Non-404 API errors are real failures and bubble
            # through exception_to_structured_error below.
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
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"entry_id": entry_id},
                suggestions=[
                    "Use ha_get_integration() without entry_id to "
                    "see all config entries",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

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
            entry_id, reason = await get_entry_id_for_flow_helper(
                client, helper_type, target, warnings
            )
            if entry_id is None:
                raise_flow_helper_lookup_error(
                    reason, helper_type, target, detail="; ".join(warnings) or None
                )

            result: dict[str, Any] = await self._delete_resolved_flow_helper(
                helper_type=helper_type,
                target=target,
                entry_id=entry_id,
                wait_bool=wait_bool,
                warnings=warnings,
            )
            return result

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={
                    "helper_type": helper_type,
                    "target": target,
                },
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify the target exists using ha_search() "
                    + "or ha_get_integration()",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises

    @with_auto_backup(
        domain_fn=flow_helper_backup_domain,
        id_fn=lambda kw: str(kw.get("entry_id") or ""),
        skip_fn=skip_unless_flow_helper,
    )
    async def _delete_resolved_flow_helper(
        self,
        *,
        helper_type: HelperTypeLiteral,
        target: str,
        entry_id: str,
        wait_bool: bool,
        warnings: list[str],
    ) -> dict[str, Any]:
        """Remove a resolved flow entry, capturing Templates under their write lock."""
        client = self._client
        # Step 2: collect sub-entity IDs for the wait phase
        sub_entities = await _get_entities_for_config_entry(client, entry_id, warnings)
        entity_ids = [e["entity_id"] for e in sub_entities if "entity_id" in e]

        # Step 3: delete the config entry
        delete_result = await self._delete_flow_config_entry(
            entry_id, target, helper_type
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
            # The entry is already gone: a failed check is a warning, as in the
            # sibling delete tools, never an error for a delete that worked.
            checks = dict(zip(entity_ids, results, strict=True))
            failed = {
                eid: res
                for eid, res in checks.items()
                if isinstance(res, BaseException)
            }
            for eid, exc in failed.items():
                if not isinstance(
                    exc, (HomeAssistantConnectionError, HomeAssistantAuthError)
                ):
                    logger.warning(
                        f"Unexpected error verifying removal of {eid}: {exc}",
                        exc_info=exc,
                    )
            if failed:
                response.setdefault("warnings", []).append(
                    "Deletion confirmed but removal verification failed: "
                    + "; ".join(f"{eid}: {exc}" for eid, exc in sorted(failed.items()))
                )
            not_removed = [
                eid
                for eid, res in checks.items()
                if res is not True and eid not in failed
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

    async def _delete_flow_config_entry(
        self, entry_id: str, target: str, helper_type: HelperTypeLiteral
    ) -> Any:
        """Delete the resolved config entry, mapping a delete-time 404 to NOT_FOUND.

        TOCTOU window: entry_id resolved at step 1 may be gone before the DELETE
        reaches HA. A 404 surfaces as RESOURCE_NOT_FOUND so a concurrent removal
        is not masked as success; non-404 errors bubble through
        exception_to_structured_error.
        """
        try:
            return await self._client.delete_config_entry(entry_id)
        except HomeAssistantAPIError as e:
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
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={
                    "entry_id": entry_id,
                    "target": target,
                    "helper_type": helper_type,
                },
            )
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

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

        A bare id is a storage id (``stored_helper_target``), as
        ``ha_config_set_helper`` and the backups resolve it, and an entity_id
        goes by its registry entry: one read through the component, or the
        3-retry lookup without it. A target still unresolved is classified by
        why the registry has no usable record.
        """
        try:
            stored = await stored_helper_target(self._client, helper_type, target)
            if stored is not None:
                storage_id, entity, reason = stored
                response = await self._delete_simple_via_unique_id(
                    helper_type, storage_id, target, entity, wait_bool
                )
                return removal_reply(response, entity, reason)
            # stored_helper_target refuses a bare id no stored helper has.
            entity_id = target
            (
                unique_id,
                registry_result,
                component_used,
            ) = await self._resolve_helper_unique_id(entity_id)

            if not unique_id:
                return await self._delete_simple_helper_fallback(
                    helper_type,
                    target,
                    entity_id,
                    registry_result,
                    component_used,
                )

            return await self._delete_simple_via_unique_id(
                helper_type, unique_id, target, entity_id, wait_bool
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"helper_type": helper_type, "target": target},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify target exists using ha_search()",
                    "Ensure helper is not used by automations or scripts",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises

    async def _resolve_helper_unique_id(
        self, entity_id: str
    ) -> tuple[str | None, dict[str, Any] | None, bool]:
        """Resolve a SIMPLE helper's unique_id from the entity registry.

        Returns ``(unique_id, registry_result, component_used)``. When the
        component advertises registry_lookup, ONE in-process read replaces the
        3-attempt exponential-backoff loop. That loop absorbed the
        registry-registration LAG between a helper's creation and its entity
        landing in the registry index (not a WS-timing race — the legacy read
        hits the same live registry over the same socket); the single read is
        equally subject to that lag, but a bare id is resolved by its storage
        id before this read, and a stale/missing resolve is classified, never
        turned into a delete by a guessed id.
        On capability miss / component error the legacy retry loop runs
        unchanged. ``component_used`` records which path served the read so the
        exhausted-fallback detail can word the "3 attempts" text accurately.
        """
        client = self._client
        component = await resolve_entities_via_component(client, [entity_id])
        if component is not None:
            found = component.get("entities") or []
            if found:
                # Shape a config/entity_registry/get-style ack so the
                # exhausted-fallback err_detail below reads registry_result
                # uniformly across both the component and legacy paths.
                registry_result: dict[str, Any] = {"success": True, "result": found[0]}
                unique_id = found[0].get("unique_id")
                if unique_id:
                    logger.info(f"Found unique_id: {unique_id} for {entity_id}")
                return unique_id, registry_result, True
            # Assignment form (not a dict literal in the return) keeps the
            # no-return-success-false AST rule scoped to real tool returns:
            # this is an internal registry-ack shape, not an MCP tool response.
            miss_result: dict[str, Any] = {
                "success": False,
                "error": "not found in entity registry",
            }
            return None, miss_result, True
        unique_id, legacy_result = await self._resolve_unique_id_via_registry_retry(
            entity_id
        )
        return unique_id, legacy_result, False

    async def _resolve_unique_id_via_registry_retry(
        self, entity_id: str
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Legacy 3-retry registry lookup for a helper's unique_id.

        Returns ``(unique_id, registry_result)`` — ``registry_result`` is the
        last WebSocket response (or None if none arrived), used by the caller's
        exhausted-fallback detail.
        """
        client = self._client
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
                    wait_time = _REGISTRY_RETRY_BASE_DELAY * (2**attempt)
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
                    wait_time = _REGISTRY_RETRY_BASE_DELAY * (2**attempt)
                    await asyncio.sleep(wait_time)
        return unique_id, registry_result

    async def _delete_simple_helper_fallback(
        self,
        helper_type: HelperTypeLiteral,
        target: str,
        entity_id: str,
        registry_result: dict[str, Any] | None,
        component_used: bool,
    ) -> dict[str, Any]:
        """Classify a SIMPLE-helper target that names no stored helper.

        No stored id matched (``stored_helper_target``) and the registry
        lookup yielded no unique_id; deleting by the object id would guess a
        storage id that can belong to another helper's renamed entity. The
        target is absent from state and registry, or registered without a
        unique_id (ENTITY_NOT_FOUND); absent from state but still registered
        (SERVICE_CALL_FAILED); present but not registry-managed
        (RESOURCE_NOT_FOUND); or behind a failed registry read (its code).
        Always raises a structured error.
        """
        # Confirm via the registry too — a disabled entity is
        # state-absent but still registry-resident, so
        # state-absence alone is not enough to classify as
        # confirmed-absent. The APIError-404 branch routes the
        # never-existed-target case (HA returns 404 on
        # get_entity_state for unknown entity_ids) into the same
        # confirmed-absent path so the resulting ENTITY_NOT_FOUND
        # raise carries the structured "typo or removed" hint
        # message rather than a raw 404.
        if await self._state_absent(entity_id):
            if not await self._registry_still_has_entry(target, entity_id):
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
                            f"Helper {entity_id} not found. May indicate "
                            "it was already removed, never "
                            "existed, or the identifier is a "
                            "typo. Verify with "
                            "ha_search() before "
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
                        "its registry entry exists, but no "
                        "unique_id to delete it by was read from it."
                    ),
                    suggestions=[
                        "Re-enable the entity via "
                        + "ha_set_entity(enabled=True), then retry "
                        + "deletion.",
                        "Or inspect the entity registry entry "
                        + "directly to confirm unique_id presence.",
                    ],
                    context={
                        "target": target,
                        "entity_id": entity_id,
                    },
                )
            )

        # The state is present (an absent state raised above). If the
        # registry definitely has no entry — a component miss, or Core's
        # not_found — the entity is not registry-managed rather than missing.
        registry_miss = not (registry_result or {}).get("success") and (
            component_used or (registry_result or {}).get("error_code") == "not_found"
        )
        if registry_miss:
            raise_unregistered_entity_error(target, helper_type, entity_id)
        if not (registry_result or {}).get("success"):
            # The state shows the entity exists; report the read failure.
            _raise_registry_read_failure(target, entity_id, registry_result)

        # The registry returned an entry, but without a unique_id. The
        # component path resolves via ONE authoritative in-process lookup (no
        # retry loop), so its text must not claim "3 attempts".
        if component_used:
            not_found_detail = (
                f"Component registry lookup found no unique_id for {entity_id}."
            )
        else:
            not_found_detail = (
                f"Registry entry for {entity_id} has no unique_id after 3 attempts."
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                not_found_detail,
                suggestions=[
                    "Helper may not be properly registered or was "
                    "already deleted. Use ha_search() to "
                    "verify.",
                ],
                context={"target": target, "entity_id": entity_id},
            )
        )
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    async def _state_absent(self, entity_id: str) -> bool:
        """Return True when ``entity_id`` is absent from the state machine.

        A non-404 APIError (transient/auth) is re-raised so it is not
        mis-classified as a missing target; a 404 is treated as state-absent.
        """
        return not await get_entity_state_or_none(self._client, entity_id)

    async def _registry_still_has_entry(self, target: str, entity_id: str) -> bool:
        """Return True if ``entity_id`` still has an entity-registry entry.

        Only Core's ``not_found``, or its ``invalid_format`` for an id that
        cannot name an entity, confirms absence. Any other failed read, and a
        reply without an entry, is raised as the read failure, so a disabled
        helper behind a blocked read is not reported as already removed.
        """
        reply = await self._client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
        if (reply or {}).get("success"):
            if (reply.get("result") or {}).get("entity_id"):
                return True
            reply = {"error": "the reply carried no registry entry"}
        elif (reply or {}).get("error_code") in ("not_found", "invalid_format"):
            return False
        _raise_registry_read_failure(target, entity_id, reply)
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    async def _delete_simple_via_unique_id(
        self,
        helper_type: HelperTypeLiteral,
        unique_id: str,
        target: str,
        entity_id: str | None,
        wait_bool: bool,
    ) -> dict[str, Any]:
        """Delete a SIMPLE helper by its storage id; ``entity_id`` is None when
        the helper has no entity in the registry."""
        client = self._client
        delete_message: dict[str, Any] = {
            "type": f"{helper_type}/delete",
            f"{helper_type}_id": unique_id,
        }
        logger.info(f"Sending WebSocket delete: {delete_message}")
        result = await client.send_websocket_message(delete_message)
        logger.info(f"WebSocket delete response: {result}")

        if result.get("success"):
            response: dict[str, Any] = {
                "success": True,
                "action": "delete",
                "target": target,
                "helper_type": helper_type,
                "method": "websocket_delete",
                "entry_id": None,
                "entity_ids": [entity_id] if entity_id else [],
                "require_restart": False,
                "unique_id": unique_id,
                "message": (
                    f"Successfully deleted {helper_type}: {target} "
                    f"(entity: {entity_id})."
                ),
            }
            if wait_bool and entity_id:
                await verify_entity_removed(client, entity_id, response)
            return response

        # Core's storage collection holds only UI-created items; a YAML helper
        # has a registry entry with a unique_id but no stored item to delete.
        if ws_failure_code(result) is ErrorCode.RESOURCE_NOT_FOUND:
            code, message, suggestion = not_stored_reply(helper_type, unique_id, target)
            raise_tool_error(
                create_error_response(
                    code,
                    message,
                    context={
                        "target": target,
                        "entity_id": entity_id,
                        "unique_id": unique_id,
                    },
                    suggestions=[suggestion],
                )
            )

        error_msg = result.get("error", "Unknown error")
        if isinstance(error_msg, dict):
            error_msg = error_msg.get("message", str(error_msg))
        raise_tool_error(
            create_error_response(
                ws_failure_code(result),
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
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


def register_integration_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register integration management tools with the MCP server."""
    register_tool_methods(mcp, IntegrationTools(client))
