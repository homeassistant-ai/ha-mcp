"""Wire contract constants, capabilities and limits for the WebSocket commands."""

from __future__ import annotations

import re

__all__ = [
    "ALL_SEARCH_TYPES",
    "BLUEPRINT_DOMAINS",
    "CALL_SERVICE_DEFAULT_TIMEOUT",
    "CALL_SERVICE_MAX_TIMEOUT",
    "CAPABILITIES",
    "COLLECTION_HELPER_DOMAINS",
    "CONFIG_SEARCH_TYPES",
    "DEFAULT_LIMIT",
    "ENTITY_COMPONENTS_KEY",
    "FLOW_HELPER_DOMAINS",
    "FUZZY_THRESHOLD",
    "HELPERS_LIST_COLLECTION_DOMAINS",
    "HIDDEN_SCORE_PENALTY",
    "LIMITS",
    "MAX_BODY_BYTES",
    "MAX_RESULTS",
    "REGISTRY_KINDS",
    "SCHEMA_VERSION",
    "SEARCH_TYPE_AUTOMATION",
    "SEARCH_TYPE_ENTITY",
    "SEARCH_TYPE_HELPER",
    "SEARCH_TYPE_SCENE",
    "SEARCH_TYPE_SCRIPT",
    "SERVER_ENTRY_UPDATE_FLUSH_DELAY_S",
    "SERVER_ENTRY_UPDATE_MAX_PIP_SPEC",
    "WS_API_PREFIX",
    "WS_BACKUP_PREP",
    "WS_BLUEPRINT_GET",
    "WS_BULK_CALL_SERVICE",
    "WS_CALL_SERVICE",
    "WS_CONFIG_ENTRIES",
    "WS_DASHBOARDS",
    "WS_DASHBOARD_EDIT",
    "WS_DEVICE_GET",
    "WS_DEVICE_LIST",
    "WS_ENTITY_ENRICH",
    "WS_ENTITY_LOOKUP",
    "WS_EXPOSURE",
    "WS_HELPERS_LIST",
    "WS_INFO",
    "WS_OVERVIEW",
    "WS_REFERENCE_DATA",
    "WS_REGISTRIES",
    "WS_REGISTRY_LOOKUP",
    "WS_SEARCH",
    "WS_SERVER_ENTRY",
    "WS_SERVER_ENTRY_UPDATE",
    "WS_SERVICES_LIST",
    "WS_STATES",
    "WS_SYSTEM_SNAPSHOT",
    "WS_TEMPLATE_DIAGNOSE",
    "_SPLIT_RE",
]

# --- Wire contract -----------------------------------------------------------
WS_API_PREFIX = "ha_mcp_tools"
WS_INFO = f"{WS_API_PREFIX}/info"
WS_SEARCH = f"{WS_API_PREFIX}/search"
WS_OVERVIEW = f"{WS_API_PREFIX}/overview"
WS_HELPERS_LIST = f"{WS_API_PREFIX}/helpers_list"
WS_STATES = f"{WS_API_PREFIX}/states"
WS_BLUEPRINT_GET = f"{WS_API_PREFIX}/blueprint_get"
WS_DEVICE_GET = f"{WS_API_PREFIX}/device_get"
WS_DEVICE_LIST = f"{WS_API_PREFIX}/device_list"
WS_ENTITY_ENRICH = f"{WS_API_PREFIX}/entity_enrich"
WS_EXPOSURE = f"{WS_API_PREFIX}/exposure"
WS_CONFIG_ENTRIES = f"{WS_API_PREFIX}/config_entries"
WS_REGISTRY_LOOKUP = f"{WS_API_PREFIX}/registry_lookup"
WS_SYSTEM_SNAPSHOT = f"{WS_API_PREFIX}/system_snapshot"
WS_ENTITY_LOOKUP = f"{WS_API_PREFIX}/entity_lookup"
WS_BACKUP_PREP = f"{WS_API_PREFIX}/backup_prep"
WS_REGISTRIES = f"{WS_API_PREFIX}/registries"
WS_DASHBOARDS = f"{WS_API_PREFIX}/dashboards"
WS_DASHBOARD_EDIT = f"{WS_API_PREFIX}/dashboard_edit"
WS_SERVICES_LIST = f"{WS_API_PREFIX}/services_list"
WS_REFERENCE_DATA = f"{WS_API_PREFIX}/reference_data"
WS_SERVER_ENTRY = f"{WS_API_PREFIX}/server_entry"
WS_SERVER_ENTRY_UPDATE = f"{WS_API_PREFIX}/server_entry_update"
WS_CALL_SERVICE = f"{WS_API_PREFIX}/call_service"
WS_BULK_CALL_SERVICE = f"{WS_API_PREFIX}/bulk_call_service"
WS_TEMPLATE_DIAGNOSE = f"{WS_API_PREFIX}/template_diagnose"

