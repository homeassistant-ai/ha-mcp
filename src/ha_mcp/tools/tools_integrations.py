"""
Integration management tools for Home Assistant MCP server.

This module provides tools to list, enable, disable, and delete Home Assistant
integrations (config entries) via the REST and WebSocket APIs.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal, NoReturn, get_args

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .component_registry_lookup import resolve_entities_via_component
from .config_entry_flow import (
    FLOW_HELPER_TYPES,
    create_config_entry,
    iter_schema_fields,
    update_config_entry_options,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .tools_config_helpers import (
    SIMPLE_HELPER_TYPES,
    _get_entities_for_config_entry,
)
from .util_helpers import (
    JSON_STRING_COERCION,
    build_pagination_metadata,
    fetch_integration_diagnostics,
    get_logger_levels,
    parse_diagnostics_fields,
    wait_for_entity_removed,
    websocket_error_message,
)

logger = logging.getLogger(__name__)

# The ``ha_mcp_tools`` component command that serves config entries (identity +
# already-materialized ``options`` + ``subentries``) from HA's live registry in
# one in-process frame, replacing the REST list-all + OptionsFlow start/abort
# dance + subentries WS call. Module-local constant per the component-routing
# idiom (see ``component_devices.WS_DEVICE_GET``).
WS_CONFIG_ENTRIES = "ha_mcp_tools/config_entries"


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


def options_from_form_flow(flow: dict[str, Any]) -> dict[str, Any]:
    """Extract ``{field_name: current_value}`` from a form-type OptionsFlow.

    Reads each ``data_schema`` entry's ``description.suggested_value``
    first: HA's ``add_suggested_values_to_schema`` injects the entry's
    *persisted* option there (voluptuous renders ``suggested_value=...``
    into the ``description`` sub-object, not as a top-level field key),
    and it is what the HA UI renders as the current value. Falls back to
    ``default`` (the static schema default a brand-new form would show)
    and then ``value`` (constant-type fields ship ``value`` instead of
    ``default``). A field can carry both ``suggested_value`` and
    ``default`` at once — e.g. a group helper's ``hide_members`` stored as
    ``True`` over a schema default of ``False`` — and the stored value
    must win (issue #1575). Nested section fields are flattened into the
    returned top-level map. Fields with a missing or ``None`` value are skipped.
    """
    out: dict[str, Any] = {}
    # Defensive: HA should always return a list of dict fields, but guard
    # against malformed shapes so a bad response degrades to {} instead of
    # raising AttributeError (e.g. a string data_schema would iterate chars).
    data_schema = flow.get("data_schema")
    if not isinstance(data_schema, list):
        return out
    for field in iter_schema_fields(data_schema):
        name = field.get("name")
        if name is None:
            continue
        value = None
        description = field.get("description")
        if isinstance(description, dict):
            value = description.get("suggested_value")
        if value is None:
            value = field.get("default", field.get("value"))
        if value is not None:
            out[name] = value
    return out


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

    The flag lets callers surface degraded reads instead of passing ``{}``
    off as real options: ``smart_search`` flips ``partial`` when a probe
    fails mid-search, and ``ha_get_integration`` attaches a ``warnings``
    entry on its single-entry and list responses. The abort in ``finally``
    is cleanup; a failed abort does not flip ``ok`` (the options were
    already harvested).

    Home Assistant does not expose ``ConfigEntry.options`` through any
    read-only REST or WebSocket endpoint — ``/api/config/config_entries/entry``
    deliberately omits the field. The closest approximation that the HA UI
    itself uses is the OptionsFlow's first-step ``data_schema``: HA injects
    the persisted options into each field's ``description.suggested_value``
    (via ``add_suggested_values_to_schema``), which
    :func:`options_from_form_flow` prefers over the static schema
    ``default`` (issue #1575).

    Probe failures log at ``warning`` (so breakage of a deliberate
    single-entry probe is discoverable) unless ``quiet=True``, which demotes
    them to ``debug`` for bulk fan-out callers (e.g. ``smart_search`` probes
    one entry per flow-helper on every ``ha_search``; a per-entry
    warning there would spam the log on routine searches).

    Exposed at module level (not as a method) so non-class callers such as
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
    except Exception as exc:
        log_probe_failure(
            f"Failed to fetch options for {entry_id}: {type(exc).__name__}: {exc}"
        )
        return {}, False
    finally:
        if flow_id:
            try:
                await client.abort_options_flow(flow_id)
            except Exception as abort_err:
                log_probe_failure(
                    f"Failed to abort options flow {flow_id}: "
                    f"{type(abort_err).__name__}: {abort_err}"
                )


async def _fetch_entries_via_component(
    client: Any, *, entry_id: str | None = None, domain: str | None = None
) -> list[dict[str, Any]] | None:
    """One ``ha_mcp_tools/config_entries`` read; ``None`` ⇒ run the legacy path.

    Returns the component's ``entries`` list — each row in the
    ``config_entries/get`` shape (identity + status fields plus the entry's
    already-materialized ``options`` [raw persisted, secret-scrubbed] and its
    ``subentries`` identity rows) — so a single in-process frame replaces the
    legacy REST list-all + OptionsFlow start/abort probe + subentries WS call.
    Pass ``entry_id`` for the single entry (empty list ⇒ no such entry) or
    ``domain`` to filter the list; neither lists all.

    Consumers: ``ha_get_integration`` (single-entry + list) and radio's
    ``resolve_entry_id`` (single-instance domain → entry_id) both import this so
    the domain/entry-scoped read routes through one place.

    ``None`` on capability miss, downgrade (``unknown_command`` → invalidate the
    cached caps), or command error/timeout (logged) — the caller falls back to
    its legacy path.

    Per the uniform transport-fallback taxonomy, a connection-establishment
    failure IS caught here and mapped to ``None`` (legacy fallback), like every
    component fetch helper. The callers' legacy paths are NOT the shared pooled WS —
    ``ha_get_integration`` reads pure REST (``get_config_entry`` /
    ``GET /config/config_entries`` + the REST OptionsFlow probe) and radio's
    ``resolve_entry_id`` uses the REST client's ``send_websocket_message``
    bridge — so a WS outage must not kill the tool when the legacy path can
    still serve the entry. (The bridge itself raises on a dead transport since
    #1947, which only matters for the radio caller: when the socket is dead
    that path has nothing to serve either, while the pure-REST caller is
    unaffected.) The catch stays broad so no unexpected fault escapes and
    kills the tool; routing any
    non-command component failure back to the legacy fetch is safe here (mirrors
    ``get_component_caps``' own broad-catch precedent). Otherwise the same caps-gate
    discipline as ``component_devices.fetch_device_via_component``.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "config_entries"):
        return None
    kwargs: dict[str, Any] = {}
    if entry_id is not None:
        kwargs["entry_id"] = entry_id
    if domain is not None:
        kwargs["domain"] = domain
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_CONFIG_ENTRIES, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_CONFIG_ENTRIES, exc)
        return None
    except Exception as exc:
        # DEVIATION (see docstring): the legacy path is pure REST / the REST-client
        # WS bridge, NOT the shared pooled WS. A pooled-WS drop
        # (HomeAssistantConnectionError) OR get_websocket_client() raising a plain
        # Exception when WebSocketManager can't (re)connect must fall back to legacy
        # rather than kill the tool.
        logger.warning(
            "%s connection error; falling back to legacy: %r",
            WS_CONFIG_ENTRIES,
            exc,
        )
        return None
    result = raw.get("result")
    entries = result.get("entries") if isinstance(result, dict) else None
    if not isinstance(entries, list):
        logger.debug(
            "%s returned a malformed result (no 'entries' list); falling back to legacy",
            WS_CONFIG_ENTRIES,
        )
        return None
    return entries


