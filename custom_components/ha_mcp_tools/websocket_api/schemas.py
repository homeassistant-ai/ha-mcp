"""Voluptuous request schemas for the ha_mcp_tools WebSocket commands."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from ..const import CHANNEL_DEV, CHANNEL_STABLE
from .constants import (
    ALL_SEARCH_TYPES,
    BLUEPRINT_DOMAINS,
    CALL_SERVICE_DEFAULT_TIMEOUT,
    CALL_SERVICE_MAX_TIMEOUT,
    DEFAULT_LIMIT,
    REGISTRY_KINDS,
    SERVER_ENTRY_UPDATE_MAX_PIP_SPEC,
    WS_BACKUP_PREP,
    WS_BLUEPRINT_GET,
    WS_BULK_CALL_SERVICE,
    WS_CALL_SERVICE,
    WS_CONFIG_ENTRIES,
    WS_DASHBOARD_EDIT,
    WS_DASHBOARDS,
    WS_DEVICE_GET,
    WS_DEVICE_LIST,
    WS_ENTITY_ENRICH,
    WS_ENTITY_LOOKUP,
    WS_EXPOSURE,
    WS_HELPERS_LIST,
    WS_INFO,
    WS_OVERVIEW,
    WS_REFERENCE_DATA,
    WS_REGISTRIES,
    WS_REGISTRY_LOOKUP,
    WS_SEARCH,
    WS_SERVER_ENTRY,
    WS_SERVER_ENTRY_UPDATE,
    WS_SERVICES_LIST,
    WS_STATES,
    WS_SYSTEM_SNAPSHOT,
    WS_TEMPLATE_DIAGNOSE,
)


def _info_schema() -> dict[Any, Any]:
    return {vol.Required("type"): WS_INFO}


# The same bound as ha_eval_template's timeout parameter; it keeps one diagnosis
# from holding a render thread longer than a caller could usefully wait.
TEMPLATE_DIAGNOSE_MAX_TIMEOUT = 60.0


def _template_diagnose_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_TEMPLATE_DIAGNOSE,
        vol.Required("template"): str,
        vol.Optional("variables"): dict,
        vol.Optional("strict", default=False): bool,
        vol.Optional("timeout", default=3.0): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=TEMPLATE_DIAGNOSE_MAX_TIMEOUT)
        ),
    }


# The nine hide dimensions ``VisibilityConfig.to_wire`` emits, split by wire type
# (seven id/name lists, two bool flags), plus the optional
# ``allowlist_authorization`` precedence flag the server adds for a component that
# advertises ``search_visibility_allowlist_authorization``. Kept in lockstep with
# the server's ``to_wire`` / ``VisibilityWire`` and :func:`_visibility_hidden_set`;
# a new dimension is a new component capability, added on BOTH sides (see
# :func:`_visibility_param_schema`). The union is pinned equal to the server
# resolver's key set by the cross-seam contract test.
_VISIBILITY_LIST_KEYS = (
    "exclude_categories",
    "deny_entity_ids",
    "exclude_areas",
    "exclude_labels",
    "allow_entity_ids",
    "allow_areas",
    "allow_labels",
)
_VISIBILITY_BOOL_KEYS = (
    "exclude_hidden",
    "respect_assist_exposure",
    "allowlist_authorization",
)


def _visibility_param_schema() -> Any:
    """Voluptuous schema for the ``search`` ``visibility`` dict — exactly the ten keys.

    The nine hide dimensions plus the ``allowlist_authorization`` precedence flag
    (sent only when this component advertises
    ``search_visibility_allowlist_authorization``; absent means legacy
    precedence). Enumerating the known keys (PREVENT_EXTRA is voluptuous' default
    for a nested ``Schema``) makes an unknown key a loud ``invalid_format`` command
    error rather than a silent drop. If a newer server emits an eleventh key to
    this component, the server's error taxonomy converts that into a legacy
    fallback with the filter STILL correctly applied — structural fail-closed for
    free — instead of partial, unwarned filtering. Built at call time so it honors the
    monkeypatched ``vol`` in the unit suite.
    """
    schema: dict[Any, Any] = {vol.Optional(key): [str] for key in _VISIBILITY_LIST_KEYS}
    schema.update({vol.Optional(key): bool for key in _VISIBILITY_BOOL_KEYS})
    return vol.Schema(schema)


def _search_schema() -> dict[Any, Any]:
    """Build the schema for search WebSocket requests."""
    return {
        vol.Required("type"): WS_SEARCH,
        vol.Optional("query"): vol.Any(str, None),
        vol.Optional("search_types"): [vol.In(ALL_SEARCH_TYPES)],
        vol.Optional("domain_filter"): str,
        vol.Optional("area_filter"): str,
        vol.Optional("state_filter"): str,
        vol.Optional("result_fields"): [vol.In(("is_group", "member_entity_ids"))],
        vol.Optional("exact", default=True): bool,
        vol.Optional("include_hidden", default=True): bool,
        vol.Optional("include_config", default=False): bool,
        vol.Optional("limit", default=DEFAULT_LIMIT): vol.All(int, vol.Range(min=1)),
        vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
        # Opt-in entity visibility for component search. The component advertises
        # ``search_visibility`` and ``search_visibility_allowlist_authorization``;
        # the server reads those flags before sending these raw VisibilityConfig
        # dimensions, and adds ``allowlist_authorization`` only for the second flag.
        # The ten known keys are enumerated so an unknown one fails loudly and the
        # server falls back to its own filtered legacy path.
        # See ``_visibility_param_schema``.
        vol.Optional("visibility"): _visibility_param_schema(),
    }


def _overview_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_OVERVIEW,
        vol.Optional("include_notifications", default=True): bool,
        vol.Optional("include_repairs", default=True): bool,
    }


def _helpers_list_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_HELPERS_LIST,
        vol.Optional("helper_types"): [str],
        vol.Optional("include_flow_helpers", default=True): bool,
    }


def _states_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_STATES,
        vol.Required("entity_ids"): [str],
    }


def _blueprint_get_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_BLUEPRINT_GET,
        vol.Required("domain"): vol.In(BLUEPRINT_DOMAINS),
        vol.Required("path"): str,
    }


def _device_get_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_DEVICE_GET,
        vol.Required("device_id"): str,
        vol.Optional("include_entities", default=False): bool,
    }


def _device_list_schema() -> dict[Any, Any]:
    return {vol.Required("type"): WS_DEVICE_LIST}


def _entity_enrich_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_ENTITY_ENRICH,
        vol.Required("entity_ids"): [str],
    }


def _exposure_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_EXPOSURE,
        vol.Optional("entity_id"): vol.Any(str, None),
    }


def _config_entries_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_CONFIG_ENTRIES,
        vol.Optional("entry_id"): vol.Any(str, None),
        vol.Optional("domain"): vol.Any(str, None),
        vol.Optional("include_subentry_data", default=False): bool,
    }


def _registry_lookup_schema() -> dict[Any, Any]:
    # Exactly one of entity_ids / config_entry_id is meaningful; ``vol.Exclusive``
    # rejects a request carrying BOTH (the two share the ``target`` group).
    # Voluptuous has no clean way to express "at least one of" in a flat schema,
    # so a request with NEITHER present is caught in ``_do_registry_lookup``
    # instead (raises ``HomeAssistantError`` rather than a silent empty result).
    return {
        vol.Required("type"): WS_REGISTRY_LOOKUP,
        vol.Exclusive("entity_ids", "target"): [str],
        vol.Exclusive("config_entry_id", "target"): str,
    }


def _system_snapshot_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_SYSTEM_SNAPSHOT,
        vol.Optional("include_states", default=True): bool,
        vol.Optional("include_entities", default=True): bool,
        vol.Optional("include_issues", default=True): bool,
        vol.Optional("include_config_entries", default=True): bool,
    }


def _entity_lookup_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_ENTITY_LOOKUP,
        vol.Required("unique_id"): str,
        vol.Optional("domain"): vol.Any(str, None),
        vol.Optional("platform"): vol.Any(str, None),
    }


def _backup_prep_schema() -> dict[Any, Any]:
    return {vol.Required("type"): WS_BACKUP_PREP}


def _registries_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_REGISTRIES,
        vol.Required("registries"): [vol.In(REGISTRY_KINDS)],
        vol.Optional("category_scopes"): [str],
    }


def _dashboards_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_DASHBOARDS,
        vol.Optional("mode", default="list"): vol.In(("list", "get", "search")),
        # ``None``/absent url_path = the default dashboard (``get`` mode).
        vol.Optional("url_path"): vol.Any(str, None),
        vol.Optional("query"): vol.Any(str, None),
    }


def _services_list_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_SERVICES_LIST,
        vol.Optional("domain"): vol.Any(str, None),
        vol.Optional("language", default="en"): str,
    }


def _reference_data_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_REFERENCE_DATA,
        vol.Optional("include_states", default=True): bool,
    }


def _server_entry_schema() -> dict[Any, Any]:
    return {vol.Required("type"): WS_SERVER_ENTRY}


def _single_line_pip_spec(value: str) -> str:
    """Reject a multi-line / control-char ``pip_spec`` (defence-in-depth over D6)."""
    if any(ord(c) < 32 for c in value):
        raise vol.Invalid("pip_spec must be a single-line string")
    return value


def _server_entry_update_schema() -> dict[Any, Any]:
    # Both fields are optional at the schema level (voluptuous cannot cleanly express
    # "at least one of"); ``_server_entry_update_prep`` raises when NEITHER is present.
    # ``channel`` is gated to the known set and ``pip_spec`` gets the length +
    # single-line cap — schema-level defence-in-depth over (and symmetric with) the
    # server's own channel validation (D6) and pip-spec normalization.
    return {
        vol.Required("type"): WS_SERVER_ENTRY_UPDATE,
        vol.Optional("channel"): vol.In((CHANNEL_STABLE, CHANNEL_DEV)),
        vol.Optional("pip_spec"): vol.All(
            str,
            vol.Length(max=SERVER_ENTRY_UPDATE_MAX_PIP_SPEC),
            _single_line_pip_spec,
        ),
    }


def _call_service_schema() -> dict[Any, Any]:
    # ``entity_ids`` is the set of targets to CONFIRM (the pre/post transition is
    # built for these), not the service target itself — a caller may pass an empty
    # list for a non-entity service (``automation.trigger`` etc.) and still get the
    # dispatch result. ``timeout`` is capped at ``CALL_SERVICE_MAX_TIMEOUT`` so a
    # single write frame cannot park the connection. Mutable defaults use the
    # callable form so each validation produces a fresh ``{}`` / ``[]``.
    return {
        vol.Required("type"): WS_CALL_SERVICE,
        vol.Required("domain"): str,
        vol.Required("service"): str,
        vol.Optional("service_data", default=dict): dict,
        vol.Optional("entity_ids", default=list): [str],
        vol.Optional("wait", default=True): bool,
        vol.Optional("timeout", default=CALL_SERVICE_DEFAULT_TIMEOUT): vol.All(
            vol.Any(int, float), vol.Range(min=0, max=CALL_SERVICE_MAX_TIMEOUT)
        ),
        vol.Optional("return_response", default=False): bool,
        # Optional confirmation HINT (the server's ``_SERVICE_TO_STATE.get(service)``,
        # or None): the expected primary state the waiter confirms on REACHING —
        # skipping a multi-phase service's intermediate states and attribute-only
        # noise — and immediate-matches for an idempotent no-op. Optional/default-None
        # so a server that does not send it (or a non-mapped service) keeps today's
        # any-first-event confirmation. It governs confirmation TIMING only; the
        # returned transition is always the REAL observed one.
        vol.Optional("expected_state"): vol.Any(str, None),
    }


def _bulk_call_service_schema() -> dict[Any, Any]:
    # A batch of fully-resolved operations, each the single ``call_service`` row
    # minus its own ``wait`` / ``timeout`` / ``return_response`` (those are batch
    # scoped: one ``wait`` flag, one shared ``timeout`` deadline, and no per-op
    # ``return_response`` — bulk stays simple, the single call covers that need).
    # ``operations`` must be non-empty (an empty batch is a caller error, not a
    # no-op). ``timeout`` is capped at ``CALL_SERVICE_MAX_TIMEOUT`` so a single
    # batch frame cannot park the connection past that bound. Mutable per-op
    # defaults use the callable form so each validation yields a fresh ``{}`` /
    # ``[]``.
    operation = {
        vol.Required("domain"): str,
        vol.Required("service"): str,
        vol.Optional("service_data", default=dict): dict,
        vol.Optional("entity_ids", default=list): [str],
        # Per-op confirmation HINT (see ``_call_service_schema``): optional/default-
        # None so an older server (or a non-mapped service) keeps any-first-event
        # confirmation for that op.
        vol.Optional("expected_state"): vol.Any(str, None),
    }
    return {
        vol.Required("type"): WS_BULK_CALL_SERVICE,
        vol.Required("operations"): vol.All([operation], vol.Length(min=1)),
        vol.Optional("parallel", default=True): bool,
        vol.Optional("wait", default=True): bool,
        vol.Optional("timeout", default=CALL_SERVICE_DEFAULT_TIMEOUT): vol.All(
            vol.Any(int, float), vol.Range(min=0, max=CALL_SERVICE_MAX_TIMEOUT)
        ),
    }


def _dashboard_edit_schema() -> dict[Any, Any]:
    """The additive edit command; cross-field validation precedes any save."""
    return {
        vol.Required("type"): WS_DASHBOARD_EDIT,
        vol.Optional("url_path"): vol.Any(str, None),
        vol.Optional("expected_hash"): vol.Any(str, None),
        vol.Optional("config"): dict,
        vol.Optional("patch"): list,
    }