# Wire-format generation of the request/response envelopes. Bumped only on an
# *incompatible* shape change to an existing command; additive fields do not
# bump it (the server checks ``schema_version >= N`` before using a new shape).
SCHEMA_VERSION = 1

# Advertised command support and additive feature flags; the server gates
# each consumer on ``capability in caps.capabilities``. Never remove an entry
# without a major bump. (``info`` is always present in 1.1.0+, so it carries no
# capability key of its own.)
CAPABILITIES: list[str] = [
    "search",
    # Location/state filters precede pagination; search accepts any result window.
    "search_unified",
    # A flag on search: gates its additive result_fields request and generic
    # is_group/member_entity_ids response fields.
    "search_entity_membership",
    "overview",
    "helpers_list",
    "states",
    "blueprint_get",
    # A flag on blueprint_get: gates its additive ``yaml`` result field (the raw
    # on-disk blueprint text). The server only asks for the text when this is
    # advertised, so an older build simply serves the parsed body and the server
    # looks for the text elsewhere.
    "blueprint_text",
    "device_get",
    "device_list",
    # A semantic flag shared by every device-registry-backed read. Components
    # predating this flag enumerate only Core's main ``devices`` collection and
    # cannot provide authoritative Core 2026.9 child-device/effective-area data.
    # A newer server therefore falls back to Core's native registry endpoints
    # unless this flag accompanies the individual command capability.
    "device_registry_child_semantics",
    "entity_enrich",
    "exposure",
    "config_entries",
    "registry_lookup",
    "system_snapshot",
    "entity_lookup",
    "backup_prep",
    "registries",
    "dashboards",
    "dashboard_edit",
    # A flag, not a standalone command: gates the additive whole-document
    # search-result keys on ``ha_mcp_tools/dashboards`` mode=search
    # (``document_matches`` + ``yaml_skipped`` + ``load_failed``, issue #2008).
    # The server's ha_search dashboard bucket routes through the component only
    # when this is advertised — an older component without the keys would
    # silently narrow coverage to the card-scoped walk and hide load failures.
    "dashboards_doc_search",
    "services_list",
    "reference_data",
    # A flag, not a standalone command: gates the optional ``visibility`` param
    # the server may pass to ``ha_mcp_tools/search`` so an old component that
    # would ignore the param is never sent it (param-sniffing is banned for
    # routing; the CAPABILITIES flag is what the server gates on).
    "search_visibility",
    # A semantic flag on search_visibility: this component understands the
    # ``allowlist_authorization`` wire key (revised allowlist precedence). The
    # server sends that key only to a component advertising this flag, and with
    # an active allowlist requires the flag or falls back to its legacy resolver.
    "search_visibility_allowlist_authorization",
    "server_entry",
    # The WRITE counterpart of ``server_entry`` (Phase 3). The server gates its
    # ``ha_dev_manage_server(update_source)`` embedded-mode direct-write on this; an
    # old component that lacks it is never sent the frame and the server stays on its
    # legacy options-flow submit.
    "server_entry_update",
    # The first WRITE capability (Phase 3). The server gates its ``ha_call_service``
    # component route on this; an old component that lacks it is never sent a
    # component write and stays on the legacy REST path.
    "call_service",
    # The BATCH write capability (Phase 3, D5a). The server gates its bulk-control
    # component route on this; a component that lacks it is never sent a batch
    # write and stays on the legacy per-entity path.
    "bulk_call_service",
    # The server's ha_eval_template asks for a failed template's line only when
    # this is advertised; without it the error is returned as Core reported it.
    "template_diagnose",
]

# The registry kinds ``ha_mcp_tools/registries`` can serve. The WS schema gates
# on this so an out-of-range kind never reaches the reader; ``category`` also
# requires ``category_scopes`` (categories are scoped).
REGISTRY_KINDS = ("area", "floor", "label", "category")

# Blueprint domains this component will read a body for. Mirrors core's blueprint
# domains; the WS schema gates on it so an out-of-range domain never reaches the
# path jail. Kept next to the blueprint command it governs.
BLUEPRINT_DOMAINS = ("automation", "script")

# Advisory caps advertised in ``info.limits`` so no single WS frame balloons.
MAX_RESULTS = 500
MAX_BODY_BYTES = 1_000_000
LIMITS = {"max_results": MAX_RESULTS, "max_body_bytes": MAX_BODY_BYTES}

DEFAULT_LIMIT = 10

# ``call_service`` confirmation-wait bounds. The default mirrors the legacy
# ``ha_call_service`` 10s subscribe-and-sample window; the cap bounds a
# caller-supplied ``timeout`` (schema ``vol.Range``) so a single write frame can
# never park the WS connection for longer than this. ``blocking=True`` only means
# HA finished DISPATCHING — a mesh device (Zigbee/Z-Wave) may still be settling —
# so the wait is bounded and its expiry is ``partial``, never a failure (D4).
CALL_SERVICE_DEFAULT_TIMEOUT = 10.0
CALL_SERVICE_MAX_TIMEOUT = 60.0