def _split_component_entry_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split a component ``config_entries`` row into ``(entry, subentries)``.

    The returned ``entry`` mirrors the legacy REST per-entry shape: the row's
    identity + status fields and its already-materialized ``options`` (raw
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


def _flatten_option_sections(options: dict[str, Any]) -> dict[str, Any]:
    """Additively surface one level of nested option *sections* at the top level.

    HA's OptionsFlow groups related fields under a *section* key, so a template
    helper persists e.g. ``{"advanced_options": {"availability": "..."}}``. The
    legacy OptionsFlow-derived read flattens those sections — exposing
    ``options["availability"]`` directly — whereas the component serves the RAW
    persisted mapping with the section nesting intact. To keep the two read paths
    interchangeable for consumers, copy each nested section's leaf keys up to the
    top level WITHOUT overwriting an existing top-level key (first section wins on
    a cross-section collision) and WITHOUT removing the nested original (raw
    nesting preserved for fidelity). Returns a NEW dict; the input is not mutated.
    A non-dict is returned unchanged.
    """
    if not isinstance(options, dict):
        return options
    flattened: dict[str, Any] = dict(options)
    for value in options.values():
        if isinstance(value, dict):
            for leaf_key, leaf_value in value.items():
                if leaf_key not in flattened:
                    flattened[leaf_key] = leaf_value
    return flattened


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
            "openWorldHint": False,
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
            bool,
            Field(
                description="Include the options object for each entry. "
                "Automatically enabled when domain filter is set. "
                "For UI-created flow-based helpers (template, group, "
                "utility_meter, derivative, ...), the current config — "
                "template body, group members, source entity, etc. — is "
                "surfaced here by probing the options flow. Prefer this over "
                "include_schema when you only need to read the current values; "
                "use include_schema when you also need the field types or "
                "selector metadata.",
                default=False,
            ),
        ] = False,
        include_schema: Annotated[
            bool,
            Field(
                description="When entry_id is set, also return the options flow schema "
                "(available fields and their types). Use before ha_config_set_helper "
                "to understand what can be updated. Only applies when supports_options=true.",
                default=False,
            ),
        ] = False,
        include_subentries: Annotated[
            bool,
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
            bool,
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
                    "Use exact substring matching for query filter (default: True). "
                    "Set to False for fuzzy matching when the query may contain typos."
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
                description="Max entries to return per page in list mode (default: 50)",
            ),
        ] = 50,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of entries to skip for pagination (default: 0)",
            ),
        ] = 0,
        include_diagnostics: Annotated[
            bool,
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
        include_knx_project: Annotated[
            bool,
            Field(
                description=(
                    "When entry_id is a KNX config entry, also return the parsed "
                    "ETS project: the full group-address table (address, name, "
                    "DPT, description) under knx_project.group_addresses, plus the "
                    "group-range hierarchy and project metadata. This is the "
                    "parsed-project GA table that is NOT in the diagnostics dump; "
                    "per-entity GA assignments are already covered by "
                    "include_diagnostics (config_store / configuration_yaml). "
                    "Ignored (with a warning) when the entry is not a KNX "
                    "integration. The KNX integration exposes a single project, "
                    "so the result is the same regardless of which KNX entry_id "
                    "is used."
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
            JSON_STRING_COERCION,
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
            int | None,
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
                ge=1,
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
            int | None,
            Field(
                description=(
                    "Pagination start index (default 0) for list-valued "
                    "diagnostics_data_path results. Ignored when "
                    "diagnostics_data_path is unset, diagnostics_data_limit is "
                    "unset, or the resolved value is not a list. Only applies "
                    "when include_diagnostics=True."
                ),
                default=0,
                ge=0,
            ),
        ] = 0,
        diagnostics_data_limit: Annotated[
            int | None,
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
                ge=1,
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
        - Get the parsed KNX ETS project (group-address table): ha_get_integration(entry_id="<knx entry>", include_knx_project=True)
        - Walk a sub-tree: ha_get_integration(entry_id="abc123", include_diagnostics=True, diagnostics_data_path="<dotted-path>")
        - Paginate a large list: ha_get_integration(entry_id="abc123", include_diagnostics=True, diagnostics_data_path="<list-valued path>", diagnostics_data_limit=10, diagnostics_data_offset=20)
        - List config subentries: ha_get_integration(entry_id="abc123", include_subentries=True)
        - Inspect subentry create schema: ha_get_integration(entry_id="abc123", include_subentry_schema=True, subentry_type="conversation")
        - Inspect subentry reconfigure schema: ha_get_integration(entry_id="abc123", include_subentry_schema=True, subentry_type="conversation", subentry_id="sub123")
        - List template entries: ha_get_integration(domain="template")

        STATES: 'loaded', 'setup_error', 'setup_retry', 'not_loaded',
        'failed_unload', 'migration_error'.

        OPTIONS: ``options`` reflect the entry's persisted values; a field that
        was never set may be absent (rather than shown at its schema default).
        Values that match a ``secrets.yaml`` entry are returned as
        ``"**redacted**"``. Use ``include_schema=True`` to see every editable
        field and its default/type. Nested option *sections* (e.g. a template
        helper's ``advanced_options``) are additively flattened one level —
        each section's leaf keys are copied to the top of ``options`` (mirroring
        the OptionsFlow-derived read) while the raw nested section is preserved
        for fidelity, and an existing top-level key is never overwritten.

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
            include_opts = include_options
            include_schema_bool = include_schema
            include_diagnostics_bool = include_diagnostics
            include_knx_project_bool = include_knx_project
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
            # Auto-enable options when domain filter is set
            if domain is not None:
                include_opts = True

            # If entry_id provided, get specific config entry
            if entry_id is not None:
                return await self._get_entry_detail_response(
                    entry_id,
                    include_schema=include_schema_bool,
                    include_subentries=include_subentries_bool,
                    include_subentry_schema=include_subentry_schema_bool,
                    subentry_type=subentry_type,
                    subentry_id=subentry_id,
                    show_advanced_options=show_advanced_options_bool,
                    include_diagnostics=include_diagnostics_bool,
                    include_knx_project=include_knx_project_bool,
                    device_id=device_id,
                    fields_list=fields_list,
                    truncate_bytes=truncate_bytes,
                    diagnostics_data_path=diagnostics_data_path,
                    data_offset_int=data_offset_int,
                    data_limit_int=data_limit_int,
                )

            # List mode - get all config entries
            result = await self._list_entries(
                domain, query, include_opts, exact_match_bool, limit_int, offset_int
            )
            ignored_detail_params = self._ignored_diagnostics_detail_params(
                include_diagnostics=include_diagnostics_bool,
                include_knx_project=include_knx_project_bool,
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
        include_knx_project: bool,
        device_id: str | None,
        fields_list: list[str] | None,
        truncate_bytes: int | None,
        diagnostics_data_path: str | None,
        data_offset_int: int,
        data_limit_int: int | None,
    ) -> dict[str, Any]:
        """Build the single-entry response, attaching diagnostics/KNX when asked."""
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
        if include_knx_project:
            await self._attach_knx_project(resp, entry_id)
        return resp

    @staticmethod
    def _ignored_diagnostics_detail_params(
        *,
        include_diagnostics: bool,
        include_knx_project: bool,
        device_id: str | None,
        fields_list: list[str] | None,
        truncate_bytes: int | None,
        diagnostics_data_path: str | None,
        data_offset_int: int,
        data_limit_int: int | None,
    ) -> list[str]:
        """Detail-only diagnostics/KNX params that are ignored in list mode."""
        ignored_detail_params: list[str] = []
        if include_diagnostics:
            ignored_detail_params.append("include_diagnostics")
        if include_knx_project:
            ignored_detail_params.append("include_knx_project")
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
            rows = await _fetch_entries_via_component(self._client, entry_id=entry_id)
            if rows is not None:
                return await self._single_entry_from_component(
                    entry_id,
                    rows,
                    include_schema,
                    include_subentries=include_subentries,
                    include_subentry_schema=include_subentry_schema,
                    subentry_type=subentry_type,
                    subentry_id=subentry_id,
                    show_advanced_options=show_advanced_options,
                )

            result = await self._client.get_config_entry(entry_id)
            entry_domain = result.get("domain") if isinstance(result, dict) else None

            # Surface `options` on every per-entry response (HA's REST endpoint
            # omits the field). For entries with supports_options=True we probe
            # via OptionsFlow — see `fetch_entry_options_with_status`. When
            # include_schema is also requested, `_fetch_options_schema` below
            # populates options from the same flow init so we don't pay for
            # two round-trips.
            probe_warnings: list[str] = []
            await self._probe_legacy_entry_options(
                result, entry_id, include_schema, probe_warnings
            )

            resp: dict[str, Any] = {
                "success": True,
                "entry_id": entry_id,
                "entry": result,
            }
            if probe_warnings:
                resp["warnings"] = probe_warnings

            # Surface the effective Python logger level for this integration
            # so users can confirm logger.set_level changes took effect.
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
            return None  # unreachable: exception_to_structured_error raises

    async def _probe_legacy_entry_options(
        self,
        result: Any,
        entry_id: str,
        include_schema: bool | None,
        probe_warnings: list[str],
    ) -> None:
        """Fill a legacy REST entry's ``options`` via OptionsFlow, noting misses.

        Mutates ``result['options']`` in place and appends to ``probe_warnings``
        when the probe fails. No-op for a non-dict ``result`` or when
        ``include_schema`` is set (the schema path populates options instead).
        """
        if isinstance(result, dict):
            result.setdefault("options", {})
            if result.get("supports_options") and not include_schema:
                options, probe_ok = await fetch_entry_options_with_status(
                    self._client, entry_id
                )
                result["options"] = options
                if not probe_ok:
                    probe_warnings.append(
                        f"options probe failed for {entry_id}: the "
                        "OptionsFlow could not be read, so 'options' may "
                        "be incomplete — empty options does not mean the "
                        "entry has none"
                    )

    async def _single_entry_from_component(
        self,
        entry_id: str,
        rows: list[dict[str, Any]],
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
        this one read replaces the legacy REST list-all + OptionsFlow
        start/abort probe + subentries WS call. The options schema (and the
        subentry schema) still come from the legacy live flow: a schema only
        exists inside an open flow, which the component cannot serialize. When a
        schema is requested the component's ``options`` are kept
        (``populate_options=False``) rather than overwritten by the
        OptionsFlow-derived suggested-value shape.

        ``log_level`` / ``log_level_raw`` come from ``get_logger_levels`` on both
        paths (the component does not carry logger overrides).
        """
        if not rows:
            # ``async_get_entry(entry_id)`` found nothing — an authoritative
            # not-found, mapped to the same 404 the legacy REST get raises.
            raise HomeAssistantAPIError(
                f"Config entry not found: {entry_id}", status_code=404
            )
        entry, subentries = _split_component_entry_row(rows[0])
        entry.setdefault("options", {})
        # Mirror the OptionsFlow-derived read: additively flatten one level of
        # nested option sections (raw nesting preserved). See
        # `_flatten_option_sections`.
        entry["options"] = _flatten_option_sections(entry["options"])

        resp: dict[str, Any] = {
            "success": True,
            "entry_id": entry_id,
            "entry": entry,
        }

        # Surface the effective Python logger level for this integration
        # (unconditionally, for symmetry with the legacy path and _format_entry).
        level_warnings: list[str] = []
        logger_levels = await get_logger_levels(self._client, level_warnings)
        level_info = logger_levels.get(entry.get("domain") or "")
        # UNKNOWN, not DEFAULT — see the component path above.
        resp["log_level"] = (
            "UNKNOWN"
            if level_warnings
            else (level_info["name"] if level_info else "DEFAULT")
        )
        resp["log_level_raw"] = level_info["raw"] if level_info else None
        if level_warnings:
            resp.setdefault("warnings", []).extend(level_warnings)

        # Options schema only exists in a live options flow — read it from the
        # legacy flow, but keep the component-provided options (populate_options
        # False) so the raw persisted values win over the flow-derived shape.
        if include_schema and entry.get("supports_options"):
            await self._fetch_options_schema(entry_id, resp, populate_options=False)

        if include_subentries:
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

    async def _attach_knx_project(self, resp: dict[str, Any], entry_id: str) -> None:
        """Attach the parsed KNX ETS project to a single-entry response.

        Reads ``knx/get_knx_project`` (the same command the KNX panel uses) and
        attaches the group-address table, group-range hierarchy, and project
        metadata under ``resp["knx_project"]``. The KNX integration exposes one
        project globally, so the command takes no entry scope; this is gated on
        the entry actually being a KNX integration to avoid attaching unrelated
        data to a non-KNX entry.

        Failures are surfaced as warnings rather than raised — the primary
        entry read already succeeded, mirroring how diagnostics fetch failures
        are handled. The "KNX integration not loaded" case collapses into
        ha_get_integration's existing no-such-entry handling (no KNX entry → no
        entry_id to pass here), so it needs no special casing.
        """
        entry = resp.get("entry") if isinstance(resp.get("entry"), dict) else {}
        domain = entry.get("domain") if isinstance(entry, dict) else None
        if domain != "knx":
            resp.setdefault("warnings", []).append(
                f"include_knx_project ignored: entry {entry_id} is not a KNX "
                f"integration (domain={domain!r})"
            )
            return

        result = await self._client.send_websocket_message(
            {"type": "knx/get_knx_project"}
        )
        if not isinstance(result, dict) or not result.get("success"):
            error_msg = websocket_error_message(
                result.get("error", "Unknown error")
                if isinstance(result, dict)
                else result
            )
            resp.setdefault("warnings", []).append(
                f"Failed to fetch KNX project: {error_msg}"
            )
            return

        project = result.get("result")
        if not project:
            # No ETS project uploaded yet — get_knxproject() returns None.
            resp["knx_project"] = {
                "count": 0,
                "group_addresses": {},
                "group_ranges": {},
                "info": {},
                "note": (
                    "The KNX integration is loaded but no ETS project has been "
                    "uploaded yet. Upload a .knxproj file via the KNX panel to "
                    "populate the group-address table."
                ),
            }
            return

        group_addresses = project.get("group_addresses", {})
        resp["knx_project"] = {
            "count": len(group_addresses),
            "group_addresses": group_addresses,
            "group_ranges": project.get("group_ranges", {}),
            "info": project.get("info", {}),
        }

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
        """Class-method alias for :func:`options_from_form_flow`."""
        return options_from_form_flow(flow)

    async def _fetch_options_schema(
        self, entry_id: str, resp: dict[str, Any], *, populate_options: bool = True
    ) -> None:
        """Start an options flow to read the schema, then abort it.

        Also populates ``resp["entry"]["options"]`` for form-type flows from
        the same flow result so callers requesting both schema and options
        don't pay for two round-trips. Pass ``populate_options=False`` on the
        component-served path, where ``options`` already carry the raw persisted
        values and must NOT be overwritten by the OptionsFlow-derived
        suggested-value shape.
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
                if entry is not None and populate_options:
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
            schema_warnings: list[str] = resp.setdefault("warnings", [])
            schema_warnings.append(
                f"options schema probe failed for {entry_id}: "
                f"{type(schema_err).__name__} — 'options_schema' is missing "
                "and 'options' may be incomplete"
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
        # Component fast path: one in-process read (domain filtered server-side,
        # options materialized on each row) replaces the REST list + per-entry
        # OptionsFlow probes. Normalize the domain to HA's canonical lowercase so
        # the component's exact-match filter mirrors the legacy client-side one.
        domain_norm = domain.strip().lower() if domain else None
        rows = await _fetch_entries_via_component(self._client, domain=domain_norm)
        if rows is not None:
            return await self._list_entries_from_component(
                rows, domain, query, include_opts, exact_match, limit_int, offset_int
            )

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
        # advertise supports_options=True. See `fetch_entry_options_with_status`.
        formatted_entries = [
            self._format_entry(entry, include_opts, logger_levels) for entry in entries
        ]

        # quiet=True: per-entry probe failures are aggregated into a response
        # warning below instead of one log line each (bulk fan-out).
        probe_failures: list[str] = []
        if include_opts:
            options_targets = [
                e for e in formatted_entries if e.get("supports_options")
            ]
            if options_targets:
                fetched = await asyncio.gather(
                    *(
                        fetch_entry_options_with_status(
                            self._client, e["entry_id"], quiet=True
                        )
                        for e in options_targets
                    ),
                    return_exceptions=False,
                )
                for entry, (opts, probe_ok) in zip(
                    options_targets, fetched, strict=True
                ):
                    entry["options"] = opts
                    if not probe_ok:
                        probe_failures.append(entry["entry_id"])

        return self._finalize_entry_list(
            formatted_entries,
            domain,
            query,
            exact_match,
            limit_int,
            offset_int,
            probe_failures,
        )

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
        materialized each entry's ``options`` on the row, so there is no
        per-entry OptionsFlow probe and thus no probe-failure warnings.
        ``options`` are raw persisted values (a field never set may be absent),
        may contain ``"**redacted**"`` markers, and have nested option sections
        additively flattened one level (raw nesting preserved) — see
        ``ha_get_integration``'s OPTIONS note and ``_flatten_option_sections``.
        """
        logger_levels = await get_logger_levels(self._client)
        formatted_entries = [
            self._format_entry(row, include_opts, logger_levels) for row in rows
        ]
        # Mirror the OptionsFlow-derived read: additively flatten one level of
        # nested option sections on each row (raw nesting preserved). Only the
        # include_opts path carries an ``options`` key. See
        # `_flatten_option_sections`.
        if include_opts:
            for formatted in formatted_entries:
                formatted["options"] = _flatten_option_sections(
                    formatted.get("options", {})
                )
        return self._finalize_entry_list(
            formatted_entries, domain, query, exact_match, limit_int, offset_int, []
        )

    def _finalize_entry_list(
        self,
        formatted_entries: list[dict[str, Any]],
        domain: str | None,
        query: str | None,
        exact_match: bool | None,
        limit_int: int,
        offset_int: int,
        probe_failures: list[str],
    ) -> dict[str, Any]:
        """Query-filter, summarize, and paginate formatted entries.

        Shared tail of the component-served and legacy list paths so their
        response shapes stay identical. ``probe_failures`` is always empty on
        the component path (options ride the same read — no per-entry probe).
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
            result_data["domain_filter"] = domain.strip().lower()
        if probe_failures:
            result_data["warnings"] = [
                f"options probe failed for {len(probe_failures)} "
                f"entr{'y' if len(probe_failures) == 1 else 'ies'} "
                f"({', '.join(probe_failures)}) — their 'options' may be "
                "incomplete; empty options does not mean an entry has none"
            ]
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
        name="ha_set_integration",
        tags={"Integrations"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Set Integration",
        },
    )
    @with_auto_backup(domain="integration", id_param="entry_id")
    @log_tool_usage
    async def ha_set_integration(
        self,
        entry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Config entry ID of an existing integration (enable/disable "
                    "and options-update modes). Omit when adding via 'domain'."
                ),
                default=None,
            ),
        ] = None,
        enabled: Annotated[
            bool | None,
            Field(
                description=(
                    "True to enable, False to disable the entry. Requires "
                    "entry_id; mutually exclusive with 'domain' and 'config'."
                ),
                default=None,
            ),
        ] = None,
        domain: Annotated[
            str | None,
            Field(
                description=(
                    "Integration domain to add (e.g. 'workday', "
                    "'local_calendar') — starts and drives that domain's "
                    "config flow. Pass the flow's form fields in 'config'."
                ),
                default=None,
            ),
        ] = None,
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Flow form data. With 'domain': input for the new "
                    "integration's config flow. With 'entry_id' alone: input "
                    "for the entry's options flow (updates its options). "
                    "Multi-step flows consume keys per step; menu steps take "
                    "'next_step_id'. The step's data_schema is returned on "
                    "validation errors so field names can be corrected."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage an integration (config entry): enable/disable, add, or update options.

        Modes (pick one):
        - Enable/disable: entry_id + enabled.
        - Add integration: domain (+ config) — drives the domain's config
          flow, including menus and multi-step forms.
        - Update options: entry_id + config — drives the entry's options
          flow (what the "Configure" button does in the HA UI).

        WHEN NOT TO USE:
        - Helpers (template, group, utility_meter, ...): use
          ha_config_set_helper.
        - Config subentries: use
          ha_config_set_helper(helper_type='config_subentry').
        - Removing an entry: use ha_remove_helpers_integrations.

        Use ha_get_integration() to find entry IDs, and
        ha_get_integration(entry_id=..., include_schema=True) to inspect the
        options fields before an update.

        Caveats: adding an integration runs its config flow exactly as the HA
        UI would (may pair devices, scan the network, create entities). Flows
        requiring a browser step (OAuth) or an asynchronous provider step
        error out at that step with a structured error instead of completing.

        EXAMPLES:
        - Disable: ha_set_integration(entry_id="abc123", enabled=False)
        - Add: ha_set_integration(domain="workday", config={"name": "Workday"})
        - Update options: ha_set_integration(entry_id="abc123", config={"scan_interval": 30})
        """
        try:
            if domain is not None and entry_id is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Pass either 'domain' (add a new integration) or "
                        "'entry_id' (modify an existing one), not both",
                        context={"entry_id": entry_id, "domain": domain},
                    )
                )
            if enabled is not None and (domain is not None or config is not None):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "'enabled' is mutually exclusive with 'domain' and "
                        "'config' — enable/disable is a separate call",
                        context={"entry_id": entry_id, "domain": domain},
                    )
                )

            if domain is not None:
                # Add mode: drive the domain's config flow.
                validate_identifier_not_empty(
                    domain,
                    "domain",
                    suggestions=[
                        "Pass the integration domain, e.g. 'workday' or "
                        "'local_calendar'",
                    ],
                )
                return await create_config_entry(self._client, domain, config or {})

            if entry_id is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Nothing to do — provide 'domain' to add an "
                        "integration, or 'entry_id' with 'enabled' "
                        "(enable/disable) or 'config' (update options)",
                        suggestions=[
                            "Use ha_get_integration() to find valid config entry IDs",
                        ],
                    )
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
                return await self._set_entry_enabled(entry_id, enabled)

            if config is not None:
                # Options mode: drive the entry's options flow.
                return await update_config_entry_options(self._client, entry_id, config)

            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Nothing to do — pass 'enabled' to enable/disable the "
                    "entry, or 'config' to update its options",
                    context={"entry_id": entry_id},
                )
            )
            return None  # unreachable: raise_tool_error raises

        except ToolError:
            raise
        except Exception as e:
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

    async def _set_entry_enabled(self, entry_id: str, enabled: bool) -> dict[str, Any]:
        """Enable or disable a config entry via ``config_entries/disable``."""
        message = {
            "type": "config_entries/disable",
            "entry_id": entry_id,
            "disabled_by": None if enabled else "user",
        }

        result = await self._client.send_websocket_message(message)

        if not result.get("success"):
            error_msg = result.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to {'enable' if enabled else 'disable'} integration: {error_msg}",
                    context={"entry_id": entry_id},
                )
            )

        require_restart = (result.get("result") or {}).get("require_restart", False)

        if require_restart:
            note = "Home Assistant restart required for changes to take effect."
        else:
            note = (
                "Integration has been loaded."
                if enabled
                else "Integration has been unloaded."
            )

        return {
            "success": True,
            "message": f"Integration {'enabled' if enabled else 'disabled'} successfully",
            "entry_id": entry_id,
            "require_restart": require_restart,
            "note": note,
        }

    @tool(
        name="ha_remove_helpers_integrations",
        tags={"Helper Entities", "Integrations"},
        annotations={
            "openWorldHint": False,
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
                    "Ignored when helper_type=None or "
                    "helper_type='config_subentry' (no entity poll, "
                    "require_restart returned)."
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
        Use ha_search() / ha_get_integration() to verify before
        removal. Cannot be undone.
        """
        # === Confirm gate (uniform for all four paths) ===
        if not confirm:
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
                "For simple helpers, use ha_search() to find the helper_id",
                "For flow helpers, use ha_search() to find an entity_id",
            ],
            context={"helper_type": helper_type},
        )

        wait_bool = wait
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
            entry_id, reason = await _get_entry_id_for_flow_helper(
                client, helper_type, target, warnings
            )
            if entry_id is None:
                self._raise_flow_helper_lookup_error(reason, helper_type, target)

            # Step 2: collect sub-entity IDs for the wait phase
            sub_entities = await _get_entities_for_config_entry(
                client, entry_id, warnings
            )
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
                    "Verify the target exists using ha_search() "
                    + "or ha_get_integration()",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises

    def _raise_flow_helper_lookup_error(
        self,
        reason: FlowLookupReason,
        helper_type: HelperTypeLiteral,
        target: str,
    ) -> NoReturn:
        """Raise the structured error for a failed flow-helper entry_id lookup.

        ``reason`` discriminates the failure mode without a second WebSocket
        round-trip. The lookup helper already queried the registry; the response
        told us everything we need.
        """
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
                        "Edit the YAML file and reload the relevant integration.",
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
                        "typo. Verify with ha_search() "
                        "before retrying."
                    ),
                    context={
                        "target": target,
                        "helper_type": helper_type,
                        "entity_id": entity_id,
                    },
                    suggestions=[
                        "Use ha_search() — flow helper "
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
                    "ha_search() — flow helper types often "
                    "expose entities under a different domain than "
                    "the helper_type itself (e.g. utility_meter → "
                    "sensor.*, switch_as_x → switch.* / light.*).",
                ],
            )
        )

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
        except Exception as e:
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

        Uses a 3-retry registry lookup with exponential backoff to find the
        helper's unique_id, then falls back to direct-id-delete and a
        confirmed-absent classification if the registry has no record.
        """
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
            (
                unique_id,
                registry_result,
                component_used,
            ) = await self._resolve_helper_unique_id(entity_id)

            if not unique_id:
                return await self._delete_simple_helper_fallback(
                    helper_type,
                    helper_id,
                    target,
                    entity_id,
                    wait_bool,
                    registry_result,
                    component_used,
                )

            return await self._delete_simple_via_unique_id(
                helper_type, unique_id, target, entity_id, wait_bool
            )

        except ToolError:
            raise
        except Exception as e:
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
        equally subject to that lag, but on this delete path a stale/missing
        resolve degrades to the direct-id fallback rather than a wrong delete.
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
        return unique_id, registry_result

    async def _delete_simple_helper_fallback(
        self,
        helper_type: HelperTypeLiteral,
        helper_id: str,
        target: str,
        entity_id: str,
        wait_bool: bool,
        registry_result: dict[str, Any] | None,
        component_used: bool,
    ) -> dict[str, Any]:
        """Handle SIMPLE-helper deletion when the registry yielded no unique_id.

        Tries a direct-id delete, then classifies the target as confirmed-absent
        (ENTITY_NOT_FOUND) or a real failure (SERVICE_CALL_FAILED). Always
        returns a success response or raises a structured error.
        """
        # Fallback strategy 1: direct-ID delete if unique_id not found
        response = await self._try_direct_id_delete(
            helper_type, helper_id, target, entity_id, wait_bool
        )
        if response is not None:
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
        if await self._state_absent(entity_id):
            if not await self._registry_still_has_entry(entity_id):
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
                        "registry entry exists but unique_id was "
                        "absent and the direct-id fallback "
                        "delete failed."
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

        # All fallbacks exhausted
        err_detail = (
            registry_result.get("error", "Unknown error")
            if registry_result
            else "No registry response"
        )
        max_retries = 3
        # The component path resolves via ONE authoritative in-process
        # lookup (no retry loop), so the detail text must not claim
        # "3 attempts" there. The legacy branch's wording is unchanged.
        if component_used:
            not_found_detail = (
                f"Component registry lookup found no unique_id for "
                f"{entity_id}: {err_detail}"
            )
        else:
            not_found_detail = (
                f"Helper not found in entity registry after "
                f"{max_retries} attempts: {err_detail}"
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

    async def _try_direct_id_delete(
        self,
        helper_type: HelperTypeLiteral,
        helper_id: str,
        target: str,
        entity_id: str,
        wait_bool: bool,
    ) -> dict[str, Any] | None:
        """Fallback: delete a SIMPLE helper by bare id when no unique_id resolved.

        Returns the success response, or None when the direct-id delete did not
        succeed (caller proceeds to confirmed-absent classification).
        """
        client = self._client
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

        if not result.get("success"):
            return None

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

    async def _state_absent(self, entity_id: str) -> bool:
        """Return True when ``entity_id`` is absent from the state machine.

        A non-404 APIError (transient/auth) is re-raised so it is not
        mis-classified as a missing target; a 404 is treated as state-absent.
        """
        client = self._client
        try:
            final_state_check = await client.get_entity_state(entity_id)
            return not final_state_check
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
            return True

    async def _registry_still_has_entry(self, entity_id: str) -> bool:
        """Return True if ``entity_id`` still has an entity-registry entry.

        On a verify failure, conservatively returns True so a transient error
        is not misread as confirmed-absent.
        """
        client = self._client
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
                    return True
        except HomeAssistantAPIError as verify_err:
            # On verify failure, conservatively assume the
            # entry is still there rather than misclassify
            # a verify failure as confirmed-absent.
            logger.debug(f"Registry verify for {entity_id} failed: {verify_err}")
            return True
        return False

    async def _delete_simple_via_unique_id(
        self,
        helper_type: HelperTypeLiteral,
        unique_id: str,
        target: str,
        entity_id: str,
        wait_bool: bool,
    ) -> dict[str, Any]:
        """Delete a SIMPLE helper by its resolved registry unique_id."""
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
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


def register_integration_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register integration management tools with the MCP server."""
    register_tool_methods(mcp, IntegrationTools(client))