# Delay before ``server_entry_update``'s deferred ``async_update_entry`` fires, so
# the WS ``{scheduled: True}`` response flushes to the calling (embedded) server
# BEFORE the resulting entry reload tears that server's thread down. Mirrors the
# server side's ``tools_dev._SELF_ACTION_FLUSH_DELAY_S`` (its legacy options-flow
# self-restart uses the same headroom for the ingress/webhook hop).
SERVER_ENTRY_UPDATE_FLUSH_DELAY_S = 1.0

# pip_spec defence-in-depth cap (the SERVER already validates per D6): a single-line
# requirement string under this many chars. Mirrors ``tools_dev._update_source``.
SERVER_ENTRY_UPDATE_MAX_PIP_SPEC = 500

# Fuzzy floor + hidden penalty, mirrored from the server so the two scorers do
# not drift (guarded by the golden parity test).
FUZZY_THRESHOLD = 70
HIDDEN_SCORE_PENALTY = 20

# --- Search surfaces ---------------------------------------------------------
SEARCH_TYPE_ENTITY = "entity"
SEARCH_TYPE_AUTOMATION = "automation"
SEARCH_TYPE_SCRIPT = "script"
SEARCH_TYPE_SCENE = "scene"
SEARCH_TYPE_HELPER = "helper"
ALL_SEARCH_TYPES = [
    SEARCH_TYPE_ENTITY,
    SEARCH_TYPE_AUTOMATION,
    SEARCH_TYPE_SCRIPT,
    SEARCH_TYPE_SCENE,
    SEARCH_TYPE_HELPER,
]
# raw_config surfaces reached via each domain's EntityComponent in hass.data.
CONFIG_SEARCH_TYPES = (
    SEARCH_TYPE_AUTOMATION,
    SEARCH_TYPE_SCRIPT,
    SEARCH_TYPE_SCENE,
)

# Collection ("storage collection") helpers — entities in the state machine.
# Matched on entity_id / friendly_name AND the live state-attribute body (an
# input_select's ``options``, an input_number's ``min``/``max``/``step``, …).
COLLECTION_HELPER_DOMAINS = frozenset(
    {
        "input_boolean",
        "input_number",
        "input_text",
        "input_select",
        "input_datetime",
        "input_button",
        "counter",
        "timer",
        "schedule",
    }
)
# Flow (config-entry-backed) helpers. Indexed from ``entry.options`` / ``title``
# directly — no OptionsFlow start/abort dance, and NEVER ``entry.data``.
FLOW_HELPER_DOMAINS = frozenset(
    {
        "template",
        "group",
        "utility_meter",
        "threshold",
        "derivative",
        "integration",
        "min_max",
        "statistics",
        "trend",
        "tod",
        "random",
        "switch_as_x",
        "mold_indicator",
        "history_stats",
        "bayesian",
        "filter",
        "generic_thermostat",
        "generic_hygrostat",
        "combine",
    }
)

# Collection helper domains enumerated by ``ha_mcp_tools/helpers_list``: the
# collection helpers ``search`` indexes PLUS zone/person, which are state-machine
# entities the server's ``ha_config_list_helpers`` also accepts. Kept SEPARATE
# from :data:`COLLECTION_HELPER_DOMAINS` so search behaviour is unchanged — zones
# and persons are not indexed as "helpers" by ``ha_mcp_tools/search``.
#
# ``tag`` is deliberately EXCLUDED: tags are a storage collection with no state
# entity (the server reaches them via ``tag/list``, and its create/list paths
# special-case ``tag`` precisely because it has no entity_id), so a from-states
# scan can never enumerate them. Advertising it as covered would make an empty
# result indistinguishable from "no tags exist" (a silent-wrong listing); it is
# left OUT of ``covered_types`` so the server falls back to its legacy
# ``tag/list`` path for that type. See :func:`_do_helpers_list`.
HELPERS_LIST_COLLECTION_DOMAINS = COLLECTION_HELPER_DOMAINS | frozenset(
    {"zone", "person"}
)

# Every ``EntityComponent`` self-registers here (core's
# ``entity_component.DATA_INSTANCES``). Collection-helper domains (input_*,
# counter, timer, schedule) do NOT set ``hass.data[DOMAIN]`` and their
# ``StorageCollection`` is a setup-local (``helpers/collection.py`` writes
# nothing to ``hass.data``), so this registry is how their component — and thus
# each entity's storage ``_config`` body — is reached. See
# :func:`_collection_storage_index`.
ENTITY_COMPONENTS_KEY = "entity_components"

_SPLIT_RE = re.compile(r"[._\-\s]+")
