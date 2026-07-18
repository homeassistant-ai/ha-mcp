"""In-process WebSocket command surface for the ha_mcp_tools component.

This module registers versioned ``ha_mcp_tools/*`` WebSocket commands that the
ha-mcp server calls in-process (same HA core, no REST/WS round-trips) behind a
capability gate. It registers twenty-two commands (twenty-two capabilities — the
``search_visibility`` capability is a flag on the existing ``search`` command,
and ``info`` itself carries no capability entry):

* ``ha_mcp_tools/info`` — the handshake: ``schema_version`` + ``capabilities[]``
  + ``component_version`` + advisory ``limits`` + the instance ``timezone``
  (an additive field consumers detect by presence — no capability entry). One
  cached probe tells the server which commands are live (capability
  negotiation, NOT a version floor).
* ``ha_mcp_tools/search`` — a unified in-process search over live registries and
  states, joined and scored, mirroring today's ``ha_search`` response envelope.
* ``ha_mcp_tools/overview`` — the raw in-process reads the server's
  ``get_system_overview`` + ``ha_get_overview`` wrapper consume (states,
  services, entity/device/area registries, ``hass.config``, persistent
  notifications, repairs issues) in one call, so the server builds its existing
  overview envelope with no extra HA round-trips.
* ``ha_mcp_tools/helpers_list`` — collection helpers (live state-attribute
  bodies) AND flow helpers (``ConfigEntry.options``/``title``/``entry_id`` —
  never ``entry.data``), each with the CURRENT entity_id + display name from the
  registry (renamed helpers show current values — issue #1794), closing the
  documented "flow helpers cannot be listed" gap with no OptionsFlow dance. The
  response's ``covered_types`` names which helper_type values were authoritatively
  enumerated, so the server falls back to its legacy ``<type>/list`` path for an
  uncovered type (e.g. ``tag``, which has no state entity) instead of trusting an
  empty result.
* ``ha_mcp_tools/states`` — a bulk state read: ``State.as_dict()`` for each
  requested entity_id (a pure ``hass.states.get`` in-memory read) plus the list
  of ids with no state, so the server's ``ha_get_state`` serves a 100-entity
  bulk call from one in-process frame instead of up to 100 REST GETs. The body
  is byte-identical to the REST ``/api/states/<id>`` serialization by
  construction; the server maps found/missing onto its per-id error contract.
* ``ha_mcp_tools/blueprint_get`` — the full body of one installed blueprint
  (``{metadata, config}``), which core's ``blueprint/list`` never returns (it
  serves only ``{metadata}``). The path is jailed under
  ``<config>/blueprints/<domain>/`` (symlink-safe containment, mirroring the
  file-tool jail) and the file read + parse run off the event loop in the async
  prep. ``!input`` markers are preserved; every other custom tag (``!secret`` /
  ``!include`` / …) is neutralized to ``None`` at load time, so no resolved
  secret plaintext can ever reach the body.
* ``ha_mcp_tools/device_get`` — one device registry entry by id
  (``{device: <DeviceEntry.dict_repr> | None}``), so a single-device lookup no
  longer pulls the entire device registry. The body is core's
  ``DeviceEntry.dict_repr`` returned VERBATIM — byte-identical to one element of
  ``config/device_registry/list`` (which sends ``json_bytes(entry.dict_repr)``)
  by construction, since this command's ``connection.send_result`` runs the same
  JSON encoder over the same dict. Consumers keep their own transforms over the
  raw shape; ``device`` is ``None`` when no such device exists. With
  ``include_entities`` set, a sibling ``entities`` key carries the device's
  entity-registry rows (``RegistryEntry.as_partial_dict``, the
  ``config/entity_registry/list`` shape, disabled entities included) so listing a
  device's entities no longer pulls the whole entity registry either — the raw
  DeviceEntry stays untouched; the join is a sibling.
* ``ha_mcp_tools/device_list`` — every device registry entry as that same raw
  ``DeviceEntry.dict_repr`` shape (``{devices: [...]}``): the in-process
  equivalent of ``config/device_registry/list`` served through the component
  seam, so ``ha_get_device`` list mode need not mix a legacy WS read with the
  component path.
* ``ha_mcp_tools/entity_enrich`` — the area/floor/labels/aliases join for a set
  of entity_ids (``{entities: {id: {area, floor, labels, aliases}}}``), computed
  by the SAME ``_entity_record``/``_RegistryView`` registry join the search path
  uses (device-inherited area/labels included). Lets ``ha_get_entity`` add the
  resolved-name enrichment fields the raw registry entry lacks (it carries
  ``area_id`` / label *ids*, not resolved names) without the caller fanning out
  its own area/floor/label registry reads. Registry-only entities (no state) are
  enriched too — the join keys off the registry, not the state machine.
* ``ha_mcp_tools/exposure`` — voice-assistant exposure with names/areas attached.
  List mode mirrors core's ``ws_list_exposed_entities`` (``{exposed_entities:
  {id: {assistant: True}}}`` — byte-identical to ``homeassistant/expose_entity/
  list``); single-entity mode reads core's module-level
  ``async_get_entity_settings``. Both add a sibling ``entity_info`` map enriching
  each id through the same registry join (friendly_name/domain/area/floor/labels),
  so the server no longer needs a second search + manual correlation to name an
  exposed entity. Three parity guardrails hold: only ``should_expose``-true
  assistants are reported (the raw helper is not pre-filtered like the legacy
  shape); core's ``HomeAssistantError("Unknown entity")`` on a junk id degrades to
  the not-exposed default (the legacy path never raises on junk); and a missing
  ``hass.states.get(id)`` omits the live-state fields (friendly_name/state) rather
  than crashing.
* ``ha_mcp_tools/dashboards`` — Lovelace dashboards read in-process, three modes.
  ``list`` mirrors the ``lovelace/dashboards/list`` row shape (id/url_path/title/
  icon/show_in_sidebar/require_admin) with an additive per-row ``mode`` so the
  server can exclude YAML dashboards; ``get`` returns one dashboard's config body
  (``await store.async_load`` in the async prep) with a structured
  ``yaml_excluded`` status for YAML-mode dashboards (the server falls back to
  legacy for those — a YAML body may carry resolved ``!secret`` plaintext);
  ``search`` walks every STORAGE dashboard's views/cards/sections (plus view-level
  badges and sections-view header cards) for a query substring (capped at 200 with a
  ``truncated`` flag). YAML-dashboard bodies are never emitted — storage-only. All
  Store loads run in :func:`_dashboards_prep`.
* ``ha_mcp_tools/services_list`` — the REST ``/api/services`` service catalog
  (``async_get_all_descriptions``) joined with the ``services`` backend
  translations (``async_get_translations``), both loaded in the async prep.
  Filtered by ``domain`` (exact) only; the server re-runs its exact query filter +
  pagination over the payload. No ``query`` coarse-filter (a per-service superset of
  the server's concatenation-based filter cannot be built cheaply, and no consumer
  forwards ``query``).
* ``ha_mcp_tools/reference_data`` — the service index + entity-id universe the
  config-reference validator consumes: ``{services: [{domain, services:{name:{}}}],
  entity_ids: [...]}``. The ``services`` shape is the REST ``/api/services`` list
  ``build_service_index`` reads (bodies are empty dicts — the index only reads
  keys); ``entity_ids`` is every ``hass.states.async_all()`` id. Pure, no prep.
* ``ha_mcp_tools/search`` (``search_visibility`` capability) — the search command
  additionally accepts a raw ``visibility`` config dict; when present it excludes
  the server's opt-in hidden entities before counts/pagination (the pure
  :func:`_visibility_hidden_set` mirrors the server's ``hidden_entity_ids``, with
  the Assist dimension delegated to core's ``async_should_expose``), so
  ``ha_search`` can route through the component even with an active filter. A
  degraded dimension (unknown ``exclude_category`` / empty-registry allowlist /
  unavailable Assist) fails open, and :func:`_visibility_warnings` returns the
  resolver-parity ``visibility_warnings`` the response carries so the filtering is
  not silently incomplete.
* ``ha_mcp_tools/server_entry`` — the component locates its OWN server config
  entry (``entry.data[CONF_ENTRY_TYPE] == server``, the one marker key it reads
  from ``entry.data``) and returns ``{entry_id, channel, pip_spec}`` (channel /
  pip_spec from ``entry.options``), so ``ha_dev_manage_server`` need not probe
  every ``ha_mcp_tools``-domain entry's options-flow schema from the outside.
* ``ha_mcp_tools/call_service`` — the FIRST write capability. Fires exactly one
  ``hass.services.async_call`` in-process and returns the REAL pre→post state
  transition for the target ``entity_ids`` — event-confirmed via an
  ``EVENT_STATE_CHANGED`` listener registered BEFORE the dispatch (closing the
  fast-entity race). The server's optional ``expected_state`` hint governs only WHEN
  the confirming state is settled — the waiter confirms on reaching it (skipping a
  multi-phase service's intermediate states and attribute-only noise) and immediate-
  matches an idempotent no-op — but the RETURNED transition is always the real
  observed one, never the hint. All awaiting work (the dispatch, the immediate-match,
  the bounded confirmation wait) runs in :func:`_call_service_prep`;
  :func:`_do_call_service` is a pure formatter. An AUTHORITATIVE component-side
  domain block refuses ``domain == "ha_mcp_tools"`` (case/whitespace-normalized)
  BEFORE any ``has_service``/dispatch, independent of (and in addition to) the
  server-side guard — so this second write path can NEVER be turned into an
  in-process invoker of the admin-gated ``ha_mcp_tools.*`` services
  (``get_caller_token`` → arbitrary config-dir file/YAML writes). A confirmation
  timeout is reported as ``partial`` (``success`` still holds); a failure BEFORE
  the dispatch raises, a failure AFTER it does not (the call already landed).
* ``ha_mcp_tools/bulk_call_service`` — the BATCH write capability (D5a). One frame
  runs the D1 ``ha_mcp_tools`` domain block for EVERY operation FIRST, before any
  dispatch or listener: a batch is fail-closed — one refused op raises the whole
  frame and NOTHING dispatches, so no partial batch can smuggle a
  ``ha_mcp_tools.*`` (or unknown-service) op past the guard. It then registers ALL
  confirmation listeners in one synchronous pass BEFORE any dispatch
  (register-before-fire is trivially correct for the batch), fires the operations
  (``parallel`` by default, or sequentially), and waits on ONE shared deadline for
  every op's transition. A per-op ``async_call`` failure under ``parallel`` is
  captured on that op's result (``error`` + ``dispatched: false``) WITHOUT aborting
  the others; a post-dispatch confirmation timeout is ``partial``, never a failure.
  All awaiting work lives in :func:`_bulk_call_service_prep`;
  :func:`_do_bulk_call_service` is a pure formatter that reuses the single
  ``call_service`` guard / transition / diff helpers.

* ``ha_mcp_tools/config_entries`` — config entries as the ``config_entries/get``
  WS shape (``created_at`` / ``modified_at`` / ``entry_id`` / ``domain`` /
  ``title`` / ``state`` / ``source`` / ``supports_*`` / ``supported_subentry_types``
  / ``pref_disable_*`` / ``disabled_by`` / ``reason`` / ``options`` /
  ``subentries`` — the full ``as_json_fragment`` field set), filtered by ``domain``
  or fetched by
  ``entry_id``. ``state`` is serialized as ``ConfigEntryState.value`` (mirroring
  core's ``as_json_fragment``). ``entry.data`` (integration credentials) is
  NEVER read; ``options`` is passed through a resolved-``!secret`` scrub (an
  options leaf equal to a ``secrets.yaml`` value becomes ``"**redacted**"``,
  loaded off the loop by :func:`_config_entries_prep`) — data minimization
  parity with the flow-helper indexing.
* ``ha_mcp_tools/registry_lookup`` — entity-registry rows
  (``RegistryEntry.as_partial_dict``, the ``config/entity_registry/list`` shape,
  disabled entities included) for either a set of ``entity_ids`` (missing ids in
  a sibling ``missing`` list) or ALL entities bound to a ``config_entry_id``. The
  config-entry scan returns EVERY match — it does not reuse the single-valued
  ``_entities_by_config_entry`` index, so a multi-entity flow helper
  (utility_meter + its tariffs) does not silently lose its sub-entities. Exactly
  one of the two is required; a request with NEITHER raises
  ``HomeAssistantError`` rather than silently returning an empty result.
* ``ha_mcp_tools/system_snapshot`` — one consistent synchronous pass over the
  live objects the health path reads: ``config_entries`` (identity fields only —
  no options/subentries), ``issues`` (the ``_overview_repairs`` slice),
  ``entities`` (the ``registry_lookup`` row shape), ``states``
  (``State.as_dict()``). ``include_*`` flags gate each section. Reading them in a
  single frame kills the 3x ``config_entries/get`` TOCTOU the server had.
* ``ha_mcp_tools/entity_lookup`` — registry entries whose ``unique_id`` matches
  (optionally narrowed by ``domain`` / ``platform``), returned as
  ``{matches: [{entity_id, unique_id, platform, domain, config_entry_id,
  categories, disabled_by, hidden_by}]}``. Multiple matches across platforms are
  all returned; the server picks. The in-process read is authoritative
  immediately (no registry-write settle retry).
* ``ha_mcp_tools/backup_prep`` — the backup identity the server needs before a
  create: ``{agent_ids, local_agent_id, default_password}`` read from the backup
  integration's in-process ``DATA_MANAGER``. ``local_agent_id`` uses the SAME
  preference the server's ``_get_local_backup_agent_id`` does (``hassio.local``
  over ``backup.local``). A missing backup integration / manager raises
  ``HomeAssistantError`` so the server's command-error fallback fires. The
  password is sensitive but the legacy ``backup/config/info`` already serves it
  to the same admin connection — parity, not new exposure.
* ``ha_mcp_tools/registries`` — the area / floor / label / category registries as
  the FULL-FIELD ``config/<x>_registry/list`` shapes (byte-compatible with the
  legacy WS list responses; timestamps as ``created_at`` / ``modified_at``
  floats via ``.timestamp()``). Only the requested ``registries`` keys are
  present; ``category`` REQUIRES a non-empty ``category_scopes`` (categories are
  scoped) — a ``category`` request without one raises ``HomeAssistantError``
  rather than silently serving ``{}``. ``category_registry`` is imported
  function-locally (not needed at module top).

``ha_mcp_tools/config_get`` was withdrawn before release: it served an entity's
``raw_config``, whose freshness lags the config file between a write and the next
completed reload (no version marker distinguishes a fresh body from a stale one),
so a get racing a reload returned a pre-edit body. ``ha_config_get_{automation,
script}`` stay on the legacy REST path (which reads the fresh config file);
scenes were already legacy-only. A file-reading redesign may return (issue #1813).

Design notes that are load-bearing:

* **Capability negotiation, not version-lockstep.** ``CAPABILITIES`` grows one
  entry per shipped command (except the always-present ``info`` handshake); the
  server asks "do you support ``search``?" rather than "are you >= X". The
  manifest version is reported for display only.
* **Data minimization.** Flow-helper indexing reads ``ConfigEntry.options`` /
  ``title`` only — **never** ``ConfigEntry.data`` (integration credentials).
* **YAML config bodies are never emitted.** automation/script/scene bodies are
  indexed for *matching*, but a matched item's ``config`` body is returned only
  when it is storage/editor-backed AND ``include_config`` is set. YAML-loaded
  items return identity/metadata only (their ``raw_config`` may carry resolved
  ``!secret`` plaintext). Body emission for YAML belongs to a future file-based
  tool.
* **Resolved secrets are scrubbed from the match corpus.** Because YAML bodies
  (and flow-helper options) can hold ``!secret`` values resolved to plaintext,
  a body leaf that exactly equals a ``secrets.yaml`` value is dropped before
  scoring (:func:`_load_secret_values`) — otherwise a query equal to a suspected
  secret would confirm it via ``match_in_config`` (a probe oracle). Blocked, not
  merely unemitted.
* **Event-loop hygiene.** Every registry/state join is a pure in-memory read
  over live data — run synchronously, no persistent index (always fresh, zero
  cache-invalidation surface). The one blocking read — ``secrets.yaml`` for the
  match-corpus scrub — runs in the executor via the command wrapper's async
  pre-step (:func:`_search_prep`), never on the event loop.

Extension point — to add another command later: write ``_do_<name>(hass,
params)``, append its capability to :data:`CAPABILITIES`, and add one row to
:func:`_command_specs`. ``info`` enumerates the rest.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import voluptuous as vol
import yaml  # type: ignore[import-untyped]
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers import (
    floor_registry as fr,
)
from homeassistant.helpers import (
    issue_registry as ir,
)
from homeassistant.helpers import (
    label_registry as lr,
)

from .const import (
    COMPONENT_VERSION,
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    OPT_CHANNEL,
    OPT_PIP_SPEC,
)

_LOGGER = logging.getLogger(__name__)

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
WS_SERVICES_LIST = f"{WS_API_PREFIX}/services_list"
WS_REFERENCE_DATA = f"{WS_API_PREFIX}/reference_data"
WS_SERVER_ENTRY = f"{WS_API_PREFIX}/server_entry"
WS_CALL_SERVICE = f"{WS_API_PREFIX}/call_service"
WS_BULK_CALL_SERVICE = f"{WS_API_PREFIX}/bulk_call_service"

# Wire-format generation of the request/response envelopes. Bumped only on an
# *incompatible* shape change to an existing command; additive fields do not
# bump it (the server checks ``schema_version >= N`` before using a new shape).
SCHEMA_VERSION = 1

# Which commands exist. Grows one entry per shipped command; the server gates
# each consumer on ``capability in caps.capabilities``. Never remove an entry
# without a major bump. (``info`` is always present in 1.1.0+, so it carries no
# capability key of its own.)
CAPABILITIES: list[str] = [
    "search",
    "overview",
    "helpers_list",
    "states",
    "blueprint_get",
    "device_get",
    "device_list",
    "entity_enrich",
    "exposure",
    "config_entries",
    "registry_lookup",
    "system_snapshot",
    "entity_lookup",
    "backup_prep",
    "registries",
    "dashboards",
    "services_list",
    "reference_data",
    # A flag, not a standalone command: gates the optional ``visibility`` param
    # the server may pass to ``ha_mcp_tools/search`` so an old component that
    # would ignore the param is never sent it (param-sniffing is banned for
    # routing; the CAPABILITIES flag is what the server gates on).
    "search_visibility",
    "server_entry",
    # The first WRITE capability (Phase 3). The server gates its ``ha_call_service``
    # component route on this; an old component that lacks it is never sent a
    # component write and stays on the legacy REST path.
    "call_service",
    # The BATCH write capability (Phase 3, D5a). The server gates its bulk-control
    # component route on this; a component that lacks it is never sent a batch
    # write and stays on the legacy per-entity path.
    "bulk_call_service",
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


# =============================================================================
# Registration (thin @websocket_command wrappers over the pure `_do_*` funcs)
# =============================================================================
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the ``ha_mcp_tools/*`` WebSocket commands.

    Idempotent: HA's ``async_register_command`` overwrites an existing handler,
    so re-running on a config-entry reload is harmless. Called from the tools
    config-entry setup alongside the service registrations.
    """
    for schema, do_fn, prep in _command_specs():
        websocket_api.async_register_command(hass, _build_handler(schema, do_fn, prep))
    _LOGGER.debug(
        "Registered ha_mcp_tools WS commands: schema_version=%s capabilities=%s",
        SCHEMA_VERSION,
        CAPABILITIES,
    )


def _command_specs() -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, pure-handler, async-prep) rows. Append one row per command.

    ``prep`` (or ``None``) is an ``async`` pre-step run before the pure handler;
    it returns keyword args merged into the ``do_fn`` call. It is the seam for a
    command that must touch the filesystem/network off the event loop —
    :func:`_search_prep` loads ``secrets.yaml`` in the executor — keeping every
    ``_do_*`` function a pure, synchronous in-memory read.
    """
    return [
        (_info_schema(), lambda hass, msg: _do_info(hass), None),
        (_search_schema(), _do_search, _search_prep),
        (_overview_schema(), _do_overview, None),
        (_helpers_list_schema(), _do_helpers_list, _helpers_list_prep),
        (_states_schema(), _do_states, None),
        (_blueprint_get_schema(), _do_blueprint_get, _blueprint_get_prep),
        (_device_get_schema(), _do_device_get, None),
        (_device_list_schema(), _do_device_list, None),
        (_entity_enrich_schema(), _do_entity_enrich, None),
        (_exposure_schema(), _do_exposure, None),
        (_config_entries_schema(), _do_config_entries, _config_entries_prep),
        (_registry_lookup_schema(), _do_registry_lookup, None),
        (_system_snapshot_schema(), _do_system_snapshot, None),
        (_entity_lookup_schema(), _do_entity_lookup, None),
        (_backup_prep_schema(), _do_backup_prep, None),
        (_registries_schema(), _do_registries, None),
        (_dashboards_schema(), _do_dashboards, _dashboards_prep),
        (_services_list_schema(), _do_services_list, _services_list_prep),
        (_reference_data_schema(), _do_reference_data, None),
        (_server_entry_schema(), _do_server_entry, None),
        # The first WRITE command: the dispatch + the bounded confirmation wait are
        # inherently async, so ALL of the work lives in the ``_call_service_prep``
        # async pre-step and ``_do_call_service`` is a pure response formatter.
        (_call_service_schema(), _do_call_service, _call_service_prep),
        # The BATCH write command (D5a): the same async seam — all guards, the
        # register-before-fire pass, the dispatches, and the bounded wait live in
        # ``_bulk_call_service_prep``; ``_do_bulk_call_service`` is a pure formatter.
        (
            _bulk_call_service_schema(),
            _do_bulk_call_service,
            _bulk_call_service_prep,
        ),
    ]


def _build_handler(schema: dict[Any, Any], do_fn: Any, prep: Any = None) -> Any:
    """Wrap a pure ``_do_*`` function as an admin-gated WS command handler.

    An optional ``prep`` async pre-step runs first (off-loop I/O such as the
    ``secrets.yaml`` read); the keyword args it returns are passed to ``do_fn``.
    """

    @websocket_api.websocket_command(schema)
    @websocket_api.require_admin
    @websocket_api.async_response
    async def _handler(
        hass: HomeAssistant, connection: Any, msg: dict[str, Any]
    ) -> None:
        extra = await prep(hass, msg) if prep is not None else {}
        connection.send_result(msg["id"], do_fn(hass, msg, **extra))

    return _handler


def _info_schema() -> dict[Any, Any]:
    return {vol.Required("type"): WS_INFO}


# The nine hide dimensions ``VisibilityConfig.to_wire`` emits, split by wire type
# (seven id/name lists, two bool flags). Kept in lockstep with the server's
# ``to_wire`` and :func:`_visibility_hidden_set`; a new dimension is a new component
# capability, added on BOTH sides (see :func:`_visibility_param_schema`). The union
# is pinned equal to the server resolver's key set by the cross-seam contract test.
_VISIBILITY_LIST_KEYS = (
    "exclude_categories",
    "deny_entity_ids",
    "exclude_areas",
    "exclude_labels",
    "allow_entity_ids",
    "allow_areas",
    "allow_labels",
)
_VISIBILITY_BOOL_KEYS = ("exclude_hidden", "respect_assist_exposure")


def _visibility_param_schema() -> Any:
    """Voluptuous schema for the ``search`` ``visibility`` dict — exactly the nine keys.

    Enumerating the known dimensions (PREVENT_EXTRA is voluptuous' default for a
    nested ``Schema``) makes an unknown key a loud ``invalid_format`` command error
    rather than a silent drop. If a newer server emits a tenth dimension to this
    1.2.0 component, the server's error taxonomy converts that into a legacy fallback
    with the filter STILL correctly applied — structural fail-closed for free —
    instead of partial, unwarned filtering. Built at call time so it honors the
    monkeypatched ``vol`` in the unit suite.
    """
    schema: dict[Any, Any] = {vol.Optional(key): [str] for key in _VISIBILITY_LIST_KEYS}
    schema.update({vol.Optional(key): bool for key in _VISIBILITY_BOOL_KEYS})
    return vol.Schema(schema)


def _search_schema() -> dict[Any, Any]:
    return {
        vol.Required("type"): WS_SEARCH,
        vol.Optional("query"): vol.Any(str, None),
        vol.Optional("search_types"): [vol.In(ALL_SEARCH_TYPES)],
        vol.Optional("domain_filter"): str,
        vol.Optional("area_filter"): str,
        vol.Optional("state_filter"): str,
        vol.Optional("exact", default=True): bool,
        vol.Optional("include_hidden", default=True): bool,
        vol.Optional("include_config", default=False): bool,
        vol.Optional("limit", default=DEFAULT_LIMIT): vol.All(
            int, vol.Range(min=1, max=MAX_RESULTS)
        ),
        vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
        # Opt-in entity-visibility filter (``search_visibility`` capability): the
        # server's raw VisibilityConfig hide dimensions. When present and
        # non-empty, hidden entities are excluded from the entity results before
        # counts/pagination, mirroring the legacy ``load_hidden_set`` hard-exclude
        # so ``ha_search`` can drop its "filter active -> legacy only" gate. Gated
        # by the CAPABILITIES flag, so an old component never receives it. The nine
        # known keys are enumerated so an unknown dimension fails loudly (the server
        # then falls back to legacy with the filter applied) — see
        # :func:`_visibility_param_schema`.
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


# =============================================================================
# ha_mcp_tools/info
# =============================================================================
def _do_info(hass: HomeAssistant | None = None) -> dict[str, Any]:
    """Return the handshake payload.

    ``timezone`` is an additive field (``hass.config.time_zone``) consumers detect
    by presence — it carries NO capability entry and does NOT bump
    ``schema_version``. ``hass`` is optional (defaulting to ``None`` so a direct
    ``_do_info()`` still works for callers that only need the static handshake);
    when absent, ``timezone`` degrades to ``None``.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "component_version": COMPONENT_VERSION,
        "capabilities": list(CAPABILITIES),
        "limits": dict(LIMITS),
        "timezone": _config_time_zone(hass),
    }


def _config_time_zone(hass: HomeAssistant | None) -> str | None:
    """``hass.config.time_zone`` as a string, guarded against a hass-less call."""
    config = getattr(hass, "config", None)
    tz = getattr(config, "time_zone", None)
    return tz if isinstance(tz, str) and tz else None


# =============================================================================
# ha_mcp_tools/search
# =============================================================================
@dataclass
class _RegistryView:
    """Bundle of the five HA registries (any may be ``None`` if unavailable)."""

    entity: Any = None
    area: Any = None
    floor: Any = None
    label: Any = None
    device: Any = None


def _resolve_registries(hass: HomeAssistant) -> _RegistryView:
    """Snapshot the five registries. Test seam — monkeypatched in unit tests."""
    return _RegistryView(
        entity=_safe(er.async_get, hass),
        area=_safe(ar.async_get, hass),
        floor=_safe(fr.async_get, hass),
        label=_safe(lr.async_get, hass),
        device=_safe(dr.async_get, hass),
    )


def _safe(fn: Any, hass: HomeAssistant) -> Any:
    try:
        return fn(hass)
    except Exception:  # pragma: no cover - defensive; core drift
        return None


def _substrate_unavailable(name: str) -> Exception:
    """Build a ``HomeAssistantError`` for a drifted / unavailable core substrate.

    A core registry / service / state accessor that RAISED or was renamed comes
    back as ``None`` (registries, via ``_safe``) or a non-``Mapping``
    (services / descriptions) from the guarded readers. For the reads whose WHOLE
    answer is that substrate — ``entity_lookup``, ``registries``,
    ``reference_data``, ``services_list`` — returning a well-formed EMPTY would let
    the server trust an authoritative-negative it should instead fall back to legacy
    for (mistaking core DRIFT for "no such entry" / "empty catalog"). Raising routes
    the server's command-error path to its legacy WS/REST read, mirroring
    :func:`_backup_unavailable` / :func:`_registries_missing_category_scopes`. A
    genuinely-EMPTY-but-present substrate (a real empty registry) is NOT drift and
    keeps returning its empty result — the guards below key off unavailability
    (``None`` / non-``Mapping``), never off an empty-but-valid collection.
    """
    from homeassistant.exceptions import HomeAssistantError

    err: Exception = HomeAssistantError(
        f"ha_mcp_tools: the {name} is unavailable (core drift); the server should "
        "fall back to its legacy read"
    )
    return err


def _do_search(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    secret_values: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Unified in-process search. Pure over ``hass`` — the WS wrapper is thin.

    Joins live registries + states, scores per the server's tiers, paginates
    per surface, and returns the ``ha_search``-shaped envelope.

    ``secret_values`` is the resolved-``!secret`` scrub set, loaded off the event
    loop by :func:`_search_prep` and passed in (default empty — the loader is
    skipped for an entity-only search, and direct callers/tests supply it
    explicitly). It keeps this function a pure, synchronous in-memory read.
    """
    query_lower = (params.get("query") or "").strip().lower()
    match_all = not query_lower
    exact = params.get("exact", True)
    include_hidden = params.get("include_hidden", True)
    include_config = params.get("include_config", False)
    limit = params.get("limit", DEFAULT_LIMIT)
    offset = params.get("offset", 0)
    search_types = params.get("search_types") or ALL_SEARCH_TYPES
    domain_filter = params.get("domain_filter")
    area_filter = params.get("area_filter")
    state_filter = params.get("state_filter")
    # Opt-in visibility filter (search_visibility capability). A non-empty dict of
    # the server's raw VisibilityConfig fields; applied as a hard entity exclude.
    visibility = params.get("visibility")

    view = _resolve_registries(hass)
    diagnostics: dict[str, int] = {}
    partial_reasons: list[str] = []
    # Visibility degradation warnings (unknown category / empty-registry allowlist /
    # Assist unavailable), collected in the entity block below when a visibility
    # filter is applied. Surfaced additively so the fast path isn't silent about
    # incomplete filtering (parity with the server's load_hidden_set warnings).
    visibility_warnings: list[str] = []

    # ``secret_values`` (loaded off-loop by _search_prep) scrubs resolved-!secret
    # plaintext from the config-body match corpus: a YAML-loaded automation/script/
    # scene body (or a flow-helper's options) can carry a secret resolved to
    # plaintext, and matching inside it would make ha_search a probe oracle (query
    # a suspected secret, confirm via match_in_config). See _load_secret_values.

    # --- Entities ------------------------------------------------------------
    entities: list[dict[str, Any]] = []
    entity_total = 0
    entity_has_more = False
    if SEARCH_TYPE_ENTITY in search_types:
        scored_entities = _search_entities(
            hass,
            view,
            query_lower,
            match_all=match_all,
            exact=exact,
            include_hidden=include_hidden,
            domain_filter=domain_filter,
            area_filter=area_filter,
            state_filter=state_filter,
        )
        # Opt-in visibility filter: a hard exclude applied BEFORE counts/pagination,
        # exactly where the legacy path drops ``visibility_hidden`` entities at the
        # top of ``_match_exact_search_entity`` (independent of ``include_hidden``,
        # which the ``_search_entities`` join already applied). See
        # :func:`_visibility_hidden_set`.
        if isinstance(visibility, Mapping) and visibility:
            states_list = _iter_states(hass)
            # Probe Assist availability once (only when the config asks for it) so a
            # requested-but-unavailable Assist dimension both skips its hiding and
            # surfaces the resolver-parity degradation warning.
            assist_available = (
                _assist_exposure_available(hass)
                if visibility.get("respect_assist_exposure")
                else True
            )
            hidden = _visibility_hidden_set(
                view,
                states_list,
                visibility,
                lambda eid: _assist_should_expose(hass, eid),
                assist_available=assist_available,
            )
            visibility_warnings = _visibility_warnings(
                view, states_list, visibility, assist_available=assist_available
            )
            if hidden:
                scored_entities = [
                    r for r in scored_entities if r["entity_id"] not in hidden
                ]
        scored_entities.sort(key=lambda r: (-r["score"], r["entity_id"]))
        entity_total = len(scored_entities)
        page = scored_entities[offset : offset + limit]
        entity_has_more = offset + len(page) < entity_total
        entities = [_project_entity(r) for r in page]

    # --- Config surfaces (automations + scripts + scenes + helpers) ----------
    # One combined pagination window, mirroring the server's config branch.
    combined: list[tuple[str, dict[str, Any]]] = []
    for domain in CONFIG_SEARCH_TYPES:
        if domain in search_types:
            combined.extend(
                (domain, rec)
                for rec in _search_config_surface(
                    hass,
                    view,
                    domain,
                    query_lower,
                    match_all=match_all,
                    exact=exact,
                    include_config=include_config,
                    partial_reasons=partial_reasons,
                    diagnostics=diagnostics,
                    secret_values=secret_values,
                )
            )
    if SEARCH_TYPE_HELPER in search_types:
        combined.extend(
            ("helper", rec)
            for rec in _search_helpers(
                hass,
                query_lower,
                match_all=match_all,
                exact=exact,
                include_config=include_config,
                secret_values=secret_values,
            )
        )

    combined.sort(key=lambda item: (-item[1]["score"], _sort_key(item[1])))
    config_total = len(combined)
    config_page = combined[offset : offset + limit]
    config_has_more = offset + len(config_page) < config_total

    buckets: dict[str, list[dict[str, Any]]] = {
        "automations": [],
        "scripts": [],
        "scenes": [],
        "helpers": [],
    }
    bucket_of = {
        SEARCH_TYPE_AUTOMATION: "automations",
        SEARCH_TYPE_SCRIPT: "scripts",
        SEARCH_TYPE_SCENE: "scenes",
        "helper": "helpers",
    }
    for surface, rec in config_page:
        buckets[bucket_of[surface]].append(rec)

    result: dict[str, Any] = {
        "entities": entities,
        "entity_total_matches": entity_total,
        "entity_has_more": entity_has_more,
        "automations": buckets["automations"],
        "scripts": buckets["scripts"],
        "scenes": buckets["scenes"],
        "helpers": buckets["helpers"],
        "config_total_matches": config_total,
        "config_has_more": config_has_more,
        "partial": bool(partial_reasons),
        "partial_reason": " ; ".join(partial_reasons) if partial_reasons else None,
    }
    if diagnostics:
        result["diagnostics"] = diagnostics
    # Additive (present only when non-empty, no schema_version bump): the server's
    # ha_search consumer merges these into the response's top-level warnings.
    if visibility_warnings:
        result["visibility_warnings"] = visibility_warnings
    return result


def _sort_key(rec: dict[str, Any]) -> str:
    """Stable tiebreak for combined config sorting."""
    return str(rec.get("entity_id") or rec.get("id") or rec.get("name") or "")


async def _search_prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Async pre-step for ``search``: load the secret-scrub set off the loop.

    The scrub only applies to config/helper surfaces, so an entity-only search
    skips the ``secrets.yaml`` read entirely (perf gate). When a scrubbed surface
    is requested, the blocking ``open()`` + ``yaml.safe_load`` runs in the
    executor via :meth:`hass.async_add_executor_job` so the WS handler never
    blocks the event loop. The loaded set is handed to :func:`_do_search`.
    """
    search_types = msg.get("search_types") or ALL_SEARCH_TYPES
    scrub_surfaces = (*CONFIG_SEARCH_TYPES, SEARCH_TYPE_HELPER)
    if not any(st in search_types for st in scrub_surfaces):
        return {"secret_values": frozenset()}
    values = await hass.async_add_executor_job(_load_secret_values, hass)
    return {"secret_values": values}


def _load_secret_scrub(hass: HomeAssistant) -> tuple[frozenset[str], bool]:
    """Load the ``secrets.yaml`` scrub values, plus a ``degraded`` flag.

    Returns ``(values, degraded)``. ``values`` are the plaintext string forms of the
    instance's ``secrets.yaml`` scalars; they scrub resolved ``!secret`` plaintext
    out of two surfaces: the config-body match corpus (so ``ha_search`` cannot be a
    probe oracle — a query equal to a suspected secret confirmed via
    ``match_in_config``) and the ``options`` emitted by ``config_entries`` /
    ``helpers_list`` (so a resolved secret never leaves the component).

    Both string AND numeric scalars are collected as their ``str()`` form: an
    unquoted ``alarm_code: 1234`` is a YAML int, and a config-entry option can carry
    that secret back as an int leaf, so the scrub must be able to match it whether it
    arrives as ``1234`` or ``"1234"``. bool scalars are excluded ("True"/"False" are
    never credentials and would over-redact).

    ``degraded`` is True ONLY when a ``secrets.yaml`` is PRESENT but could not be
    read/parsed: the scrub then silently turns OFF, and a caller emitting options can
    surface ``degraded`` so an unredacted response is not mistaken for a cleanly
    scrubbed one. An ABSENT ``secrets.yaml`` (the common case) is NOT degraded —
    there is simply nothing to scrub.

    Defensive by design — never raises into the WS handler. Loaded off the event loop
    by the async preps once per call, never cached, so an edited ``secrets.yaml``
    applies on the next call. ``secrets.yaml`` is a flat ``key: value`` mapping with
    no custom tags, so the plain ``yaml.safe_load`` (not HA's ``!secret``/
    ``!include`` loader) reads it correctly.
    """
    config = getattr(hass, "config", None)
    path_fn = getattr(config, "path", None)
    if not callable(path_fn):
        return frozenset(), False
    try:
        path = path_fn("secrets.yaml")
        if not path:
            return frozenset(), False
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError:
        # Expected: many instances have no secrets.yaml — nothing to scrub.
        return frozenset(), False
    except Exception:
        # Present-but-unreadable / malformed / permission error: unexpected, so warn
        # once (this runs once per call) AND report degraded so the emission callers
        # can signal that options were NOT redacted, rather than raising into the WS
        # handler.
        _LOGGER.warning(
            "Could not read secrets.yaml for the secret-scrub; continuing WITHOUT "
            "redaction (emitted options may be unredacted)",
            exc_info=True,
        )
        return frozenset(), True
    if not isinstance(raw, dict):
        return frozenset(), False
    return _collect_secret_strings(raw), False


def _collect_secret_strings(raw: dict[Any, Any]) -> frozenset[str]:
    """Plaintext ``str()`` forms of ``secrets.yaml`` scalars (str/int/float).

    bool scalars are excluded ("True"/"False" are never credentials and would
    over-redact); empty strings are dropped. See :func:`_load_secret_scrub`.
    """
    values: set[str] = set()
    for v in raw.values():
        if isinstance(v, bool):
            continue
        if isinstance(v, str):
            if v:
                values.add(v)
        elif isinstance(v, (int, float)):
            values.add(str(v))
    return frozenset(values)


def _load_secret_values(hass: HomeAssistant) -> frozenset[str]:
    """The ``secrets.yaml`` scrub set (see :func:`_load_secret_scrub`); degraded dropped.

    The ``search`` corpus scrub is best-effort and does not surface the degraded
    signal (its filtering degrading open is the pre-PR behaviour); the
    ``config_entries`` / ``helpers_list`` emission preps call
    :func:`_load_secret_scrub` directly so they can surface it.
    """
    values, _degraded = _load_secret_scrub(hass)
    return values


# --- Entity join + scoring ---------------------------------------------------
def _search_entities(
    hass: HomeAssistant,
    view: _RegistryView,
    query_lower: str,
    *,
    match_all: bool,
    exact: bool,
    include_hidden: bool,
    domain_filter: str | None,
    area_filter: str | None,
    state_filter: str | None,
) -> list[dict[str, Any]]:
    """Score every state against the query over the joined registry view."""
    results: list[dict[str, Any]] = []
    area_filter_lower = area_filter.lower() if area_filter else None
    for state in _iter_states(hass):
        rec = _entity_record(state, view)
        if domain_filter and rec["domain"] != domain_filter:
            continue
        if rec["_hidden"] and not include_hidden:
            continue
        if state_filter is not None and rec["state"] != state_filter:
            continue
        if area_filter_lower is not None and not _entity_matches_area(
            rec, area_filter_lower
        ):
            continue

        if match_all:
            score: int | None = _apply_hidden_penalty(100, rec["_hidden"])
            match_type = "match_all"
        else:
            tier = _text_tier(query_lower, rec["_match_texts"], fuzzy=not exact)
            if tier is None:
                continue
            score = _apply_hidden_penalty(tier, rec["_hidden"])
            match_type = _entity_match_type(
                query_lower,
                rec["entity_id"],
                rec["friendly_name"],
                rec["domain"],
                rec["aliases"],
                exact=exact,
            )
        rec["score"] = score
        rec["match_type"] = match_type
        results.append(rec)
    return results


def _entity_match_type(
    query_lower: str,
    entity_id: str,
    friendly: str,
    domain: str,
    aliases: list[str],
    *,
    exact: bool,
) -> str:
    """Classify an entity hit into the server's match_type taxonomy.

    The server labels matches two ways and the component must be
    indistinguishable from it:

    - **exact mode** — the server's ``_match_exact_search_entity`` stamps a flat
      ``"exact_match"`` on every hit, so mirror that constant.
    - **fuzzy mode** — the server's ``FuzzySearchEngine`` emits a richer set that
      agents key on. ``"alias_match"`` wins when the hit is driven by an alias
      token the id/name don't already carry (the engine's ``alias_hit`` tracking
      — closes #1166); otherwise the ``_get_match_type`` tiers: ``exact_id`` /
      ``exact_name`` / ``exact_domain`` / ``partial_id`` / ``partial_name``,
      falling to ``fuzzy_match``.
    """
    if exact:
        return "exact_match"
    if _is_alias_driven(query_lower, entity_id, friendly, aliases):
        return "alias_match"
    return _get_match_type_tier(query_lower, entity_id, friendly, domain)


def _is_alias_driven(
    query_lower: str, entity_id: str, friendly: str, aliases: list[str]
) -> bool:
    """Whether a query token lands only on an alias, mirroring the engine's alias_hit.

    Collects the alias tokens (and each alias's separator-stripped concat form)
    that are NOT already present in the id/name token set; a query token in that
    set means the friendly_name / id alone would not have surfaced this entity.
    """
    id_tail = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
    id_name_tokens = set(_tokenize(entity_id)) | set(_tokenize(str(friendly)))
    id_name_tokens.add(_SPLIT_RE.sub("", id_tail.lower()))
    id_name_tokens.add(_SPLIT_RE.sub("", str(friendly).lower()))
    alias_only: set[str] = set()
    for alias in aliases:
        a_lower = str(alias).lower()
        for tok in _tokenize(a_lower):
            if tok not in id_name_tokens:
                alias_only.add(tok)
        a_concat = _SPLIT_RE.sub("", a_lower)
        if a_concat and a_concat not in id_name_tokens:
            alias_only.add(a_concat)
    return bool(set(_tokenize(query_lower)) & alias_only)


def _get_match_type_tier(
    query_lower: str, entity_id: str, friendly: str, domain: str
) -> str:
    """The server's ``_get_match_type`` id/name/domain tiers (non-alias hits)."""
    eid = entity_id.lower()
    fname = str(friendly).lower()
    if query_lower == eid:
        return "exact_id"
    if query_lower == fname:
        return "exact_name"
    if query_lower == domain.lower():
        return "exact_domain"
    if query_lower in eid:
        return "partial_id"
    if query_lower in fname:
        return "partial_name"
    return "fuzzy_match"


def _registry_enrichment(view: _RegistryView, entity_id: str) -> dict[str, Any]:
    """Join one entity_id with the entity/device/area/floor/label registries.

    The shared registry read behind BOTH the search record (:func:`_entity_record`)
    and the ``entity_enrich`` / ``exposure`` commands, so the area/floor/label-name
    resolution lives in exactly one place. Resolves the entity's aliases plus its
    area / floor / label NAMES (device-inherited when the entity itself carries
    none), keyed off the entity registry — no ``State`` object required, so a
    registry-only (stateless) entity is enriched too. Returns the public
    enrichment fields (``area`` / ``floor`` / ``labels`` / ``aliases``) alongside
    the internal ``_area_id`` / ``_hidden`` / ``_dev_texts`` the scorer consumes.
    """
    reg = _reg_entity(view, entity_id)
    # String entries only: HA core's aliases can carry the COMPUTED_NAME
    # sentinel (entity_registry.ComputedNameType._singleton, "the computed
    # entity name is an alias"). Blind str() published it as a literal
    # "ComputedNameType._singleton" alias on every carrying entity — fake data
    # in results AND a scored match_text. The name it stands for is already
    # matched via ``friendly``, so dropping the sentinel loses nothing.
    aliases = (
        sorted(a for a in (getattr(reg, "aliases", None) or []) if isinstance(a, str))
        if reg
        else []
    )
    area_id = getattr(reg, "area_id", None) if reg else None
    device_id = getattr(reg, "device_id", None) if reg else None
    labels = set(getattr(reg, "labels", None) or []) if reg else set()
    hidden = bool(getattr(reg, "hidden_by", None)) if reg else False

    dev = _device(view, device_id) if device_id else None
    dev_texts: list[str] = []
    if dev is not None:
        if area_id is None:
            area_id = getattr(dev, "area_id", None)
        labels |= set(getattr(dev, "labels", None) or [])
        for attr in ("name_by_user", "name", "manufacturer", "model"):
            val = getattr(dev, attr, None)
            if val:
                dev_texts.append(str(val))

    return {
        "area": _area_name(view, area_id),
        "floor": _floor_name_for_area(view, area_id),
        "labels": _label_names(view, labels),
        "aliases": aliases,
        "_area_id": area_id,
        "_hidden": hidden,
        "_dev_texts": dev_texts,
    }


def _entity_record(state: Any, view: _RegistryView) -> dict[str, Any]:
    """Join a state with the entity/device/area/floor/label registries."""
    entity_id = getattr(state, "entity_id", "") or ""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    attrs = getattr(state, "attributes", None) or {}
    friendly = attrs.get("friendly_name", entity_id)

    join = _registry_enrichment(view, entity_id)
    area_name = join["area"]
    floor_name = join["floor"]
    label_names = join["labels"]
    aliases = join["aliases"]

    # Scored texts extend the server's id + friendly-name pair with the specific
    # joined identifiers (alias / area / floor / label / device). The bare domain
    # is deliberately excluded: matching it would score every entity of a domain
    # at the exact tier (a "light" query flooding all lights), which the server
    # does not do — domain is a filter dimension, not a scored text.
    match_texts = [entity_id, friendly, *aliases, *label_names, *join["_dev_texts"]]
    if area_name:
        match_texts.append(area_name)
    if floor_name:
        match_texts.append(floor_name)

    return {
        "entity_id": entity_id,
        "friendly_name": friendly,
        "domain": domain,
        "state": getattr(state, "state", "unknown"),
        "area": area_name,
        "floor": floor_name,
        "labels": label_names,
        "aliases": aliases,
        "_hidden": join["_hidden"],
        "_area_id": join["_area_id"],
        "_match_texts": match_texts,
    }


def _entity_matches_area(rec: dict[str, Any], area_filter_lower: str) -> bool:
    area_id = rec.get("_area_id")
    if area_id and str(area_id).lower() == area_filter_lower:
        return True
    area_name = rec.get("area")
    return bool(area_name and str(area_name).lower() == area_filter_lower)


def _project_entity(rec: dict[str, Any]) -> dict[str, Any]:
    """Strip internal ``_``-prefixed keys for the wire response."""
    return {
        "entity_id": rec["entity_id"],
        "friendly_name": rec["friendly_name"],
        "domain": rec["domain"],
        "state": rec["state"],
        "area": rec["area"],
        "floor": rec["floor"],
        "labels": rec["labels"],
        "aliases": rec["aliases"],
        "score": rec["score"],
        "match_type": rec["match_type"],
    }


# --- Config surfaces (automation/script/scene) -------------------------------
def _search_config_surface(
    hass: HomeAssistant,
    view: _RegistryView,
    domain: str,
    query_lower: str,
    *,
    match_all: bool,
    exact: bool,
    include_config: bool,
    partial_reasons: list[str],
    diagnostics: dict[str, int],
    secret_values: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Score one config domain's loaded entities (raw_config indexed, not emitted for YAML)."""
    component = hass.data.get(domain) if getattr(hass, "data", None) else None
    entities = getattr(component, "entities", None)
    if entities is None:
        diagnostics["config_components_inaccessible"] = (
            diagnostics.get("config_components_inaccessible", 0) + 1
        )
        return []

    results: list[dict[str, Any]] = []
    for entity in entities:
        entity_id = getattr(entity, "entity_id", None)
        if not entity_id:
            continue
        name, item_id, config_dict = _extract_config(domain, entity)
        source = _classify_source(item_id)

        if match_all:
            score: int | None = 100
            match_in_name = False
            match_in_config = False
        else:
            scored = _config_score(
                query_lower,
                entity_id,
                name,
                config_dict,
                exact=exact,
                secret_values=secret_values,
            )
            if scored is None:
                continue
            score, match_in_name, match_in_config = scored

        # Scenes never emit a component-served body: a HomeAssistantScene holds no
        # raw storage dict (its states are runtime State objects), so config stays
        # None and config_dict is used only as the faithful match corpus.
        config_out: dict[str, Any] | None = None
        if (
            domain != SEARCH_TYPE_SCENE
            and include_config
            and source == "storage"
            and config_dict is not None
        ):
            if _too_large(config_dict):
                partial_reasons.append(f"{domain} {entity_id} body omitted (too large)")
            else:
                config_out = config_dict

        rec: dict[str, Any] = {
            "id": item_id,
            "entity_id": entity_id,
            "source": source,
            "score": score,
            "match_in_name": match_in_name,
            "match_in_config": match_in_config,
            "config": config_out,
        }
        # Scenes carry a "name"; automations/scripts carry an "alias".
        if domain == SEARCH_TYPE_SCENE:
            rec["name"] = name
        else:
            rec["alias"] = name
        results.append(rec)
    return results


def _extract_config(
    domain: str, entity: Any
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Return (display_name, item_id, config_dict) for a config entity.

    Uses defensive getattr because the exact accessor can drift across core
    versions: automation/script expose ``raw_config``; scenes expose
    ``scene_config`` (name/icon/id/states) rather than ``raw_config``. For a
    scene the returned ``config_dict`` is the faithful MATCH corpus only (see
    :func:`_scene_match_corpus`), never an emittable body.
    """
    entity_id = getattr(entity, "entity_id", "") or ""
    name = getattr(entity, "name", None) or entity_id
    unique_id = getattr(entity, "unique_id", None)

    if domain == SEARCH_TYPE_SCENE:
        scene_config = getattr(entity, "scene_config", None)
        config_dict = _scene_match_corpus(scene_config)
        item_id = unique_id
        if item_id is None and config_dict is not None:
            item_id = config_dict.get("id")
        if config_dict is not None:
            cfg_name = config_dict.get("name")
            if cfg_name:
                name = str(cfg_name)
        return str(name), (str(item_id) if item_id is not None else None), config_dict

    raw = getattr(entity, "raw_config", None)
    config_dict = dict(raw) if isinstance(raw, dict) else None
    item_id = unique_id
    if item_id is None and config_dict is not None:
        item_id = config_dict.get("id")
    return str(name), (str(item_id) if item_id is not None else None), config_dict


def _scene_match_corpus(scene_config: Any) -> dict[str, Any] | None:
    """Faithful, minimal MATCH corpus for a scene — never an emittable body.

    A ``HomeAssistantScene`` holds no raw storage dict: ``scene_config.states`` is
    a ``{entity_id: State}`` map of RUNTIME ``State`` objects. Scoring/emitting
    those (each stringifying to ``<state light.x=on; ...>``) was garbage and
    diverged the component's scoring from any real body. Index only the faithful,
    non-runtime facts instead: ``id`` / ``name`` / ``icon`` plus the entity-id
    KEYS of ``states`` (so "which scenes touch ``light.x``" still matches) — no
    State values, no timestamps or contexts. Used for MATCHING only; the scene
    record never emits a ``config`` body (see :func:`_search_config_surface`).
    """
    if scene_config is None:
        return None
    if isinstance(scene_config, Mapping):
        src: Mapping[str, Any] = scene_config
    else:
        collected: dict[str, Any] = {}
        for attr in ("id", "name", "icon", "states", "entities"):
            val = getattr(scene_config, attr, None)
            if val is not None:
                collected[attr] = val
        src = collected
    out: dict[str, Any] = {}
    for key in ("id", "name", "icon"):
        val = src.get(key)
        if val is not None:
            out[key] = str(val)
    entity_ids: set[str] = set()
    for key in ("states", "entities"):
        mapping = src.get(key)
        if isinstance(mapping, Mapping):
            entity_ids.update(str(k) for k in mapping)
    if entity_ids:
        out["entities"] = sorted(entity_ids)
    return out or None


def _plainify(value: Any) -> Any:
    """Best-effort conversion of registry/state objects to plain JSON-able data.

    Recurses on any ``Mapping`` (not just ``dict``): a nested ``MappingProxyType``
    (common in ``ConfigEntry.options``) must be walked into a plain dict, NOT
    stringified — otherwise a secret buried inside it survives embedded in the repr
    string, past the equality scrub that runs on the result.
    """
    if isinstance(value, Mapping):
        return {str(k): _plainify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plainify(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _classify_source(item_id: str | None) -> str:
    """Classify an automation/script/scene as storage- or YAML-backed.

    HA addresses editor-managed items by their ``id`` (the entity's
    ``unique_id``); the config editor's ``/config/<domain>/config/<id>`` REST
    path — and its edit link — key off exactly that id, and 404 for items with
    no id. So an id-bearing item is treated as ``storage`` (body emittable under
    ``include_config``); an id-less item is ``yaml`` and its body is never
    emitted from search (its ``raw_config`` may carry resolved ``!secret``
    plaintext). This is the conservative rule: the safe error is toward
    withholding a body, not leaking one.
    """
    return "storage" if item_id else "yaml"


def _too_large(config_dict: dict[str, Any]) -> bool:
    """Rough guard so a huge body never balloons a single WS frame."""
    try:
        return len(repr(config_dict)) > MAX_BODY_BYTES
    except Exception:  # pragma: no cover - defensive
        return False


# --- Helpers surface ---------------------------------------------------------
def _collection_storage_index(
    hass: HomeAssistant, domains: frozenset[str]
) -> dict[str, tuple[dict[str, Any], str | None]]:
    """Map collection-helper ``entity_id`` -> ``(storage body, storage id)``.

    Collection helpers keep their full storage config on the ``CollectionEntity``
    as ``_config`` — a schedule's weekday blocks, an input_datetime's
    ``has_date``/``has_time``, an input_boolean's ``initial`` — fields the live
    state attributes do NOT carry. The ``StorageCollection`` that loaded them is a
    setup-local (``helpers/collection.py`` writes nothing to ``hass.data``), so
    the reachable in-process source is the domain's ``EntityComponent``
    (``hass.data['entity_components'][domain]``, or ``hass.data[domain]`` for the
    automation/script/scene pattern) and each entity's ``_config``.

    Domains that decompose config into ``_attr_*`` instead of keeping ``_config``
    (input_number / input_text / input_select) have no entry here; the caller
    falls back to the state-attributes body for them (and for any YAML-defined
    helper whose entity is absent). All access is getattr-guarded against drift.
    """
    index: dict[str, tuple[dict[str, Any], str | None]] = {}
    data = getattr(hass, "data", None)
    if not isinstance(data, Mapping):
        return index
    instances = data.get(ENTITY_COMPONENTS_KEY)
    for domain in domains:
        component = (
            instances.get(domain) if isinstance(instances, Mapping) else None
        ) or data.get(domain)
        entities = getattr(component, "entities", None)
        if entities is None:
            continue
        try:
            entity_list = list(entities)
        except Exception:  # pragma: no cover - defensive
            continue
        for entity in entity_list:
            entity_id = getattr(entity, "entity_id", None)
            if not entity_id:
                continue
            raw = getattr(entity, "_config", None)
            if not isinstance(raw, dict):
                continue
            storage_id = getattr(entity, "unique_id", None) or raw.get("id")
            index[entity_id] = (
                dict(raw),
                str(storage_id) if storage_id is not None else None,
            )
    return index


def _search_helpers(
    hass: HomeAssistant,
    query_lower: str,
    *,
    match_all: bool,
    exact: bool,
    include_config: bool,
    secret_values: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Index collection helpers (states) + flow helpers (config-entry options)."""
    results: list[dict[str, Any]] = []

    # Collection helpers: entities in the state machine, matched on entity_id /
    # friendly_name AND the searchable config body. That body is the entity's real
    # storage ``_config`` (a schedule's weekday blocks, an input_select's
    # ``options`` + ``initial``, …) when reachable, falling back to the live state
    # attributes otherwise (see _collection_storage_index). The name still comes
    # from the CURRENT friendly_name, not the creation-time storage name.
    storage = _collection_storage_index(hass, COLLECTION_HELPER_DOMAINS)
    for state in _iter_states(hass):
        entity_id = getattr(state, "entity_id", "") or ""
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain not in COLLECTION_HELPER_DOMAINS:
            continue
        attrs = getattr(state, "attributes", None) or {}
        name = attrs.get("friendly_name", entity_id)
        object_id = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
        stored = storage.get(entity_id)
        if stored is not None:
            body = _plainify(stored[0])
        else:
            body = dict(attrs) if isinstance(attrs, Mapping) else {}
        if match_all:
            score: int | None = 100
            match_in_name = False
            match_in_config = False
        else:
            scored = _config_score(
                query_lower,
                entity_id,
                name,
                body,
                exact=exact,
                secret_values=secret_values,
            )
            if scored is None:
                continue
            score, match_in_name, match_in_config = scored
        results.append(
            {
                "entity_id": entity_id,
                "helper_type": domain,
                "object_id": object_id,
                "name": name,
                "kind": "collection",
                "score": score,
                "match_in_name": match_in_name,
                "match_in_config": match_in_config,
                "config": body if include_config else None,
            }
        )

    # Flow helpers: config entries — options + title ONLY, never data.
    for entry in _iter_config_entries(hass):
        domain = getattr(entry, "domain", None)
        if domain not in FLOW_HELPER_DOMAINS:
            continue
        title = getattr(entry, "title", None) or ""
        # ``ConfigEntry.options`` is a ``MappingProxyType`` in live HA, not a
        # ``dict``; the old ``isinstance(..., dict)`` guard silently dropped it to
        # ``{}``, so a flow helper's body (a template's ``state``, a group's
        # members, …) was never indexed and ``match_in_config`` could never fire.
        # Accept any ``Mapping`` so the persisted options are searchable and
        # emittable under ``include_config``.
        raw_options = getattr(entry, "options", None)
        options = dict(raw_options) if isinstance(raw_options, Mapping) else {}
        entry_id = getattr(entry, "entry_id", None)
        if match_all:
            score = 100
            match_in_name = False
            match_in_config = False
        else:
            scored = _config_score(
                query_lower,
                title,
                title,
                options,
                exact=exact,
                secret_values=secret_values,
            )
            if scored is None:
                continue
            score, match_in_name, match_in_config = scored
        results.append(
            {
                "entity_id": None,
                "helper_type": domain,
                "entry_id": entry_id,
                "name": title,
                "kind": "flow",
                "score": score,
                "match_in_name": match_in_name,
                "match_in_config": match_in_config,
                # Data minimization: options only, never entry.data.
                "options": options if include_config else None,
            }
        )
    return results


# =============================================================================
# Scoring — mirrors the server's tiers (guarded by the golden parity test)
# =============================================================================
def _apply_hidden_penalty(score: int, hidden: bool) -> int:
    """Reduce ``score`` by :data:`HIDDEN_SCORE_PENALTY` for hidden entities.

    Mirrors ``utils.fuzzy_search.apply_hidden_penalty`` so the two rankings
    stay consistent.
    """
    s = int(score)
    return max(0, s - HIDDEN_SCORE_PENALTY) if hidden else s


def _calc_ratio(a: str, b: str) -> int:
    """SequenceMatcher ratio (0-100). Mirrors ``fuzzy_search.calculate_ratio``."""
    return int(SequenceMatcher(None, a, b, autojunk=False).ratio() * 100)


def _tokenize(text: str) -> list[str]:
    """Split on ``.``/``_``/``-``/whitespace, lowercase, drop empties.

    Mirrors ``utils.fuzzy_search.tokenize``.
    """
    return [t for t in _SPLIT_RE.split(text.lower()) if t]


def _sep_normalized(text: str) -> str:
    """Collapse ``.``/``_``/``-``/whitespace runs to single spaces.

    The server's fuzzy engine (BM25) tokenizes query and documents with the
    same splitter, making ``input_boolean`` and ``input boolean`` equivalent
    queries (pinned by the e2e underscore/space-equivalence test). Comparing
    separator-normalized strings replicates that equivalence for the
    component's tier scorer.
    """
    return " ".join(_tokenize(text))


def _text_tier(query_lower: str, texts: Any, *, fuzzy: bool) -> int | None:
    """Entity tier: 100 (exact), 80 (substring), fuzzy ratio (>=threshold), or None.

    Mirrors the server's ``_match_exact_search_entity`` (100/80) over the entity
    id + friendly name, extended to the joined alias/area/floor/label/domain/
    device texts. In fuzzy mode comparisons run on BOTH the raw strings and
    their separator-normalized forms (unified tokenization — ``_``/space
    equivalence), with a whole-string ``calculate_ratio`` fallback surfacing
    typos above :data:`FUZZY_THRESHOLD`. Exact mode stays raw-only for
    byte-parity with the server's exact path.
    """
    query_norm = _sep_normalized(query_lower) if fuzzy else ""
    best_substring: int | None = None
    best_ratio = 0
    for text in texts:
        if not text:
            continue
        tier, ratio = _tier_one_text(query_lower, query_norm, str(text).lower(), fuzzy)
        if tier == 100:
            return 100
        if tier == 80:
            best_substring = 80
        elif ratio > best_ratio:
            best_ratio = ratio
    if best_substring is not None:
        return best_substring
    if fuzzy and best_ratio >= FUZZY_THRESHOLD:
        return best_ratio
    return None


def _tier_one_text(
    query_lower: str, query_norm: str, text_lower: str, fuzzy: bool
) -> tuple[int | None, int]:
    """Score one candidate text: ``(tier, ratio)``.

    Tier 100 = exact (raw, or separator-normalized in fuzzy mode); tier 80 =
    substring (same two forms); otherwise ``ratio`` carries the fuzzy
    whole-string fallback (0 when not in fuzzy mode).
    """
    if query_lower == text_lower:
        return 100, 0
    text_norm = _sep_normalized(text_lower) if fuzzy and query_norm else ""
    if text_norm and query_norm == text_norm:
        return 100, 0
    if query_lower in text_lower:
        return 80, 0
    if text_norm and query_norm in text_norm:
        return 80, 0
    if fuzzy:
        return None, _calc_ratio(query_lower, text_lower)
    return None, 0


def _name_tier(query_lower: str, texts: Any, *, exact: bool) -> int | None:
    """Config-name tier: substring => 100 (not 80), else fuzzy ratio or None.

    Config name matches are binary 100/0 in the server's exact path
    (``_score_deep_match``: ``name_exact = 100 if query in id/name else 0``),
    unlike entity matches which have the 80 substring tier.
    """
    query_norm = "" if exact else _sep_normalized(query_lower)
    best_ratio = 0
    for text in texts:
        if not text:
            continue
        text_lower = str(text).lower()
        if query_lower in text_lower:
            return 100
        if not exact:
            if query_norm and query_norm in _sep_normalized(text_lower):
                return 100
            ratio = _calc_ratio(query_lower, text_lower)
            if ratio > best_ratio:
                best_ratio = ratio
    if not exact and best_ratio >= FUZZY_THRESHOLD:
        return best_ratio
    return None


def _config_score(
    query_lower: str,
    entity_id: str,
    name: str,
    config_dict: dict[str, Any] | None,
    *,
    exact: bool,
    secret_values: frozenset[str] = frozenset(),
) -> tuple[int, bool, bool] | None:
    """Score a config surface: (total, match_in_name, match_in_config) or None.

    Exact mode is binary 100/0 with a threshold of 100 (server parity); fuzzy
    mode floors at :data:`FUZZY_THRESHOLD`. ``secret_values`` scrubs the body
    match corpus (see :func:`_search_in_dict_exact`).
    """
    name_score = _name_tier(query_lower, [entity_id, name], exact=exact) or 0
    config_score = _config_body_score(
        query_lower, config_dict, exact=exact, secret_values=secret_values
    )
    threshold = 100 if exact else FUZZY_THRESHOLD
    total = max(name_score, config_score)
    if total < threshold:
        return None
    return total, name_score >= threshold, config_score >= threshold


def _config_body_score(
    query_lower: str,
    config_dict: dict[str, Any] | None,
    *,
    exact: bool,
    secret_values: frozenset[str] = frozenset(),
) -> int:
    """Match the query against a config body's keys/values.

    Exact => 100/0 substring (``_search_in_dict_exact`` parity). Fuzzy adds a
    token-vs-token ``calculate_ratio`` fallback (the server's tier-3 path).
    ``secret_values`` scrubs resolved-``!secret`` leaves from the corpus.
    """
    if config_dict is None:
        return 0
    if _search_in_dict_exact(config_dict, query_lower, secret_values) >= 100:
        return 100
    if exact:
        return 0
    leaves: list[str] = []
    _collect_string_leaves(config_dict, leaves, secret_values)
    query_tokens = _tokenize(query_lower)
    if not query_tokens:
        return 0
    doc_tokens = {tok for leaf in leaves for tok in _tokenize(leaf)}
    best = 0
    for qt in query_tokens:
        for dt in doc_tokens:
            best = max(best, _calc_ratio(qt, dt))
    return best if best >= FUZZY_THRESHOLD else 0


def _search_in_dict_exact(
    data: Any, query_lower: str, secret_values: frozenset[str] = frozenset()
) -> int:
    """Exact substring search in nested structures (100 or 0).

    Mirrors ``smart_search._scoring.ScoringMixin._search_in_dict_exact``, plus a
    secret scrub: a string leaf that exactly equals a known secret value never
    contributes a match (see :func:`_load_secret_values`), so a query equal to a
    resolved ``!secret`` cannot be confirmed via ``match_in_config``. Keys and
    non-string scalars are never secrets, so they are matched as before.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            if query_lower in str(key).lower():
                return 100
            if _search_in_dict_exact(value, query_lower, secret_values) >= 100:
                return 100
        return 0
    if isinstance(data, (list, tuple)):
        for item in data:
            if _search_in_dict_exact(item, query_lower, secret_values) >= 100:
                return 100
        return 0
    return _leaf_exact_score(data, query_lower, secret_values)


def _leaf_exact_score(
    data: Any, query_lower: str, secret_values: frozenset[str]
) -> int:
    """Exact substring score for a scalar leaf (100 or 0).

    A string leaf that exactly equals a known secret value scores 0 — the scrub
    that keeps a resolved ``!secret`` out of the match corpus.
    """
    if isinstance(data, str):
        if data in secret_values:
            return 0
        return 100 if query_lower in data.lower() else 0
    if data is not None:
        return 100 if query_lower in str(data).lower() else 0
    return 0


def _collect_string_leaves(
    data: Any, out: list[str], secret_values: frozenset[str] = frozenset()
) -> None:
    """Recursively collect string representations. Mirrors the server helper.

    A string leaf that exactly equals a known secret value is dropped so it
    never reaches the fuzzy token corpus (the scrub in :func:`_search_in_dict_exact`
    covers the exact path).
    """
    if isinstance(data, dict):
        for key, value in data.items():
            out.append(str(key))
            _collect_string_leaves(value, out, secret_values)
    elif isinstance(data, (list, tuple)):
        for item in data:
            _collect_string_leaves(item, out, secret_values)
    elif isinstance(data, str):
        if data not in secret_values:
            out.append(data)
    elif data is not None:
        out.append(str(data))


# =============================================================================
# Registry accessors (all getattr-guarded against core drift)
# =============================================================================
def _iter_states(hass: HomeAssistant) -> list[Any]:
    states = getattr(hass, "states", None)
    getter = getattr(states, "async_all", None) if states is not None else None
    if getter is None:
        return []
    try:
        return list(getter())
    except Exception:  # pragma: no cover - defensive
        return []


def _iter_config_entries(hass: HomeAssistant) -> list[Any]:
    config_entries = getattr(hass, "config_entries", None)
    getter = (
        getattr(config_entries, "async_entries", None)
        if config_entries is not None
        else None
    )
    if getter is None:
        return []
    try:
        return list(getter())
    except Exception:  # pragma: no cover - defensive
        return []


def _reg_entity(view: _RegistryView, entity_id: str) -> Any:
    return _call_lookup(view.entity, "async_get", entity_id)


def _device(view: _RegistryView, device_id: str | None) -> Any:
    if not device_id:
        return None
    return _call_lookup(view.device, "async_get", device_id)


def _area_name(view: _RegistryView, area_id: str | None) -> str | None:
    if not area_id:
        return None
    area = _call_lookup(view.area, "async_get_area", area_id)
    name = getattr(area, "name", None) if area is not None else None
    return str(name) if name else None


def _floor_name_for_area(view: _RegistryView, area_id: str | None) -> str | None:
    if not area_id:
        return None
    area = _call_lookup(view.area, "async_get_area", area_id)
    floor_id = getattr(area, "floor_id", None) if area is not None else None
    if not floor_id:
        return None
    floor = _call_lookup(view.floor, "async_get_floor", floor_id)
    name = getattr(floor, "name", None) if floor is not None else None
    return str(name) if name else None


def _label_names(view: _RegistryView, label_ids: Any) -> list[str]:
    names: list[str] = []
    for label_id in sorted(label_ids or []):
        label = _call_lookup(view.label, "async_get_label", label_id)
        name = getattr(label, "name", None) if label is not None else None
        names.append(str(name) if name else str(label_id))
    return names


def _call_lookup(registry: Any, method: str, key: str) -> Any:
    if registry is None:
        return None
    getter = getattr(registry, method, None)
    if getter is None:
        return None
    try:
        return getter(key)
    except Exception:  # pragma: no cover - defensive
        return None


def _call_no_arg(obj: Any, method: str) -> Any:
    """Call a no-argument accessor (e.g. ``async_services``), guarded."""
    if obj is None:
        return None
    fn = getattr(obj, method, None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:  # pragma: no cover - defensive
        return None


def _iso(value: Any) -> Any:
    """Serialize a datetime-ish value to an ISO string; pass through otherwise.

    HA registry/state timestamps are ``datetime`` objects. The WS layer can
    encode them, but the REST shapes the overview consumer mirrors carry ISO
    strings, so normalize here for a stable wire contract.
    """
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:  # pragma: no cover - defensive
            return None
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def _enum_value(value: Any) -> Any:
    """Unwrap a StrEnum-ish registry field (``entity_category``/``hidden_by``/…).

    HA stores these as enums whose ``.value`` is the wire string; a plain string
    (or None) passes through unchanged. Also unwraps ``ConfigEntryState`` (a plain
    ``Enum`` whose ``.value`` is the wire string, e.g. ``"loaded"``) — core's
    ``config_entries/get`` serializes it as ``entry.state.value``.
    """
    if value is None or isinstance(value, str):
        return value
    return getattr(value, "value", str(value))


def _timestamp(value: Any) -> float | None:
    """Serialize a datetime-ish registry timestamp as a float, like core.

    core's registry WS list responses emit ``created_at`` / ``modified_at`` via
    ``entry.created_at.timestamp()`` (a float, seconds since epoch), so mirror
    that. A value that is already numeric passes through; anything else (or a
    ``.timestamp()`` that raises) degrades to ``None``.
    """
    if value is None:
        return None
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        try:
            return float(ts())
        except Exception:  # pragma: no cover - defensive
            return None
    return float(value) if isinstance(value, (int, float)) else None


def _reg_name(reg: Any) -> str | None:
    """Current display name from a registry entry: user override, else original."""
    if reg is None:
        return None
    name = getattr(reg, "name", None) or getattr(reg, "original_name", None)
    return str(name) if name else None


def _current_friendly_name(
    hass: HomeAssistant, entity_id: str | None, fallback: str | None
) -> str | None:
    """Current friendly_name from the state machine, falling back to the config name."""
    if entity_id:
        for state in _iter_states(hass):
            if getattr(state, "entity_id", None) != entity_id:
                continue
            attrs = getattr(state, "attributes", None) or {}
            friendly = (
                attrs.get("friendly_name") if isinstance(attrs, Mapping) else None
            )
            if friendly:
                return str(friendly)
            break
    if fallback:
        return str(fallback)
    return entity_id


# =============================================================================
# ha_mcp_tools/helpers_list
# =============================================================================
def _do_helpers_list(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    secret_values: frozenset[str] = frozenset(),
    secret_scrub_degraded: bool = False,
) -> dict[str, Any]:
    """List collection helpers (live state bodies) + flow helpers (config-entry options).

    Flow-helper ``options`` come straight from ``ConfigEntry.options`` — no
    OptionsFlow start/abort dance, and NEVER ``entry.data`` (integration
    credentials). Every record carries the CURRENT entity_id + display name from
    the entity registry so a renamed helper shows current values (issue #1794),
    not the stale storage-collection name.

    Flow-helper ``options`` share the credential-bearing exposure class of
    ``config_entries``'s ``options`` (a flow helper IS a config entry), so they pass
    through the SAME best-effort resolved-``!secret`` scrub for uniformity — a
    present-but-unreadable ``secrets.yaml`` degrades the scrub to a no-op and sets
    ``secret_scrub_degraded: true`` (present ONLY when degraded). Collection-helper
    bodies (storage collection / live state attributes) are not scrubbed — they carry
    no YAML-resolved secret.

    ``covered_types`` names exactly the helper_type values this command can
    enumerate (the state-machine collection domains + the flow domains, minus the
    flow set when ``include_flow_helpers`` is false). It is the anti-silent-wrong
    signal: for a requested helper_type NOT in ``covered_types`` (e.g. ``tag``,
    which has no state entity), an empty ``helpers`` list means "cannot
    enumerate", NOT "none exist" — the server must fall back to its legacy
    ``<type>/list`` path rather than trust the emptiness.
    """
    requested = params.get("helper_types")
    type_filter = frozenset(requested) if requested else None
    include_flow = params.get("include_flow_helpers", True)

    view = _resolve_registries(hass)
    helpers = _collection_helpers_list(hass, view, type_filter)
    covered = set(HELPERS_LIST_COLLECTION_DOMAINS)
    if include_flow:
        helpers.extend(_flow_helpers_list(hass, view, type_filter, secret_values))
        covered |= FLOW_HELPER_DOMAINS
    result: dict[str, Any] = {
        "helpers": helpers,
        "count": len(helpers),
        "covered_types": sorted(covered),
    }
    if include_flow and secret_scrub_degraded:
        result["secret_scrub_degraded"] = True
    return result


async def _helpers_list_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Async pre-step for ``helpers_list``: load the secret-scrub set off the loop.

    Only the flow-helper ``options`` are scrubbed, so the blocking ``secrets.yaml``
    read is skipped entirely when ``include_flow_helpers`` is false (perf gate,
    mirroring ``search``'s entity-only skip). The read runs in the executor via
    :meth:`hass.async_add_executor_job`, keeping :func:`_do_helpers_list` a pure
    in-memory read. See :func:`_load_secret_scrub`.
    """
    if not msg.get("include_flow_helpers", True):
        return {"secret_values": frozenset(), "secret_scrub_degraded": False}
    values, degraded = await hass.async_add_executor_job(_load_secret_scrub, hass)
    return {"secret_values": values, "secret_scrub_degraded": degraded}


def _collection_helpers_list(
    hass: HomeAssistant, view: _RegistryView, type_filter: frozenset[str] | None
) -> list[dict[str, Any]]:
    """Collection helpers from the state machine (input_*, counter, timer, zone, …).

    The record's ``config`` is the entity's real storage ``_config`` body when
    reachable — so a schedule surfaces its weekday blocks, which the live state
    attributes omit — falling back to the state attributes otherwise (see
    :func:`_collection_storage_index`). ``name`` stays the CURRENT display name
    (a rename updates the registry, not the storage body — issue #1794).
    """
    out: list[dict[str, Any]] = []
    storage = _collection_storage_index(hass, HELPERS_LIST_COLLECTION_DOMAINS)
    for state in _iter_states(hass):
        entity_id = getattr(state, "entity_id", "") or ""
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain not in HELPERS_LIST_COLLECTION_DOMAINS:
            continue
        if type_filter is not None and domain not in type_filter:
            continue
        attrs = getattr(state, "attributes", None) or {}
        object_id = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
        reg = _reg_entity(view, entity_id)
        # Current display name: state friendly_name reflects a registry rename;
        # fall back to the registry name, then the object_id.
        current = attrs.get("friendly_name") if isinstance(attrs, Mapping) else None
        name = current or _reg_name(reg) or object_id
        # Prefer the real storage body + id; the state attributes omit fields like
        # a schedule's weekday blocks.
        stored = storage.get(entity_id)
        if stored is not None:
            body, storage_id = stored
        else:
            body = dict(attrs) if isinstance(attrs, Mapping) else {}
            storage_id = getattr(reg, "unique_id", None) or object_id
        out.append(
            {
                "helper_type": domain,
                "kind": "collection",
                "entity_id": entity_id,
                "object_id": object_id,
                "name": str(name),
                "storage_id": storage_id,
                "config": _plainify(body),
            }
        )
    return out


def _flow_helpers_list(
    hass: HomeAssistant,
    view: _RegistryView,
    type_filter: frozenset[str] | None,
    secret_values: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Flow (config-entry-backed) helpers — options + title + entry_id, never data.

    ``options`` is passed through the same resolved-``!secret`` scrub
    ``config_entries`` applies (a flow helper is a config entry, so its ``options``
    share the same exposure class); an empty ``secret_values`` is a no-op.
    """
    out: list[dict[str, Any]] = []
    entity_by_entry = _entities_by_config_entry(view)
    for entry in _iter_config_entries(hass):
        domain = getattr(entry, "domain", None)
        if domain not in FLOW_HELPER_DOMAINS:
            continue
        if type_filter is not None and domain not in type_filter:
            continue
        entry_id = getattr(entry, "entry_id", None)
        title = getattr(entry, "title", None) or ""
        raw_options = getattr(entry, "options", None)
        options = (
            _scrub_secret_values(_plainify(dict(raw_options)), secret_values)
            if isinstance(raw_options, Mapping)
            else {}
        )
        reg = entity_by_entry.get(entry_id)
        entity_id = getattr(reg, "entity_id", None) if reg is not None else None
        name = _reg_name(reg) or _current_friendly_name(hass, entity_id, title)
        out.append(
            {
                "helper_type": domain,
                "kind": "flow",
                "entry_id": entry_id,
                "entity_id": entity_id,
                "name": str(name) if name else title,
                "storage_id": entry_id,
                # Data minimization: options only, never entry.data.
                "options": options,
            }
        )
    return out


def _entities_by_config_entry(view: _RegistryView) -> dict[Any, Any]:
    """Index the first registry entity bound to each config entry (flow helpers)."""
    index: dict[Any, Any] = {}
    for entry in _all_entity_entries(view):
        config_entry_id = getattr(entry, "config_entry_id", None)
        if config_entry_id and config_entry_id not in index:
            index[config_entry_id] = entry
    return index


def _all_entity_entries(view: _RegistryView) -> list[Any]:
    """All entity-registry entries (``registry.entities`` is a mapping in HA)."""
    reg = view.entity
    entities = getattr(reg, "entities", None) if reg is not None else None
    if entities is None:
        return []
    try:
        return list(entities.values())
    except Exception:  # pragma: no cover - defensive
        return []


# =============================================================================
# ha_mcp_tools/overview
# =============================================================================
def _do_overview(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return the raw in-process reads the server's overview path consumes.

    NOT the assembled overview envelope — the RAW slices the server's
    ``get_system_overview`` + ``ha_get_overview`` wrapper fetch today (states,
    services, entity/device/area registries, ``hass.config``, persistent
    notifications, repairs issues). The server runs its existing overview logic
    over these, so detail_level / domains / pagination stay server-side and no
    logic is duplicated (or drifts) in the component. Registries are BARE lists
    (not the ``{success, result}`` WS wrapper); the server adapts. Collapses the
    ~8 round-trips to one in-process call.

    ``slice_errors`` names any slice whose accessor RAISED (empty list when
    clean). A missing/None registry degrades to an empty slice WITHOUT an entry —
    that is "nothing here", not "failed". A genuine raise is caught per slice,
    logged, and named here so the server can tell "empty" from "failed" and fall
    back to its legacy REST read for just that slice instead of trusting the
    empty value.
    """
    include_notifications = params.get("include_notifications", True)
    include_repairs = params.get("include_repairs", True)

    view = _resolve_registries(hass)
    slice_errors: list[str] = []

    def _slice(name: str, fn: Any, default: Any) -> Any:
        try:
            return fn()
        except Exception:
            _LOGGER.warning("overview slice %r degraded", name, exc_info=True)
            slice_errors.append(name)
            return default

    result: dict[str, Any] = {
        "states": _slice("states", lambda: _overview_states(hass), []),
        "services": _slice("services", lambda: _overview_services(hass), []),
        "entity_registry": _slice(
            "entity_registry", lambda: _overview_entity_registry(view), []
        ),
        "device_registry": _slice(
            "device_registry", lambda: _overview_device_registry(view), []
        ),
        "area_registry": _slice(
            "area_registry", lambda: _overview_area_registry(view), []
        ),
        "config": _slice("config", lambda: _overview_config(hass), {}),
        "notifications": _slice(
            "notifications", lambda: _overview_notifications(hass), []
        )
        if include_notifications
        else [],
        "repairs": _slice("repairs", lambda: _overview_repairs(hass), [])
        if include_repairs
        else [],
    }
    result["slice_errors"] = slice_errors
    return result


def _overview_states(hass: HomeAssistant) -> list[dict[str, Any]]:
    """States in the ``client.get_states()`` shape the overview consumer reads."""
    out: list[dict[str, Any]] = []
    for state in _iter_states(hass):
        entity_id = getattr(state, "entity_id", None)
        if not entity_id:
            continue
        attrs = getattr(state, "attributes", None) or {}
        out.append(
            {
                "entity_id": entity_id,
                "state": getattr(state, "state", "unknown"),
                "attributes": _plainify(dict(attrs))
                if isinstance(attrs, Mapping)
                else {},
                "last_changed": _iso(getattr(state, "last_changed", None)),
                "last_updated": _iso(getattr(state, "last_updated", None)),
            }
        )
    return out


def _overview_services(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Service catalog in the ``client.get_services()`` list shape.

    The consumer's ``_build_service_stats`` reads only the per-domain service
    *names*, so each service maps to an empty dict — keeps the frame small while
    preserving the ``{domain, services: {name: {...}}}`` structure.
    """
    services = _call_no_arg(getattr(hass, "services", None), "async_services")
    if not isinstance(services, Mapping):
        return []
    out: list[dict[str, Any]] = []
    for domain, svcs in services.items():
        names = list(svcs.keys()) if isinstance(svcs, Mapping) else []
        out.append({"domain": domain, "services": {name: {} for name in names}})
    return out


def _overview_entity_registry(view: _RegistryView) -> list[dict[str, Any]]:
    """Entity registry as a bare list, with the fields the overview + visibility
    consumers read (area/device/labels/entity_category/hidden_by/options/…)."""
    out: list[dict[str, Any]] = []
    for entry in _all_entity_entries(view):
        entity_id = getattr(entry, "entity_id", None)
        if not entity_id:
            continue
        out.append(
            {
                "entity_id": entity_id,
                "area_id": getattr(entry, "area_id", None),
                "device_id": getattr(entry, "device_id", None),
                "labels": sorted(
                    str(x) for x in (getattr(entry, "labels", None) or [])
                ),
                "entity_category": _enum_value(getattr(entry, "entity_category", None)),
                "hidden_by": _enum_value(getattr(entry, "hidden_by", None)),
                "categories": _plainify(getattr(entry, "categories", None) or {}),
                "options": _plainify(getattr(entry, "options", None) or {}),
                "name": getattr(entry, "name", None),
                "original_name": getattr(entry, "original_name", None),
                "platform": getattr(entry, "platform", None),
                "unique_id": getattr(entry, "unique_id", None),
                "disabled_by": _enum_value(getattr(entry, "disabled_by", None)),
            }
        )
    return out


def _overview_device_registry(view: _RegistryView) -> list[dict[str, Any]]:
    """Device registry as a bare list (id + area + labels + name/manufacturer/model)."""
    out: list[dict[str, Any]] = []
    reg = view.device
    devices = getattr(reg, "devices", None) if reg is not None else None
    values = _mapping_values(devices)
    for dev in values:
        dev_id = getattr(dev, "id", None)
        if not dev_id:
            continue
        out.append(
            {
                "id": dev_id,
                "area_id": getattr(dev, "area_id", None),
                "labels": sorted(str(x) for x in (getattr(dev, "labels", None) or [])),
                "name": getattr(dev, "name", None),
                "name_by_user": getattr(dev, "name_by_user", None),
                "manufacturer": getattr(dev, "manufacturer", None),
                "model": getattr(dev, "model", None),
            }
        )
    return out


def _overview_area_registry(view: _RegistryView) -> list[dict[str, Any]]:
    """Area registry as a bare list (area_id + name + floor_id)."""
    out: list[dict[str, Any]] = []
    for area in _all_area_entries(view):
        area_id = getattr(area, "id", None) or getattr(area, "area_id", None)
        if not area_id:
            continue
        out.append(
            {
                "area_id": area_id,
                "name": getattr(area, "name", None),
                "floor_id": getattr(area, "floor_id", None),
            }
        )
    return out


def _all_area_entries(view: _RegistryView) -> list[Any]:
    """All area-registry entries via ``async_list_areas()`` or the ``areas`` mapping."""
    reg = view.area
    if reg is None:
        return []
    listed = _call_no_arg(reg, "async_list_areas")
    if listed is not None:
        try:
            return list(listed)
        except Exception:  # pragma: no cover - defensive
            return []
    return _mapping_values(getattr(reg, "areas", None))


def _mapping_values(mapping: Any) -> list[Any]:
    """``list(mapping.values())`` guarded against a non-mapping / drift."""
    if mapping is None:
        return []
    try:
        return list(mapping.values())
    except Exception:  # pragma: no cover - defensive
        return []


def _overview_config(hass: HomeAssistant) -> dict[str, Any]:
    """The ``hass.config`` fields the wrapper's ``_fetch_system_info`` reads.

    ``base_url`` is intentionally omitted — the server supplies it from its own
    client; only HA-core config values are the component's to provide.
    """
    config = getattr(hass, "config", None)
    raw = _call_no_arg(config, "as_dict")
    if not isinstance(raw, Mapping):
        return {}
    keys = (
        "version",
        "location_name",
        "time_zone",
        "language",
        "state",
        "country",
        "currency",
        "unit_system",
        "latitude",
        "longitude",
        "elevation",
        "components",
        "safe_mode",
        "internal_url",
        "external_url",
        "allowlist_external_dirs",
    )
    return {k: _plainify(raw[k]) for k in keys if k in raw}


def _overview_notifications(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Active persistent notifications (``persistent_notification/get`` shape)."""
    store = getattr(hass, "data", None)
    data = store.get("persistent_notification") if isinstance(store, Mapping) else None
    return [
        {
            "notification_id": _field(note, "notification_id"),
            "title": _field(note, "title"),
            "message": _field(note, "message"),
            "created_at": _iso(_field(note, "created_at")),
        }
        for note in _notification_values(data)
    ]


def _field(obj: Any, key: str) -> Any:
    """Read ``key`` from a mapping (``.get``) or an object (``getattr``)."""
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _notification_values(data: Any) -> list[Any]:
    """Notification records: ``{id: note}`` mapping values, or a bare list."""
    if isinstance(data, Mapping):
        return list(data.values())
    if isinstance(data, list):
        return list(data)
    return []


def _overview_repairs(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Raw issue-registry entries (the server filters/projects them itself).

    ``ignored`` is derived from ``dismissed_version`` so the server's
    ``filter_active_repairs`` (which keys off ``ignored``) works unchanged.
    """
    registry = _safe(ir.async_get, hass)
    issues = getattr(registry, "issues", None) if registry is not None else None
    out: list[dict[str, Any]] = []
    for issue in _mapping_values(issues):
        dismissed = getattr(issue, "dismissed_version", None)
        out.append(
            {
                "issue_id": getattr(issue, "issue_id", None),
                "domain": getattr(issue, "domain", None),
                "severity": _enum_value(getattr(issue, "severity", None)),
                "translation_key": getattr(issue, "translation_key", None),
                "translation_placeholders": _plainify(
                    getattr(issue, "translation_placeholders", None) or {}
                ),
                "ignored": dismissed is not None,
                "dismissed_version": dismissed,
                "is_fixable": getattr(issue, "is_fixable", None),
                "breaks_in_ha_version": getattr(issue, "breaks_in_ha_version", None),
                "created": _iso(getattr(issue, "created", None)),
                "issue_domain": getattr(issue, "issue_domain", None),
                "learn_more_url": getattr(issue, "learn_more_url", None),
                "active": getattr(issue, "active", None),
            }
        )
    return out


# =============================================================================
# ha_mcp_tools/states
# =============================================================================
def _do_states(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return ``State.as_dict()`` for each requested entity_id + a ``missing`` list.

    ``hass.states.get(id)`` is a pure O(1) in-memory dict read, and core's
    ``State.as_dict()`` is exactly the serialization the REST ``/api/states/<id>``
    endpoint emits — so a component-served record is byte-identical to the legacy
    per-id REST fetch by construction (the WS transport JSON-encodes the same
    datetimes to the same ISO strings the REST layer does). The body is returned
    UNMODIFIED — never ``_plainify``'d — precisely so that byte-parity holds:
    ``_plainify``'s ``str()`` would render a datetime with a space separator where
    both REST and WS use ``isoformat``'s ``T``. No freshness or secrets concern:
    state bodies are always live and carry no ``!secret`` plaintext. The server
    enforces its own ``MAX_ENTITIES`` cap before calling, so no per-frame guard is
    needed here (100 full states is well within one frame — ``overview`` already
    returns every state in one call).
    """
    entity_ids = params.get("entity_ids") or []
    states: dict[str, Any] = {}
    missing: list[str] = []
    for entity_id in entity_ids:
        state = _state_get(hass, entity_id)
        if state is None:
            missing.append(entity_id)
            continue
        as_dict = _state_as_dict(state)
        if as_dict is None:
            # A live state that could not be serialized (core drift) goes to
            # ``missing`` rather than emitting a null state indistinguishable from
            # a real value — the server maps ``missing`` onto its per-id contract.
            missing.append(entity_id)
            continue
        states[entity_id] = as_dict
    return {"states": states, "missing": missing}


def _state_get(hass: HomeAssistant, entity_id: str) -> Any:
    """``hass.states.get(entity_id)`` guarded against core drift (``None`` if absent)."""
    states = getattr(hass, "states", None)
    getter = getattr(states, "get", None) if states is not None else None
    if getter is None:
        return None
    try:
        return getter(entity_id)
    except Exception:  # pragma: no cover - defensive
        return None


def _state_as_dict(state: Any) -> Any:
    """core ``State.as_dict()`` verbatim — the REST ``/api/states/<id>`` shape.

    Returned unmodified so the WS transport encodes its datetimes with the same
    ``isoformat`` the REST layer uses (byte-parity — see :func:`_do_states`).
    """
    as_dict = getattr(state, "as_dict", None)
    if callable(as_dict):
        try:
            return as_dict()
        except Exception:  # pragma: no cover - defensive
            return None
    return None


# =============================================================================
# ha_mcp_tools/blueprint_get
# =============================================================================
def _do_blueprint_get(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one installed blueprint's full body as ``{metadata, config}``.

    core's ``blueprint/list`` returns only ``{metadata}`` (no triggers /
    conditions / actions / sequence), so the server can otherwise serve metadata
    only. This reads the on-disk blueprint file and returns the parsed body:
    ``config`` is the full file (the server merges it additively over the
    ``blueprint/list`` metadata) and ``metadata`` is its ``blueprint:`` section.
    When the file is missing, unparseable, or the requested path escapes the jail,
    both come back ``None`` (the server keeps metadata-only) — see
    :func:`_read_blueprint_file`.

    Pure: the blocking jail-resolve + file read + YAML parse run in the executor
    via :func:`_blueprint_get_prep`, which passes the parsed ``body`` in.
    """
    if not isinstance(body, dict):
        return {"metadata": None, "config": None}
    metadata = body.get("blueprint")
    return {
        "metadata": _plainify(metadata) if isinstance(metadata, dict) else None,
        "config": _plainify(body),
    }


async def _blueprint_get_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Async pre-step for ``blueprint_get``: jail + read + parse off the loop.

    The path jail (symlink-safe ``Path.resolve`` containment), the ``open()`` and
    the YAML parse are all blocking filesystem work, so they run in the executor
    via :meth:`hass.async_add_executor_job` — keeping :func:`_do_blueprint_get` a
    pure assembler over the parsed ``body`` this returns (``None`` on any failure).
    """
    domain = msg["domain"]
    path = msg["path"]
    body = await hass.async_add_executor_job(_read_blueprint_file, hass, domain, path)
    return {"body": body}


def _read_blueprint_file(
    hass: HomeAssistant, domain: str, path: str
) -> dict[str, Any] | None:
    """Resolve + jail + read + parse one blueprint YAML file. ``None`` on failure.

    Blueprint files live under ``<config>/blueprints/<domain>/``. The requested
    ``path`` is joined under that root and resolved symlink-safe (mirrors the
    file-tool jail's ``_resolves_within`` — resolve the RAW input, following
    symlinks, THEN check containment, so ``<root>/<symlink>/..`` cannot escape). A
    path escaping the root — via ``..``, an absolute path, or a symlink — yields
    ``None`` (rejected, never opened). A missing file, a non-file target, a read
    error, or a YAML parse error also yields ``None``. Only a valid, contained,
    parseable blueprint returns its full parsed body.

    Parsed with :class:`_BlueprintLoader`: ``!input`` markers are preserved and
    every other custom tag (``!secret`` / ``!include`` / …) is neutralized to
    ``None``, so no resolved secret plaintext can ever enter the returned body
    (defense in depth — blueprints use ``!input``, not ``!secret``).
    """
    config = getattr(hass, "config", None)
    path_fn = getattr(config, "path", None)
    if not callable(path_fn):
        return None
    try:
        base = Path(path_fn("blueprints", domain))
        candidate = Path(path) if path.startswith("/") else base / path
        real = candidate.resolve()
        base_real = base.resolve()
    except (OSError, ValueError):
        return None
    if not (real == base_real or real.is_relative_to(base_real)):
        return None
    try:
        with open(real, encoding="utf-8") as handle:
            # Instance form (not yaml.load) mirrors the component's existing
            # _PackagesDirLoader usage; _BlueprintLoader is a SafeLoader subclass,
            # so no !!python/object can construct arbitrary types.
            loader = _BlueprintLoader(handle)
            try:
                parsed = loader.get_single_data()
            finally:
                loader.dispose()
    except (OSError, ValueError, yaml.YAMLError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _construct_blueprint_input(loader: Any, node: Any) -> dict[str, str]:
    """Represent ``!input <name>`` as ``{"__input__": <name>}``.

    A JSON-safe, unambiguous marker of a blueprint input substitution point (the
    body is a display artifact, not a runnable config), so a consumer can see
    which fields an input fills without the tag crashing a plain safe-load.
    """
    return {"__input__": str(getattr(node, "value", ""))}


def _drop_blueprint_tag(loader: Any, tag_suffix: Any, node: Any) -> None:
    """Neutralize every non-``!input`` custom tag to ``None`` (never resolve it).

    ``!secret`` must never resolve to plaintext; ``!include`` / ``!env_var`` /
    unknown tags are irrelevant to a read-only body view. Mirrors the component's
    ``_ignore_unknown_tag`` pattern in ``__init__.py``.
    """
    return None


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader for blueprint files: keep ``!input``, neutralize all other tags."""


_BlueprintLoader.add_constructor("!input", _construct_blueprint_input)
_BlueprintLoader.add_multi_constructor("!", _drop_blueprint_tag)


# =============================================================================
# ha_mcp_tools/device_get + ha_mcp_tools/device_list
# =============================================================================
def _do_device_get(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return one device registry entry by id, optionally with its entities.

    ``{device: <DeviceEntry.dict_repr> | None}`` — ``registry.async_get(device_id)``
    is a pure O(1) in-memory dict read, and the emitted body is core's
    ``DeviceEntry.dict_repr`` returned UNMODIFIED — exactly the shape
    ``config/device_registry/list`` serializes (it sends
    ``json_bytes(entry.dict_repr)``), so a component-served record is byte-identical
    to one legacy list element by construction (the WS transport JSON-encodes the
    same dict with the same encoder). The body is never ``_plainify``'d: that would
    ``str()`` the ``disabled_by`` / ``entry_type`` enums to their repr instead of the
    wire value core's encoder emits, breaking parity. ``device`` is ``None`` when no
    such device exists — the server maps that onto its own not-found contract.

    When ``include_entities`` is set, a SIBLING ``entities`` key carries the device's
    entity-registry rows (``[<RegistryEntry.as_partial_dict>, ...]`` — the same shape
    and serialization ``config/entity_registry/list`` emits), so a single-device
    lookup no longer pulls the WHOLE entity registry to list one device's entities.
    ``er.async_entries_for_device`` is called with ``include_disabled_entities=True``
    to match what ``config/entity_registry/list`` returns (it lists disabled entities
    too). The DeviceEntry dict itself stays exactly the raw shape — the join is a
    sibling, so consumers keep their own transforms. The ``entities`` key is present
    only when requested.
    """
    device_id = params.get("device_id")
    include_entities = params.get("include_entities", False)
    view = _resolve_registries(hass)
    entry = _device(view, device_id) if device_id else None
    result: dict[str, Any] = {
        "device": _device_dict_repr(entry) if entry is not None else None
    }
    if include_entities:
        result["entities"] = _device_entities(view, device_id) if device_id else []
    return result


def _do_device_list(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return every device registry entry as ``{devices: [dict_repr, ...]}``.

    The in-process equivalent of ``config/device_registry/list``: each element is
    core's ``DeviceEntry.dict_repr`` returned VERBATIM (same byte-parity rationale
    as :func:`_do_device_get`), so the server's existing device transforms consume
    it unchanged. An entry whose ``dict_repr`` is unavailable is skipped rather
    than emitted as a partial record.
    """
    view = _resolve_registries(hass)
    reg = view.device
    devices = getattr(reg, "devices", None) if reg is not None else None
    out: list[dict[str, Any]] = []
    for dev in _mapping_values(devices):
        repr_dict = _device_dict_repr(dev)
        if repr_dict is not None:
            out.append(repr_dict)
        else:
            _LOGGER.warning(
                "device_list: skipping device %r with unavailable dict_repr",
                getattr(dev, "id", None),
            )
    return {"devices": out}


def _device_dict_repr(entry: Any) -> dict[str, Any] | None:
    """core ``DeviceEntry.dict_repr`` verbatim — the ``config/device_registry/list`` shape.

    Returned UNMODIFIED so the WS transport encodes it with the same JSON
    serializer ``config/device_registry/list`` uses (byte-parity — see
    :func:`_do_device_get`). Guarded against core drift: a missing/raising
    ``dict_repr`` yields ``None`` rather than propagating.
    """
    try:
        repr_dict = entry.dict_repr
    except Exception:  # pragma: no cover - defensive; core drift
        return None
    return repr_dict if isinstance(repr_dict, dict) else None


def _device_entities(view: _RegistryView, device_id: str) -> list[dict[str, Any]]:
    """The device's entity-registry rows as ``config/entity_registry/list`` elements.

    Each row is core's ``RegistryEntry.as_partial_dict`` returned VERBATIM (the same
    shape + serialization ``config/entity_registry/list`` emits — it sends
    ``json_bytes(entry.partial_json_repr)`` over ``as_partial_dict``), so the
    server's device<->entity map builds identically off the join or the legacy list.
    A row whose ``as_partial_dict`` is unavailable is skipped.
    """
    out: list[dict[str, Any]] = []
    for entry in _entries_for_device(view, device_id):
        partial = _entity_partial_dict(entry)
        if partial is not None:
            out.append(partial)
    return out


def _entries_for_device(view: _RegistryView, device_id: str) -> list[Any]:
    """Entity-registry entries bound to ``device_id``, disabled ones INCLUDED.

    Delegates to core's ``er.async_entries_for_device`` (its device_id index) with
    ``include_disabled_entities=True`` so the result matches what
    ``config/entity_registry/list`` returns — that command lists disabled entities
    too, and dropping them would diverge the join from the legacy shape. Guarded
    against a missing registry / core drift (returns ``[]``).
    """
    reg = view.entity
    if reg is None or not device_id:
        return []
    try:
        entries = er.async_entries_for_device(
            reg, device_id, include_disabled_entities=True
        )
    except Exception:  # pragma: no cover - defensive; core drift
        return []
    return list(entries)


def _entity_partial_dict(entry: Any) -> dict[str, Any] | None:
    """core ``RegistryEntry.as_partial_dict`` verbatim — the ``config/entity_registry/list`` shape.

    Returned UNMODIFIED so the WS transport encodes it with the same serializer
    ``config/entity_registry/list`` uses (byte-parity, mirroring
    :func:`_device_dict_repr`). Guarded against core drift.
    """
    try:
        partial = entry.as_partial_dict
    except Exception:  # pragma: no cover - defensive; core drift
        return None
    return partial if isinstance(partial, dict) else None


# =============================================================================
# ha_mcp_tools/entity_enrich
# =============================================================================
def _do_entity_enrich(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return the area/floor/labels/aliases join for each requested entity_id.

    ``{entities: {id: {area, floor, labels, aliases}}}`` — each id runs through the
    SAME :func:`_registry_enrichment` join the search path uses (device-inherited
    area/labels, resolved NAMES), so ``ha_get_entity`` adds the resolved-name
    fields the raw registry entry lacks without the caller fanning out its own
    area/floor/label registry reads. Pure O(id) in-memory registry lookups; a
    registry-only (stateless) entity is enriched too (the join keys off the
    registry, not the state machine). An id with no registry entry yields empty /
    ``None`` fields rather than being dropped, so the caller can pair the result
    back to its request by key.
    """
    entity_ids = params.get("entity_ids") or []
    view = _resolve_registries(hass)
    entities: dict[str, Any] = {}
    for entity_id in entity_ids:
        join = _registry_enrichment(view, entity_id)
        entities[entity_id] = {
            "area": join["area"],
            "floor": join["floor"],
            "labels": join["labels"],
            "aliases": join["aliases"],
        }
    return {"entities": entities}


# =============================================================================
# ha_mcp_tools/exposure
# =============================================================================
def _do_exposure(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return voice-assistant exposure with names/areas attached.

    ``{exposed_entities: {id: {assistant: True}}, entity_info: {id: {...}}}``.
    ``exposed_entities`` is byte-identical to core's ``ws_list_exposed_entities``
    result (``homeassistant/expose_entity/list``): only ``should_expose``-true
    assistants appear, and an entity with none is omitted from the map — so the
    server's existing exposure shaping consumes it unchanged. ``entity_info`` is
    the additive half: each relevant id enriched through :func:`_registry_enrichment`
    (friendly_name/domain/area/floor/labels), closing the "one call gives a bare
    ``{id: {assistant: bool}}`` map with no names/areas" gap.

    Modes:

    * single-entity (``entity_id`` set) — reads core's module-level
      ``async_get_entity_settings`` for that id and enriches it (whether exposed or
      not — the caller asked about that specific entity).
    * list (``entity_id`` omitted) — mirrors ``ws_list_exposed_entities``: walks
      the exposed-entities store ids + the entity registry, keeps the exposed ones,
      and enriches each.

    Parity guardrails (mirroring the legacy shape, pinned in tests):

    1. only ``should_expose``-true assistants are reported (the raw helper returns
       every assistant that has *any* stored option, not just exposed ones);
    2. core's ``HomeAssistantError("Unknown entity")`` on a junk id is caught and
       degrades to the not-exposed default (the legacy ``expose_entity/list`` never
       raises on a junk id);
    3. a missing ``hass.states.get(id)`` omits the live-state fields
       (friendly_name / state) from ``entity_info`` rather than crashing.
    """
    entity_id = params.get("entity_id")
    view = _resolve_registries(hass)

    if entity_id:
        exposed_to = _entity_exposed_to(hass, entity_id)
        return {
            "exposed_entities": {entity_id: exposed_to} if exposed_to else {},
            "entity_info": {entity_id: _exposure_enrichment(hass, view, entity_id)},
        }

    exposed_entities: dict[str, Any] = {}
    entity_info: dict[str, Any] = {}
    for eid in _all_exposable_entity_ids(hass, view):
        exposed_to = _entity_exposed_to(hass, eid)
        if not exposed_to:
            continue
        exposed_entities[eid] = exposed_to
        entity_info[eid] = _exposure_enrichment(hass, view, eid)
    return {"exposed_entities": exposed_entities, "entity_info": entity_info}


def _entity_exposed_to(hass: HomeAssistant, entity_id: str) -> dict[str, bool]:
    """``{assistant: True}`` for the entity's ``should_expose``-true assistants.

    Reads core's ``async_get_entity_settings`` (via the local
    :func:`_async_get_entity_settings` test-seam wrapper) and keeps only assistants
    whose settings carry a truthy ``should_expose`` (guardrail 1 — the raw helper is
    not pre-filtered like ``ws_list_exposed_entities``). A junk id whose helper raises
    ``HomeAssistantError("Unknown entity")`` degrades to ``{}`` (guardrail 2), the
    same not-exposed default the legacy path returns for an id it never listed.
    """
    try:
        settings = _async_get_entity_settings(hass, entity_id)
    except Exception as exc:
        if _is_unknown_entity_error(exc):
            return {}
        raise
    out: dict[str, bool] = {}
    if isinstance(settings, Mapping):
        for assistant, opts in settings.items():
            if isinstance(opts, Mapping) and opts.get("should_expose"):
                out[str(assistant)] = True
    return out


def _exposure_enrichment(
    hass: HomeAssistant, view: _RegistryView, entity_id: str
) -> dict[str, Any]:
    """area/floor/labels + domain for an id, plus live-state fields when present.

    Runs the id through :func:`_registry_enrichment` for area/floor/labels
    (device-inherited names). ``domain`` comes from the id itself (no state
    needed). ``friendly_name`` and ``state`` are LIVE-STATE fields: included only
    when ``hass.states.get(id)`` exists, omitted otherwise (guardrail 3 — a
    disabled / legacy-only entity has no state, so those keys are simply absent
    rather than crashing the join).
    """
    join = _registry_enrichment(view, entity_id)
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    info: dict[str, Any] = {
        "domain": domain,
        "area": join["area"],
        "floor": join["floor"],
        "labels": join["labels"],
    }
    state = _state_get(hass, entity_id)
    if state is not None:
        attrs = getattr(state, "attributes", None) or {}
        friendly = (
            attrs.get("friendly_name", entity_id)
            if isinstance(attrs, Mapping)
            else entity_id
        )
        info["friendly_name"] = str(friendly)
        info["state"] = getattr(state, "state", "unknown")
    return info


def _all_exposable_entity_ids(hass: HomeAssistant, view: _RegistryView) -> list[str]:
    """Every id ``ws_list_exposed_entities`` walks: store ids plus registry ids.

    Core iterates ``chain(exposed_entities.entities, entity_registry.entities)`` —
    the legacy store (entities WITHOUT a unique_id, exposed manually) plus every
    registry entity. This reproduces that union, de-duplicated with store-first
    order, so an exposed YAML entity that lives only in the store is not missed.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for eid in _legacy_exposed_entity_ids(hass):
        if eid and eid not in seen:
            seen.add(eid)
            ordered.append(eid)
    for entry in _all_entity_entries(view):
        entry_eid = getattr(entry, "entity_id", None)
        if entry_eid and entry_eid not in seen:
            seen.add(entry_eid)
            ordered.append(entry_eid)
    return ordered


def _async_get_entity_settings(hass: HomeAssistant, entity_id: str) -> Any:
    """core's ``async_get_entity_settings(hass, entity_id)``; test seam.

    Imported lazily so the fake-hass unit suite (which MagicMock-stubs
    ``homeassistant.*`` at import time) can monkeypatch this whole function rather
    than the deep core module. Returns ``{assistant: settings_mapping}`` and raises
    ``HomeAssistantError("Unknown entity")`` for an id in neither the registry nor
    the exposed-entities store — caught by :func:`_entity_exposed_to`.
    """
    from homeassistant.components.homeassistant.exposed_entities import (
        async_get_entity_settings,
    )

    return async_get_entity_settings(hass, entity_id)


def _legacy_exposed_entity_ids(hass: HomeAssistant) -> list[str]:
    """Entity ids in the exposed-entities store (entities without a unique_id).

    The ``exposed_entities.entities`` half of core's ``ws_list_exposed_entities``
    iteration. Imported lazily (test seam); a missing store / core drift yields
    ``[]`` so list mode still enumerates the registry half.
    """
    try:
        from homeassistant.components.homeassistant.const import (
            DATA_EXPOSED_ENTITIES,
        )

        data = getattr(hass, "data", None)
        store = data.get(DATA_EXPOSED_ENTITIES) if isinstance(data, Mapping) else None
    except Exception:  # pragma: no cover - defensive; core drift
        return []
    entities = getattr(store, "entities", None)
    if isinstance(entities, Mapping):
        return [str(eid) for eid in entities]
    return []


def _is_unknown_entity_error(exc: Exception) -> bool:
    """True for core's ``HomeAssistantError('Unknown entity')`` from the settings helper.

    Keyed off the exception type NAME (not an ``isinstance`` against the imported
    class) so the fake-hass suite — which stubs ``homeassistant.exceptions`` — can
    raise a stand-in ``HomeAssistantError`` without importing the real class. The
    type name alone is too wide: core raises a plain ``HomeAssistantError`` for
    other faults too, so the message is also required to carry ``unknown entity``
    (case-insensitive). A store-read failure that raises a bare
    ``HomeAssistantError`` therefore propagates instead of being silently reported
    as not-exposed; the audit guardrail (junk id → not-exposed default) still
    matches because that raise carries the ``Unknown entity`` message.
    """
    return (
        type(exc).__name__ == "HomeAssistantError"
        and "unknown entity" in str(exc).lower()
    )


# =============================================================================
# ha_mcp_tools/config_entries
# =============================================================================
def _do_config_entries(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    secret_values: frozenset[str] = frozenset(),
    secret_scrub_degraded: bool = False,
) -> dict[str, Any]:
    """Return config entries in the ``config_entries/get`` WS shape.

    ``{entries: [{created_at, modified_at, entry_id, domain, title, state, source,
    supports_options, supports_remove_device, supports_unload, supports_reconfigure,
    supported_subentry_types, pref_disable_new_entities, pref_disable_polling,
    disabled_by, reason, error_reason_translation_key,
    error_reason_translation_placeholders, num_subentries, options, subentries}]}``.
    The FULL ``as_json_fragment`` field set (``created_at`` / ``modified_at`` as
    ``.timestamp()`` floats, ``supported_subentry_types`` as core emits it), so the
    component row carries the same fields the legacy REST row does — no field is
    dropped on the component path. Filtered by ``domain`` when
    given, or the single entry by ``entry_id``
    (``hass.config_entries.async_get_entry`` — an id that matches nothing,
    including an empty string, yields an empty list). Only a WHOLLY ABSENT
    ``entry_id`` key (``None``) selects list mode; an empty-string ``entry_id`` is
    a single-entry lookup for a nonexistent id, so it returns ``entries: []``
    (mirroring ``async_get_entry("")``) rather than falling through to list mode
    and returning the first entry. ``state`` is serialized as
    ``ConfigEntryState.value`` (mirroring
    how core's ``config_entries/get`` emits it via ``as_json_fragment``, whose
    ``json_repr`` in ``homeassistant/config_entries.py`` is this row's source
    of truth for every field above except ``options``/``subentries``, which
    this integration re-derives since ``as_json_fragment`` never carries
    credential data).

    Data minimization: ``entry.data`` (integration credentials) is NEVER read.
    ``options`` is the only credential-bearing surface emitted, so it is passed
    through a BEST-EFFORT resolved-``!secret`` scrub — an options leaf (dict value,
    list item, or scalar whose ``str()`` form) that exactly equals a ``secrets.yaml``
    value becomes ``"**redacted**"`` (``secret_values`` loaded off the loop by
    :func:`_config_entries_prep`). ``subentries`` carries identity fields only
    (``subentry_id`` / ``subentry_type`` / ``title`` / ``unique_id``) — never a
    subentry's ``data``; a core version without subentries degrades to ``[]``.

    The scrub is BEST-EFFORT: a present-but-unreadable ``secrets.yaml`` degrades it
    to a no-op (options emitted unredacted). That degradation is signalled to the
    caller as ``secret_scrub_degraded: true`` (present ONLY when degraded), so an
    agent echoing ``options`` onward can tell an unscrubbed response from a clean
    one rather than trusting redaction that did not run.

    Pure over ``hass``: the blocking ``secrets.yaml`` read is offloaded by the
    async prep, so this stays a synchronous in-memory read.
    """
    entry_id = params.get("entry_id")
    domain = params.get("domain")
    if entry_id is not None:
        # Single-entry mode. An empty string is a valid (nonexistent) id — it
        # must NOT fall through to list mode, where a truthiness check would
        # return the first entry for a bogus id.
        entry = _config_entry_by_id(hass, entry_id)
        entries: list[Any] = [entry] if entry is not None else []
    else:
        entries = _iter_config_entries(hass)
        if domain:
            entries = [e for e in entries if getattr(e, "domain", None) == domain]
    result: dict[str, Any] = {
        "entries": [_config_entry_row(e, secret_values) for e in entries]
    }
    if secret_scrub_degraded:
        result["secret_scrub_degraded"] = True
    return result


async def _config_entries_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Async pre-step for ``config_entries``: load the secret-scrub set off the loop.

    Unlike ``search`` (which skips the read for an entity-only query),
    ``config_entries`` ALWAYS emits ``options``, so the ``secrets.yaml`` read is
    unconditional. The blocking ``open()`` + ``yaml.safe_load`` runs in the
    executor via :meth:`hass.async_add_executor_job`, keeping
    :func:`_do_config_entries` a pure in-memory read. The ``degraded`` flag (a
    present-but-unreadable ``secrets.yaml``) rides through so the response can signal
    that ``options`` may be unredacted. See :func:`_load_secret_scrub`.
    """
    values, degraded = await hass.async_add_executor_job(_load_secret_scrub, hass)
    return {"secret_values": values, "secret_scrub_degraded": degraded}


def _config_entry_by_id(hass: HomeAssistant, entry_id: str) -> Any:
    """``hass.config_entries.async_get_entry(entry_id)`` guarded (``None`` if absent)."""
    config_entries = getattr(hass, "config_entries", None)
    getter = (
        getattr(config_entries, "async_get_entry", None)
        if config_entries is not None
        else None
    )
    if getter is None:
        return None
    try:
        return getter(entry_id)
    except Exception:  # pragma: no cover - defensive
        return None


def _config_entry_row(entry: Any, secret_values: frozenset[str]) -> dict[str, Any]:
    """One config entry as the ``config_entries/get`` row (options scrubbed)."""
    raw_options = getattr(entry, "options", None)
    options = _plainify(dict(raw_options)) if isinstance(raw_options, Mapping) else {}
    options = _scrub_secret_values(options, secret_values)
    return {
        # Timestamps as floats via ``.timestamp()``, mirroring core's
        # as_json_fragment (``self.created_at.timestamp()``). Absent on a core old
        # enough to predate them -> None (this row is read-only, never restored, so a
        # None key is harmless here — unlike the area-registry rows).
        "created_at": _timestamp(getattr(entry, "created_at", None)),
        "modified_at": _timestamp(getattr(entry, "modified_at", None)),
        "entry_id": getattr(entry, "entry_id", None),
        "domain": getattr(entry, "domain", None),
        "title": getattr(entry, "title", None),
        "state": _enum_value(getattr(entry, "state", None)),
        "source": getattr(entry, "source", None),
        # supports_* are computed properties (they touch the flow handler) — read
        # through _safe_prop so a domain whose handler is unavailable degrades
        # instead of raising. ``or False`` mirrors core's as_json_fragment, which
        # coerces the optional bools to False.
        "supports_options": bool(_safe_prop(entry, "supports_options")),
        "supports_remove_device": _safe_prop(entry, "supports_remove_device") or False,
        "supports_unload": _safe_prop(entry, "supports_unload") or False,
        "supports_reconfigure": bool(_safe_prop(entry, "supports_reconfigure")),
        # supported_subentry_types is a computed property (it touches the flow
        # handler, same hazard class as supports_*) — _safe_prop-guarded, defaulting
        # to {} like core's ``self._supported_subentry_types or {}``.
        "supported_subentry_types": _safe_prop(entry, "supported_subentry_types", {})
        or {},
        "pref_disable_new_entities": bool(
            getattr(entry, "pref_disable_new_entities", False)
        ),
        "pref_disable_polling": bool(getattr(entry, "pref_disable_polling", False)),
        "disabled_by": _enum_value(getattr(entry, "disabled_by", None)),
        "reason": getattr(entry, "reason", None),
        "error_reason_translation_key": getattr(
            entry, "error_reason_translation_key", None
        ),
        "error_reason_translation_placeholders": getattr(
            entry, "error_reason_translation_placeholders", None
        ),
        "num_subentries": len(_mapping_values(getattr(entry, "subentries", None))),
        "options": options,
        "subentries": _config_subentries(entry),
    }


def _config_subentries(entry: Any) -> list[dict[str, Any]]:
    """Identity fields of each config subentry — NEVER the subentry ``data``.

    ``entry.subentries`` is a ``MappingProxyType`` keyed by subentry_id in modern
    core; a version without it (``getattr`` -> ``None``) degrades to ``[]``.
    """
    return [
        {
            "subentry_id": getattr(sub, "subentry_id", None),
            "subentry_type": getattr(sub, "subentry_type", None),
            "title": getattr(sub, "title", None),
            "unique_id": getattr(sub, "unique_id", None),
        }
        for sub in _mapping_values(getattr(entry, "subentries", None))
    ]


def _scrub_secret_values(value: Any, secret_values: frozenset[str]) -> Any:
    """Recursively replace any leaf equal to a known secret with ``"**redacted**"``.

    Walks ``Mapping`` values, list/tuple items, and scalar leaves of the (already
    ``_plainify``'d) options structure. A scalar leaf whose plaintext form
    (``str(leaf)``) exactly equals a ``secrets.yaml`` value is redacted, so a
    resolved ``!secret`` never leaves the component whether an integration persists
    it as a string OR as the original scalar (an int ``alarm_code`` leaves as an int
    leaf). ``bool`` leaves are never secrets and pass through unchanged (their
    ``"True"``/``"False"`` form would over-redact). An empty ``secret_values`` is a
    no-op (fast path). Handles ``Mapping``/``tuple`` directly so it is correct even
    if applied to a structure that skipped :func:`_plainify`.
    """
    if not secret_values:
        return value
    if isinstance(value, Mapping):
        return {k: _scrub_secret_values(v, secret_values) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_secret_values(v, secret_values) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)) and str(value) in secret_values:
        return "**redacted**"
    return value


def _safe_prop(obj: Any, name: str, default: Any = None) -> Any:
    """Read a possibly-computed property, degrading to ``default`` if it raises.

    ``getattr`` alone only defaults on ``AttributeError``; a ConfigEntry's
    ``supports_options`` / ``supports_reconfigure`` are computed off the flow
    handler and can raise other errors when it is unavailable, so catch broadly.
    """
    try:
        return getattr(obj, name, default)
    except Exception:  # pragma: no cover - defensive; core drift
        return default


# =============================================================================
# ha_mcp_tools/registry_lookup
# =============================================================================
def _do_registry_lookup(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return entity-registry rows for a set of ids or one config entry.

    Rows are core's ``RegistryEntry.as_partial_dict`` VERBATIM (the
    ``config/entity_registry/list`` shape, disabled entities included), so the
    consumers that parse that exact shape today read the component-served rows
    unchanged.

    * ``config_entry_id`` — scans the whole entity registry and returns EVERY
      entity bound to that entry (``{entities: [...]}``). It deliberately does
      NOT reuse :func:`_entities_by_config_entry` (single-valued — first entity
      only), so a multi-entity flow helper (a utility_meter and its tariff
      sub-entities) does not silently lose members.
    * ``entity_ids`` — looks each up (``{entities: [...], missing: [...]}``); an
      id with no registry entry lands in ``missing`` rather than being dropped.

    Pure O(n)/O(id) in-memory registry reads. Exactly one of the two params is
    meaningful — the schema rejects both being present; neither present (or only an
    empty-string ``config_entry_id`` / empty ``entity_ids`` list — no usable target)
    raises ``HomeAssistantError`` (see :func:`_registry_lookup_missing_target`)
    rather than silently returning an empty result the caller could mistake for "no
    matches".
    """
    config_entry_id = params.get("config_entry_id")
    entity_ids = params.get("entity_ids") or []
    if not config_entry_id and not entity_ids:
        raise _registry_lookup_missing_target()

    view = _resolve_registries(hass)
    if config_entry_id:
        rows = [
            _entity_partial_dict(entry)
            for entry in _all_entity_entries(view)
            if getattr(entry, "config_entry_id", None) == config_entry_id
        ]
        return {"entities": [row for row in rows if row is not None]}

    found: list[dict[str, Any]] = []
    missing: list[str] = []
    for entity_id in entity_ids:
        entry = _reg_entity(view, entity_id)
        row = _entity_partial_dict(entry) if entry is not None else None
        if row is None:
            missing.append(entity_id)
        else:
            found.append(row)
    return {"entities": found, "missing": missing}


def _registry_lookup_missing_target() -> Exception:
    """Build a ``HomeAssistantError`` for a target-less ``registry_lookup`` request.

    Mirrors :func:`_backup_unavailable`: imported function-locally (test-stubbable)
    so a request with no usable target — neither ``entity_ids`` nor
    ``config_entry_id``, OR only an empty-string / empty-list one — raises instead of
    returning ``{entities: [], missing: []}``, a shape indistinguishable from a
    genuine "nothing matched" result, letting the server's command-error fallback
    fire for the degenerate case. (This is a deliberate divergence from
    ``config_entries``, whose empty-string ``entry_id`` is an authoritative empty
    result mirroring ``async_get_entry("")`` — a different, intentional semantic.)
    """
    from homeassistant.exceptions import HomeAssistantError

    err: Exception = HomeAssistantError(
        "ha_mcp_tools/registry_lookup requires a non-empty entity_ids or "
        "config_entry_id"
    )
    return err


# =============================================================================
# ha_mcp_tools/system_snapshot
# =============================================================================
def _do_system_snapshot(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """One consistent synchronous pass over the live objects the health path reads.

    ``{config_entries: [...], issues: [...], entities: [...], states: [...]}`` —
    every section read from the live in-memory objects in a SINGLE synchronous
    handler call, which is the point: it collapses the server's former 3x
    ``config_entries/get`` fetches (a TOCTOU where entries changed mid-read) into
    one coherent snapshot. ``include_*`` flags gate each section; a disabled
    section is an empty list (present, so the consumer need not special-case a
    missing key).

    * ``config_entries`` — identity fields only (``entry_id`` / ``domain`` /
      ``title`` / ``state`` / ``source`` / ``disabled_by``); the health view does
      not need ``options`` / ``subentries`` (and skipping them avoids the secret
      scrub here).
    * ``issues`` — the ``_overview_repairs`` slice, reused verbatim.
    * ``entities`` — the ``registry_lookup`` row shape (``as_partial_dict``).
    * ``states`` — ``State.as_dict()`` per state (REST-parity bodies).
    """
    view = _resolve_registries(hass)
    result: dict[str, Any] = {}
    result["config_entries"] = (
        [_config_entry_identity_row(e) for e in _iter_config_entries(hass)]
        if params.get("include_config_entries", True)
        else []
    )
    result["issues"] = (
        _overview_repairs(hass) if params.get("include_issues", True) else []
    )
    result["entities"] = (
        [
            row
            for row in (_entity_partial_dict(e) for e in _all_entity_entries(view))
            if row is not None
        ]
        if params.get("include_entities", True)
        else []
    )
    result["states"] = (
        _snapshot_states(hass) if params.get("include_states", True) else []
    )
    return result


def _config_entry_identity_row(entry: Any) -> dict[str, Any]:
    """Identity-only config-entry row for the health snapshot (no options/subentries)."""
    return {
        "entry_id": getattr(entry, "entry_id", None),
        "domain": getattr(entry, "domain", None),
        "title": getattr(entry, "title", None),
        "state": _enum_value(getattr(entry, "state", None)),
        "source": getattr(entry, "source", None),
        "disabled_by": _enum_value(getattr(entry, "disabled_by", None)),
    }


def _snapshot_states(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every live state as ``State.as_dict()`` (REST-parity body, unmodified)."""
    out: list[dict[str, Any]] = []
    for state in _iter_states(hass):
        if not getattr(state, "entity_id", None):
            continue
        as_dict = _state_as_dict(state)
        if as_dict is not None:
            out.append(as_dict)
    return out


# =============================================================================
# ha_mcp_tools/entity_lookup
# =============================================================================
def _do_entity_lookup(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return registry entries whose ``unique_id`` matches (domain/platform narrow).

    ``{matches: [{entity_id, unique_id, platform, domain, config_entry_id,
    categories, disabled_by, hidden_by}]}``. Scans the entity registry for every
    entry whose ``unique_id`` equals the requested one, optionally narrowed by
    ``domain`` (the entity's own domain, from its entity_id) and ``platform``
    (the owning integration). Multiple matches across platforms are all returned
    — the server picks. ``categories`` mirrors ``as_partial_dict``'s
    ``dict(entry.categories)``; ``disabled_by`` / ``hidden_by`` are unwrapped to
    their wire strings. In-process, so the read is authoritative immediately (no
    registry-write settle retry).

    A drifted entity registry (``er.async_get`` raised / renamed → ``None``) RAISES
    ``HomeAssistantError`` (→ server command-error fallback to the legacy scan)
    rather than returning ``{matches: []}`` — a well-formed empty the server can't
    tell from a genuine "no entry with that unique_id". A present-but-empty registry
    still returns ``{matches: []}`` (correct: no match).
    """
    unique_id = params.get("unique_id")
    domain = params.get("domain")
    platform = params.get("platform")
    view = _resolve_registries(hass)
    if view.entity is None:
        raise _substrate_unavailable("entity registry")
    matches: list[dict[str, Any]] = []
    for entry in _all_entity_entries(view):
        if getattr(entry, "unique_id", None) != unique_id:
            continue
        entity_id = getattr(entry, "entity_id", "") or ""
        ent_domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain and ent_domain != domain:
            continue
        if platform and getattr(entry, "platform", None) != platform:
            continue
        matches.append(
            {
                "entity_id": entity_id,
                "unique_id": getattr(entry, "unique_id", None),
                "platform": getattr(entry, "platform", None),
                "domain": ent_domain,
                "config_entry_id": getattr(entry, "config_entry_id", None),
                "categories": _plainify(getattr(entry, "categories", None) or {}),
                "disabled_by": _enum_value(getattr(entry, "disabled_by", None)),
                "hidden_by": _enum_value(getattr(entry, "hidden_by", None)),
            }
        )
    return {"matches": matches}


# =============================================================================
# ha_mcp_tools/backup_prep
# =============================================================================
def _do_backup_prep(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return the backup identity the server needs before a create.

    ``{agent_ids: [...], local_agent_id: <str|None>, default_password:
    <str|None>}`` read from the backup integration's in-process manager
    (``hass.data[DATA_MANAGER]``). ``local_agent_id`` uses the SAME preference the
    server's ``_get_local_backup_agent_id`` does — an agent whose ``name`` is
    ``"local"``, preferring ``hassio.local`` (Supervised) over ``backup.local``
    (Core). ``default_password`` is getattr-chained off
    ``manager.config.data.create_backup.password``.

    A missing backup integration (``ImportError``) or an uninitialized manager
    raises ``HomeAssistantError`` so the server's command-error path falls back to
    its legacy WS reads — NOT a silent empty result the server could mistake for
    "no agents". The password is sensitive, but the legacy ``backup/config/info``
    already serves it to the same admin connection (parity, not new exposure).

    STRUCTURAL core drift raises for the same reason, rather than degrading to a
    well-formed authoritative negative: a non-Mapping ``manager.backup_agents``
    (below) or a broken config→data→create_backup chain
    (:func:`_backup_default_password`) would otherwise produce a "no agents" / "no
    password" answer the server trusts and hard-fails on (or, for the password,
    silently drops the restore safety backup) with no fallback. Value-level reads
    (an id string, a genuinely-``None`` password on an intact chain) stay
    getattr-guarded and pass through.
    """
    try:
        from homeassistant.components.backup import DATA_MANAGER
    except ImportError as exc:
        raise _backup_unavailable("backup integration is not available") from exc
    manager = _hass_data_get(hass, DATA_MANAGER)
    if manager is None:
        raise _backup_unavailable("backup manager is not initialized")
    agents = getattr(manager, "backup_agents", None)
    if not isinstance(agents, Mapping):
        raise _backup_unavailable("backup manager exposes no agent mapping")
    return {
        "agent_ids": [str(a) for a in agents],
        "local_agent_id": _preferred_local_agent_id(agents),
        "default_password": _backup_default_password(manager),
    }


def _backup_unavailable(message: str) -> Exception:
    """Build a ``HomeAssistantError`` (imported function-locally — test-stubbable)."""
    from homeassistant.exceptions import HomeAssistantError

    err: Exception = HomeAssistantError(message)
    return err


def _hass_data_get(hass: HomeAssistant, key: Any) -> Any:
    """``hass.data.get(key)`` guarded against a non-mapping / drift."""
    data = getattr(hass, "data", None)
    if not isinstance(data, Mapping):
        return None
    try:
        return data.get(key)
    except Exception:  # pragma: no cover - defensive
        return None


def _preferred_local_agent_id(agents: Any) -> str | None:
    """The local backup agent id, mirroring the server's hassio-over-core preference.

    Collects agent ids whose agent ``name`` is exactly ``"local"``
    (``hassio.local`` on Supervised, ``backup.local`` on Core both use that
    name), prefers ``hassio.local`` then ``backup.local``, else the first local
    agent, else ``None``.
    """
    if not isinstance(agents, Mapping):
        return None
    local_ids: list[str] = [
        str(agent_id)
        for agent_id, agent in agents.items()
        if getattr(agent, "name", None) == "local"
    ]
    for preferred in ("hassio.local", "backup.local"):
        if preferred in local_ids:
            return preferred
    return local_ids[0] if local_ids else None


def _backup_default_password(manager: Any) -> str | None:
    """``manager.config.data.create_backup.password`` (getattr-chained; ``str``/None).

    A STRUCTURALLY broken chain (a missing ``config`` / ``data`` / ``create_backup``
    link — core drift) RAISES ``HomeAssistantError`` so the server falls back to its
    legacy ``backup/config/info`` read, rather than returning ``None`` the server
    reads as "no default password configured" — which on restore silently drops the
    safety backup while telling the user the password is unset. Only a genuine
    ``None`` ``password`` on an INTACT chain is the authoritative "not configured".
    """
    config = getattr(manager, "config", None)
    data = getattr(config, "data", None)
    create_backup = getattr(data, "create_backup", None)
    if config is None or data is None or create_backup is None:
        raise _backup_unavailable("backup manager config chain is unavailable")
    password = getattr(create_backup, "password", None)
    return password if isinstance(password, str) else None


# =============================================================================
# ha_mcp_tools/registries
# =============================================================================
def _do_registries(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return the requested registries as the FULL-FIELD ``config/<x>_registry/list`` shapes.

    ``{areas: [...], floors: [...], labels: [...], categories: {scope: [...]}}`` —
    only the requested ``registries`` keys are present. Each row is byte-compatible
    with the legacy WS list response the consumers parse (verified against core's
    registry serializers): area = aliases / area_id / floor_id / icon / labels /
    name / picture / created_at / modified_at, plus humidity_entity_id /
    temperature_entity_id ONLY on core >= 2024.12 (emitted conditionally so an older
    core's restore does not get None-valued keys it rejects — see
    :func:`_area_row`); floor = aliases / created_at / floor_id / icon /
    level / name / modified_at (floor rows carry NO ``labels`` — core omits it);
    label = color / created_at / description / icon / label_id / name /
    modified_at; category = category_id / created_at / icon / modified_at / name.
    Timestamps are floats (``.timestamp()``), matching core.

    ``category`` is scoped: ``category_scopes`` names which scopes to list (a
    ``{scope: [rows]}`` map) and is REQUIRED when ``category`` is requested — a
    scope-less category request raises ``HomeAssistantError`` (see
    :func:`_registries_missing_category_scopes`) rather than silently serving
    ``{}``, a shape indistinguishable from "every requested scope is empty". The
    category registry is imported function-locally (not needed at module top).
    Pure in-memory reads over the resolved registries.
    """
    requested = params.get("registries") or []
    category_scopes = params.get("category_scopes") or []
    if "category" in requested and not category_scopes:
        raise _registries_missing_category_scopes()

    view = _resolve_registries(hass)
    result: dict[str, Any] = {}
    # A requested registry whose accessor drifted (raised / renamed → ``None`` via
    # ``_safe``) RAISES → server command-error fallback to the legacy WS list,
    # rather than serving a well-formed empty list the capture pipeline would read
    # as "this entity does not exist" and silently skip. A present-but-empty
    # registry serves its empty list (correct).
    if "area" in requested:
        if view.area is None:
            raise _substrate_unavailable("area registry")
        result["areas"] = [_area_row(a) for a in _all_area_entries(view)]
    if "floor" in requested:
        if view.floor is None:
            raise _substrate_unavailable("floor registry")
        result["floors"] = [_floor_row(f) for f in _all_floor_entries(view)]
    if "label" in requested:
        if view.label is None:
            raise _substrate_unavailable("label registry")
        result["labels"] = [_label_row(x) for x in _all_label_entries(view)]
    if "category" in requested:
        result["categories"] = _category_rows(hass, category_scopes)
    return result


def _registries_missing_category_scopes() -> Exception:
    """Build a ``HomeAssistantError`` for a scope-less ``category`` request.

    Mirrors :func:`_backup_unavailable`: imported function-locally (test-stubbable)
    so a ``registries`` request naming ``category`` without a non-empty
    ``category_scopes`` raises instead of silently returning ``{}`` (a caller
    could otherwise mistake that for "no categories in any scope").
    """
    from homeassistant.exceptions import HomeAssistantError

    err: Exception = HomeAssistantError(
        "ha_mcp_tools/registries: category_scopes is required when "
        "'category' is requested"
    )
    return err


def _area_row(area: Any) -> dict[str, Any]:
    """One area as core's ``AreaEntry.json_fragment`` shape (id renamed to area_id)."""
    row: dict[str, Any] = {
        "aliases": sorted(str(a) for a in (getattr(area, "aliases", None) or [])),
        "area_id": getattr(area, "id", None) or getattr(area, "area_id", None),
        "floor_id": getattr(area, "floor_id", None),
        "icon": getattr(area, "icon", None),
        "labels": sorted(str(x) for x in (getattr(area, "labels", None) or [])),
        "name": getattr(area, "name", None),
        "picture": getattr(area, "picture", None),
        "created_at": _timestamp(getattr(area, "created_at", None)),
        "modified_at": _timestamp(getattr(area, "modified_at", None)),
    }
    # humidity_entity_id / temperature_entity_id were added to AreaEntry in core
    # 2024.12. Emit each ONLY when the running core's AreaEntry actually has the
    # attribute — a pre-2024.12 core would otherwise get a None-valued key injected
    # here that the restore path's config/area_registry/update schema rejects
    # ("extra keys not allowed"). ``hasattr`` (not ``getattr(..., None)``)
    # distinguishes "core has the field, value is None" from "core has no field".
    for attr in ("humidity_entity_id", "temperature_entity_id"):
        if hasattr(area, attr):
            row[attr] = getattr(area, attr, None)
    return row


def _floor_row(floor: Any) -> dict[str, Any]:
    """One floor as core's ``config/floor_registry/list`` shape (NO ``labels`` field)."""
    return {
        "aliases": sorted(str(a) for a in (getattr(floor, "aliases", None) or [])),
        "created_at": _timestamp(getattr(floor, "created_at", None)),
        "floor_id": getattr(floor, "floor_id", None),
        "icon": getattr(floor, "icon", None),
        "level": getattr(floor, "level", None),
        "name": getattr(floor, "name", None),
        "modified_at": _timestamp(getattr(floor, "modified_at", None)),
    }


def _label_row(label: Any) -> dict[str, Any]:
    """One label as core's ``config/label_registry/list`` shape."""
    return {
        "color": getattr(label, "color", None),
        "created_at": _timestamp(getattr(label, "created_at", None)),
        "description": getattr(label, "description", None),
        "icon": getattr(label, "icon", None),
        "label_id": getattr(label, "label_id", None),
        "name": getattr(label, "name", None),
        "modified_at": _timestamp(getattr(label, "modified_at", None)),
    }


def _category_rows(hass: HomeAssistant, scopes: list[str]) -> dict[str, Any]:
    """``{scope: [category rows]}`` for each requested scope (categories are scoped).

    A drifted / absent category registry (``None`` — ``cr.async_get`` raised /
    renamed, or the module is missing on an old core) RAISES rather than serving
    ``{scope: []}`` for every scope, so the server falls back to the legacy
    ``config/category_registry/list`` instead of trusting an empty map.
    """
    registry = _category_registry(hass)
    if registry is None:
        raise _substrate_unavailable("category registry")
    return {
        scope: [_category_row(c) for c in _list_categories(registry, scope)]
        for scope in scopes
    }


def _category_row(category: Any) -> dict[str, Any]:
    """One category as core's ``config/category_registry/list`` shape."""
    return {
        "category_id": getattr(category, "category_id", None),
        "created_at": _timestamp(getattr(category, "created_at", None)),
        "icon": getattr(category, "icon", None),
        "modified_at": _timestamp(getattr(category, "modified_at", None)),
        "name": getattr(category, "name", None),
    }


def _category_registry(hass: HomeAssistant) -> Any:
    """The category registry (imported function-locally). Test seam. ``None`` on drift."""
    try:
        from homeassistant.helpers import category_registry as cr
    except ImportError:  # pragma: no cover - defensive; core drift
        return None
    return _safe(cr.async_get, hass)


def _list_categories(registry: Any, scope: str) -> list[Any]:
    """``registry.async_list_categories(scope=...)`` guarded (scope is keyword-only)."""
    if registry is None:
        return []
    lister = getattr(registry, "async_list_categories", None)
    if not callable(lister):
        return []
    try:
        return list(lister(scope=scope))
    except Exception:  # pragma: no cover - defensive
        return []


def _all_floor_entries(view: _RegistryView) -> list[Any]:
    """All floor-registry entries via ``async_list_floors()`` or the ``floors`` mapping."""
    reg = view.floor
    if reg is None:
        return []
    listed = _call_no_arg(reg, "async_list_floors")
    if listed is not None:
        try:
            return list(listed)
        except Exception:  # pragma: no cover - defensive
            return []
    return _mapping_values(getattr(reg, "floors", None))


def _all_label_entries(view: _RegistryView) -> list[Any]:
    """All label-registry entries via ``async_list_labels()`` or the ``labels`` mapping."""
    reg = view.label
    if reg is None:
        return []
    listed = _call_no_arg(reg, "async_list_labels")
    if listed is not None:
        try:
            return list(listed)
        except Exception:  # pragma: no cover - defensive
            return []
    return _mapping_values(getattr(reg, "labels", None))


# =============================================================================
# ha_mcp_tools/dashboards
# =============================================================================
# LovelaceConfig.mode returns these wire strings (MODE_STORAGE / MODE_YAML in
# core's lovelace const). Compared as strings so a MagicMock-stubbed core in the
# unit suite (no real constants) still exercises the branches.
_LOVELACE_MODE_STORAGE = "storage"
_LOVELACE_MODE_YAML = "yaml"

# Keys of a stored dashboard collection item (core's STORAGE_DASHBOARD_*_FIELDS),
# echoed by ``lovelace/dashboards/list`` — the row shape ``list`` mode mirrors.
_DASHBOARD_ROW_KEYS = (
    "id",
    "url_path",
    "title",
    "icon",
    "show_in_sidebar",
    "require_admin",
)

# Cap on ``search``-mode matches per call so one WS frame stays bounded.
_DASHBOARD_MATCH_CAP = 200

# Structural keys walked as containers (not scored as leaf strings) in a card.
_DASHBOARD_STRUCTURAL_KEYS = frozenset({"cards", "sections"})


def _do_dashboards(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    prepped: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return Lovelace dashboards read in-process (``list`` / ``get`` / ``search``).

    Pure assembler over the plain dicts :func:`_dashboards_prep` loads off the
    event loop — every Store load (``async_load``) happens in the prep, so this
    function only shapes / walks already-materialized config. ``available`` is
    ``False`` when the lovelace integration is not set up (no ``LOVELACE_DATA``);
    the server falls back to its legacy ``lovelace/*`` path in that case.

    YAML-mode dashboard bodies are NEVER emitted (``get`` returns a ``yaml_excluded``
    status; ``search`` skips them) — their config may carry resolved ``!secret``
    plaintext, so body emission for YAML belongs to a future file-based tool.
    """
    mode = params.get("mode", "list")
    prepped = prepped or {}
    if not prepped.get("available"):
        result: dict[str, Any] = {"mode": mode, "available": False}
        if mode == "list":
            result["dashboards"] = []
        elif mode == "search":
            result["matches"] = []
            result["truncated"] = False
        return result

    if mode == "get":
        return {
            "mode": "get",
            "available": True,
            "status": prepped.get("status"),
            "url_path": prepped.get("url_path"),
            "config": prepped.get("config"),
        }
    if mode == "search":
        query_lower = (params.get("query") or "").strip().lower()
        matches, truncated = _search_dashboard_docs(
            prepped.get("docs") or [], query_lower
        )
        return {
            "mode": "search",
            "available": True,
            "matches": matches,
            "truncated": truncated,
        }
    return {"mode": "list", "available": True, "dashboards": prepped.get("rows") or []}


async def _dashboards_prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Async pre-step for ``dashboards``: do ALL the Store loading off the loop.

    Reaching ``hass.data[LOVELACE_DATA].dashboards`` is a pure in-memory read, but
    loading a dashboard's config (``LovelaceConfig.async_load``) awaits a Store
    read, so every mode's loading lives here and :func:`_do_dashboards` gets plain
    dicts. Returns ``{"prepped": {...}}`` with ``available=False`` when lovelace is
    not set up (the server falls back to legacy).
    """
    mode = msg.get("mode", "list")
    dashboards_map = _lovelace_dashboards_map(hass)
    if dashboards_map is None:
        return {"prepped": {"available": False}}

    prepped: dict[str, Any] = {"available": True}
    if mode == "get":
        prepped.update(await _dashboard_get_config(dashboards_map, msg.get("url_path")))
    elif mode == "search":
        prepped["docs"] = await _dashboard_search_docs(dashboards_map)
    else:
        prepped["rows"] = _dashboard_list_rows(dashboards_map)
    return {"prepped": prepped}


def _lovelace_dashboards_map(hass: HomeAssistant) -> Mapping[Any, Any] | None:
    """The ``{url_path|None: LovelaceConfig}`` map, or ``None`` if lovelace is absent.

    Reads ``hass.data[LOVELACE_DATA].dashboards`` via a function-local import of
    core's key (older cores keyed ``hass.data["lovelace"]``, so both are tried),
    guarded so a missing key / core drift degrades to ``None`` (the server keeps
    its legacy path) rather than raising.
    """
    try:
        from homeassistant.components.lovelace import LOVELACE_DATA

        key: Any = LOVELACE_DATA
    except Exception:  # pragma: no cover - defensive; core drift / older core
        key = "lovelace"
    data = getattr(hass, "data", None)
    if not isinstance(data, Mapping):
        return None
    container = data.get(key)
    if container is None and key != "lovelace":
        container = data.get("lovelace")
    if container is None:
        return None
    dashboards = getattr(container, "dashboards", None)
    if dashboards is None and isinstance(container, Mapping):
        dashboards = container.get("dashboards")
    return dashboards if isinstance(dashboards, Mapping) else None


def _dashboard_list_rows(dashboards_map: Mapping[Any, Any]) -> list[dict[str, Any]]:
    """One metadata row per non-default dashboard, tagged with ``mode``.

    Mirrors the ``lovelace/dashboards/list`` row shape (the stored collection item
    for storage dashboards, read from ``LovelaceConfig.config``) plus an additive
    ``mode`` so the server can exclude YAML dashboards. The default dashboard
    (``url_path`` key ``None``) is omitted — the legacy list omits it too and the
    server special-cases the built-in dashboard as always-existing; ``get`` mode
    resolves ``None`` to that default instead.
    """
    rows: list[dict[str, Any]] = []
    for url_path, dash in dashboards_map.items():
        if url_path is None:
            continue
        config = getattr(dash, "config", None)
        meta = config if isinstance(config, Mapping) else {}
        row: dict[str, Any] = {key: meta.get(key) for key in _DASHBOARD_ROW_KEYS}
        # The dict key is the authoritative url_path (the metadata may lack it).
        row["url_path"] = url_path
        row["mode"] = _dashboard_mode(dash)
        rows.append(row)
    return rows


async def _dashboard_get_config(
    dashboards_map: Mapping[Any, Any], url_path: Any
) -> dict[str, Any]:
    """Load one dashboard's config body; ``status`` names the outcome.

    ``url_path`` ``None``/absent resolves to the default dashboard. A YAML-mode
    dashboard returns ``status="yaml_excluded"`` with no body (its config may
    carry resolved ``!secret`` plaintext — storage-only emission). A missing
    dashboard or a load error returns ``status="not_found"``. Storage freshness is
    safe: ``LovelaceStorage.async_save`` mutates the in-memory object
    synchronously, so this read never lags a save (audit-verified).
    """
    dash = dashboards_map.get(url_path)
    resolved = getattr(dash, "url_path", None) if dash is not None else url_path
    if dash is None:
        return {"status": "not_found", "url_path": url_path, "config": None}
    if _dashboard_mode(dash) == _LOVELACE_MODE_YAML:
        return {"status": "yaml_excluded", "url_path": resolved, "config": None}
    loader = getattr(dash, "async_load", None)
    if not callable(loader):
        return {"status": "not_found", "url_path": resolved, "config": None}
    try:
        config = await loader(False)
    except Exception:  # any load failure degrades to not_found (fail-soft)
        return {"status": "not_found", "url_path": resolved, "config": None}
    if not isinstance(config, dict):
        return {"status": "not_found", "url_path": resolved, "config": None}
    return {"status": "ok", "url_path": resolved, "config": _plainify(config)}


async def _dashboard_search_docs(
    dashboards_map: Mapping[Any, Any],
) -> list[dict[str, Any]]:
    """Load every STORAGE dashboard's config for the ``search`` walk.

    Only storage dashboards are loaded — YAML bodies are never searched/emitted.
    A per-dashboard load error is skipped (fail-soft) rather than failing the
    whole search. Returns ``[{url_path, title, config}, ...]`` plain dicts.
    """
    docs: list[dict[str, Any]] = []
    for url_path, dash in dashboards_map.items():
        if _dashboard_mode(dash) != _LOVELACE_MODE_STORAGE:
            continue
        loader = getattr(dash, "async_load", None)
        if not callable(loader):
            continue
        try:
            config = await loader(False)
        except Exception:  # skip an unreadable dashboard, keep going (fail-soft)
            continue
        if not isinstance(config, dict):
            continue
        title = config.get("title")
        docs.append(
            {
                "url_path": url_path,
                "title": str(title) if title is not None else None,
                "config": config,
            }
        )
    return docs


def _dashboard_mode(dash: Any) -> str | None:
    """A dashboard's ``mode`` (``storage``/``yaml``), guarded against core drift."""
    mode = getattr(dash, "mode", None)
    return str(mode) if isinstance(mode, str) else None


def _search_dashboard_docs(
    docs: list[dict[str, Any]], query_lower: str
) -> tuple[list[dict[str, Any]], bool]:
    """Walk each dashboard config for ``query_lower``; return ``(matches, truncated)``.

    An empty query matches nothing (a bare substring would match every string).
    Matches are capped at :data:`_DASHBOARD_MATCH_CAP` with a ``truncated`` flag.
    """
    if not query_lower:
        return [], False
    matches: list[dict[str, Any]] = []
    for doc in docs:
        _collect_dashboard_matches(doc, query_lower, matches)
    truncated = len(matches) > _DASHBOARD_MATCH_CAP
    return matches[:_DASHBOARD_MATCH_CAP], truncated


def _collect_dashboard_matches(
    doc: dict[str, Any], query_lower: str, matches: list[dict[str, Any]]
) -> None:
    """Append every ``query_lower`` hit in one dashboard config to ``matches``.

    Walks each view's card containers (``cards`` + sections-view ``sections.cards``,
    nested cards recursed), plus the two view-level containers the card walk never
    visits: ``badges`` and a sections-view ``header.card``. This matches what the
    single-dashboard (MODE 2) search covers, so a query answered "no match" here is
    a real absence, not a blind spot for entities referenced only as a badge or in a
    header card.
    """
    config = doc.get("config")
    if not isinstance(config, dict):
        return
    views = config.get("views")
    if not isinstance(views, list):
        return
    url_path = doc.get("url_path")
    dash_title = doc.get("title")
    for view_index, view in enumerate(views):
        if not isinstance(view, dict):
            continue
        view_title = view.get("title")
        for cards, base_path in _view_card_containers(view, view_index):
            _collect_card_matches(
                cards,
                base_path,
                url_path,
                dash_title,
                view_index,
                view_title,
                query_lower,
                matches,
            )
        _collect_badge_matches(
            view, view_index, url_path, dash_title, view_title, query_lower, matches
        )
        _collect_header_card_matches(
            view, view_index, url_path, dash_title, view_title, query_lower, matches
        )


def _view_card_containers(
    view: dict[str, Any], view_index: int
) -> list[tuple[Any, str]]:
    """The card lists in a view: top-level ``cards`` plus each section's ``cards``."""
    containers: list[tuple[Any, str]] = []
    if isinstance(view.get("cards"), list):
        containers.append((view["cards"], f"views[{view_index}].cards"))
    sections = view.get("sections")
    if isinstance(sections, list):
        for si, section in enumerate(sections):
            if isinstance(section, dict) and isinstance(section.get("cards"), list):
                containers.append(
                    (section["cards"], f"views[{view_index}].sections[{si}].cards")
                )
    return containers


def _dashboard_match(
    url_path: Any,
    dash_title: Any,
    view_index: int,
    view_title: Any,
    card_path: str,
    card_type: Any,
    matched_field: str,
    matched_value: str,
) -> dict[str, Any]:
    """One MODE 4 cross-dashboard search match record (shared, fixed shape).

    Every match site — cards, badges, header cards — builds its record here so the
    wire shape stays identical (the server-side legacy walk mirrors it for parity).
    """
    return {
        "url_path": url_path,
        "title": dash_title,
        "view_index": view_index,
        "view_title": view_title,
        "card_path": card_path,
        "card_type": card_type,
        "matched_field": matched_field,
        "matched_value": matched_value,
    }


def _collect_card_matches(
    cards: Any,
    base_path: str,
    url_path: Any,
    dash_title: Any,
    view_index: int,
    view_title: Any,
    query_lower: str,
    matches: list[dict[str, Any]],
) -> None:
    """Recurse a card list, recording one match per string leaf containing the query.

    ``matched_field`` is the leaf's immediate key (``entity`` / ``entities`` /
    ``camera_image`` / any plain-string field); nested ``cards`` are walked as
    their own cards (their strings are attributed to the nested card, not the
    parent), so ``card_path`` / ``card_type`` always name the card the string
    actually lives on.
    """
    if not isinstance(cards, list):
        return
    for card_index, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        _collect_one_card_matches(
            card,
            f"{base_path}[{card_index}]",
            url_path,
            dash_title,
            view_index,
            view_title,
            query_lower,
            matches,
        )


def _collect_one_card_matches(
    card: dict[str, Any],
    card_path: str,
    url_path: Any,
    dash_title: Any,
    view_index: int,
    view_title: Any,
    query_lower: str,
    matches: list[dict[str, Any]],
) -> None:
    """Record matches for a SINGLE card at ``card_path`` and recurse its nested cards.

    Shared by :func:`_collect_card_matches` (list-indexed cards) and
    :func:`_collect_header_card_matches` (a header card is a single card, not
    list-indexed).
    """
    card_type = card.get("type")
    for field, value in _card_string_leaves(card):
        if query_lower in value.lower():
            matches.append(
                _dashboard_match(
                    url_path,
                    dash_title,
                    view_index,
                    view_title,
                    card_path,
                    card_type,
                    field,
                    value,
                )
            )
    nested = card.get("cards")
    if isinstance(nested, list):
        _collect_card_matches(
            nested,
            f"{card_path}.cards",
            url_path,
            dash_title,
            view_index,
            view_title,
            query_lower,
            matches,
        )


def _collect_badge_matches(
    view: dict[str, Any],
    view_index: int,
    url_path: Any,
    dash_title: Any,
    view_title: Any,
    query_lower: str,
    matches: list[dict[str, Any]],
) -> None:
    """Record query hits in a view's ``badges`` — entity refs the card walk misses.

    View-level badges are entity references by construction: a bare string
    (``sensor.x``) or a dict (``{type: entity, entity: sensor.x}``). A bare-string
    badge is recorded as a ``badges`` leaf; a dict badge's string leaves are walked
    like a card's. Mirrors the single-dashboard (MODE 2) badge coverage.
    """
    badges = view.get("badges")
    if not isinstance(badges, list):
        return
    for badge_index, badge in enumerate(badges):
        badge_path = f"views[{view_index}].badges[{badge_index}]"
        if isinstance(badge, str):
            if badge and query_lower in badge.lower():
                matches.append(
                    _dashboard_match(
                        url_path,
                        dash_title,
                        view_index,
                        view_title,
                        badge_path,
                        "badge",
                        "badges",
                        badge,
                    )
                )
        elif isinstance(badge, dict):
            badge_type = badge.get("type") or "badge"
            for field, value in _card_string_leaves(badge):
                if query_lower in value.lower():
                    matches.append(
                        _dashboard_match(
                            url_path,
                            dash_title,
                            view_index,
                            view_title,
                            badge_path,
                            badge_type,
                            field,
                            value,
                        )
                    )


def _collect_header_card_matches(
    view: dict[str, Any],
    view_index: int,
    url_path: Any,
    dash_title: Any,
    view_title: Any,
    query_lower: str,
    matches: list[dict[str, Any]],
) -> None:
    """Record query hits in a sections-view header card (``views[n].header.card``).

    The header accepts a card (typically Markdown) that can carry entity refs; the
    card walk never visits it. Mirrors the single-dashboard (MODE 2) header-card
    coverage.
    """
    header = view.get("header")
    if not isinstance(header, dict):
        return
    header_card = header.get("card")
    if not isinstance(header_card, dict):
        return
    _collect_one_card_matches(
        header_card,
        f"views[{view_index}].header.card",
        url_path,
        dash_title,
        view_index,
        view_title,
        query_lower,
        matches,
    )


def _card_string_leaves(card: dict[str, Any]) -> list[tuple[str, str]]:
    """``(immediate_key, string)`` for every string leaf of a card.

    Descends into nested dicts/lists but NOT the structural ``cards``/``sections``
    keys (those are walked as their own cards). The key attributed to a leaf is
    the nearest dict key, so ``entities: [{entity: light.a}]`` yields
    ``("entity", "light.a")`` and ``entities: [light.a]`` yields
    ``("entities", "light.a")`` — matching the brief's field taxonomy.
    """
    out: list[tuple[str, str]] = []
    _walk_card_leaves(card, "", out)
    return out


def _walk_card_leaves(value: Any, key: str, out: list[tuple[str, str]]) -> None:
    """Recursive worker for :func:`_card_string_leaves` (module-level for clarity).

    Descends dicts/lists collecting ``(nearest_key, string)`` leaves, skipping the
    structural ``cards``/``sections`` keys (walked as their own cards). A top-level
    card dict enters the ``dict`` branch, so its own keys attribute their leaves.
    """
    if isinstance(value, str):
        if value:
            out.append((key, value))
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in _DASHBOARD_STRUCTURAL_KEYS:
                _walk_card_leaves(v, str(k), out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_card_leaves(item, key, out)


# =============================================================================
# ha_mcp_tools/services_list
# =============================================================================
def _do_services_list(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    descriptions: Mapping[str, Any] | None = None,
    translations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reshape the service catalog to the REST ``/api/services`` list, ``domain``-filtered.

    ``descriptions`` (``async_get_all_descriptions`` — ``{domain: {service:
    desc}}``) and ``translations`` (``async_get_translations`` for the ``services``
    category) are loaded off the loop by :func:`_services_list_prep`.

    ``domain`` is the only filter — an exact match, same semantics both paths.
    There is deliberately NO ``query`` coarse-filter: the server's exact filter
    matches against the CONCATENATION ``f"{domain}.{service} {name} {description}"``
    (with a title-cased ``service.replace("_"," ").title()`` name fallback), and a
    cheap per-field component pass is NOT a superset of that — it misses queries
    spanning the ``domain.service`` / name / description boundaries and the
    title-cased fallback, so forwarding ``query`` would silently drop matching
    services. No consumer forwards ``query`` (the server re-runs its exact filter +
    pagination over the full payload), so the component simply doesn't accept it.
    Translations are scoped to the kept domains' key prefixes.
    """
    descriptions = descriptions or {}
    translations = translations or {}
    domain_filter = params.get("domain")

    services: list[dict[str, Any]] = []
    kept_domains: set[str] = set()
    for domain, domain_services in descriptions.items():
        if domain_filter and domain != domain_filter:
            continue
        services_map = (
            dict(domain_services) if isinstance(domain_services, Mapping) else {}
        )
        services.append({"domain": domain, "services": services_map})
        kept_domains.add(str(domain))

    return {
        "services": services,
        "translations": _filter_service_translations(translations, kept_domains),
    }


async def _services_list_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Async pre-step for ``services_list``: load descriptions + translations off-loop.

    Both ``async_get_all_descriptions`` and ``async_get_translations`` await
    (translation loads touch the filesystem / integration setup), so they run in
    the prep via the seam wrappers below and :func:`_do_services_list` stays a pure
    reshape/filter.
    """
    language = msg.get("language") or "en"
    descriptions = await _fetch_service_descriptions(hass)
    translations = await _fetch_service_translations(hass, language)
    return {"descriptions": descriptions, "translations": translations}


async def _fetch_service_descriptions(hass: HomeAssistant) -> Mapping[str, Any]:
    """core ``async_get_all_descriptions(hass)``; function-local import test seam.

    A non-``Mapping`` return is core drift, NOT an empty catalog: RAISE
    ``HomeAssistantError`` (→ server command-error fallback to the legacy REST
    ``/api/services`` read) rather than serving a well-formed empty catalog the
    server would trust as authoritative. A raising ``async_get_all_descriptions``
    already propagates the same way.
    """
    from homeassistant.helpers.service import async_get_all_descriptions

    result = await async_get_all_descriptions(hass)
    if not isinstance(result, Mapping):
        raise _substrate_unavailable("service descriptions")
    return result


async def _fetch_service_translations(
    hass: HomeAssistant, language: str
) -> Mapping[str, Any]:
    """core ``async_get_translations(hass, language, "services")``; test seam.

    Fails soft (empty map) so a translation-load failure degrades to the untranslated
    service list rather than failing the whole command.
    """
    from homeassistant.helpers.translation import async_get_translations

    try:
        result = await async_get_translations(hass, language, "services")
    except Exception:  # translations are additive; degrade to none on any error
        _LOGGER.warning(
            "services_list: could not load service translations; "
            "continuing without them",
            exc_info=True,
        )
        return {}
    return result if isinstance(result, Mapping) else {}


def _filter_service_translations(
    translations: Mapping[str, Any], kept_domains: set[str]
) -> dict[str, Any]:
    """Keep only translation keys whose domain segment is in ``kept_domains``.

    Backend ``services``-category keys are ``component.<domain>.services.<service>.…``
    so the domain is the second dotted segment.
    """
    return {
        key: value
        for key, value in translations.items()
        if _translation_key_domain(key) in kept_domains
    }


def _translation_key_domain(key: Any) -> str | None:
    """The ``<domain>`` segment of a ``component.<domain>.services.…`` translation key."""
    if not isinstance(key, str):
        return None
    parts = key.split(".")
    return parts[1] if len(parts) > 1 else None


# =============================================================================
# ha_mcp_tools/reference_data
# =============================================================================
def _do_reference_data(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return the service index + entity-id universe the reference validator reads.

    ``services`` is the REST ``/api/services`` list shape ``build_service_index``
    consumes — reusing :func:`_overview_services`, whose per-service bodies are
    EMPTY dicts (the index only reads service-name keys). ``entity_ids`` is every
    ``hass.states.async_all()`` id (the ``build_entity_set`` universe). Pure,
    synchronous, no prep — both are in-memory reads.

    A drifted service registry (``hass.services.async_services()`` raised / renamed
    → non-``Mapping``) or state machine (``hass.states.async_all`` absent / renamed)
    RAISES ``HomeAssistantError`` (→ server command-error fallback to the legacy
    REST ``get_services()`` / ``get_states()`` pair) rather than returning empty
    catalogs — which would make EVERY reference emit a false "not found" warning,
    where the legacy failure mode is skip-validation. A genuinely-empty (but
    present) substrate still returns its empty result.
    """
    include_states = params.get("include_states", True)
    if not isinstance(
        _call_no_arg(getattr(hass, "services", None), "async_services"), Mapping
    ):
        raise _substrate_unavailable("service registry")
    entity_ids: list[str] = []
    if include_states:
        states_obj = getattr(hass, "states", None)
        if not callable(getattr(states_obj, "async_all", None)):
            raise _substrate_unavailable("state machine")
        for state in _iter_states(hass):
            entity_id = getattr(state, "entity_id", None)
            if entity_id:
                entity_ids.append(entity_id)
    return {"services": _overview_services(hass), "entity_ids": entity_ids}


# =============================================================================
# ha_mcp_tools/search — search_visibility (opt-in hidden-set exclusion)
# =============================================================================
# HA's EntityCategory enum values (homeassistant.const). Mirrors the server
# resolver's KNOWN_ENTITY_CATEGORIES so an unknown exclude_category hides nothing.
_KNOWN_ENTITY_CATEGORIES = frozenset({"config", "diagnostic"})

# Degradation warnings surfaced when a visibility dimension fails open (mirrors the
# server resolver so the two paths emit byte-identical text — pinned by the
# cross-seam warnings contract test). The component cannot import the server
# package (it ships over HACS independently), so these strings are duplicated here
# and the contract test asserts they stay equal to ``visibility.resolver``'s.
_ASSIST_UNAVAILABLE_WARNING = (
    "Entity visibility filter is enabled with respect_assist_exposure but the "
    "Assist exposure data was unavailable; that dimension is skipped for this "
    "request (other dimensions still apply)."
)
_ALLOWLIST_REGISTRY_EMPTY_WARNING = (
    "Entity visibility filter is enabled with an area/label allowlist but the "
    "entity registry returned no entries; those allow dimensions are skipped for "
    "this request (an allow_entity_ids list, if set, still applies) so the filter "
    "does not blank every entity."
)


def _unknown_categories_warning(unknown_categories: set[str]) -> str:
    """The resolver's unknown-``exclude_categories`` warning text (byte-identical)."""
    return (
        "Entity visibility: ignoring unknown exclude_categories "
        f"{sorted(unknown_categories)} (valid: config, diagnostic)."
    )


def _visibility_hidden_set(
    view: _RegistryView,
    states: Any,
    visibility: Mapping[str, Any],
    should_expose_fn: Any,
    *,
    assist_available: bool = True,
) -> set[str]:
    """Compute the opt-in hidden entity_id set, mirroring the server's resolver.

    A pure replication of ``visibility.resolver.hidden_entity_ids`` over the live
    ``_RegistryView`` + ``states`` (rather than the WS ``{success, result}``
    payloads the server passes): a conjunction of independent hide dimensions —
    the deny list, the category / hidden / area / label excludes (area/labels are
    device-inherited), the allow-list restrict mode, and the Assist dimension. The
    Assist dimension delegates to the injectable ``should_expose_fn(entity_id) ->
    bool`` (:func:`_assist_should_expose` in production — a READ-ONLY reconstruction
    of core's ``async_should_expose`` from the explicit exposure map + expose_new +
    domain defaults, matching the resolver; a fake in tests), the injection point the
    cross-seam contract test aligns with the server's own Assist result. The
    production seam is read-only by design: core's own ``async_should_expose`` writes
    computed defaults back, which this fast path must not do (see
    :func:`_assist_should_expose`).

    ``should_expose_fn`` is consulted only when ``respect_assist_exposure`` is set
    AND ``assist_available`` is True. When the config requests the Assist dimension
    but the exposure machinery is unavailable (``assist_available=False``), the
    dimension is SKIPPED — hiding nothing by Assist — mirroring the resolver's
    "skip the dimension when its data is unavailable" fail-open (the paired
    degradation warning is surfaced by :func:`_visibility_warnings`). Kept a
    standalone pure function so it is unit-testable without the full search
    pipeline.
    """
    exclude_categories = set(visibility.get("exclude_categories") or [])
    categories = exclude_categories & _KNOWN_ENTITY_CATEGORIES
    exclude_hidden = bool(visibility.get("exclude_hidden"))
    denied = set(visibility.get("deny_entity_ids") or [])
    exclude_areas = set(visibility.get("exclude_areas") or [])
    exclude_labels = set(visibility.get("exclude_labels") or [])
    allow_entity_ids = set(visibility.get("allow_entity_ids") or [])
    allow_areas = set(visibility.get("allow_areas") or [])
    allow_labels = set(visibility.get("allow_labels") or [])
    respect_assist = bool(visibility.get("respect_assist_exposure"))
    # ``assist_active`` gates the per-entity Assist should_expose sub-check (skipped
    # when the config asks for Assist but its data is unavailable). The allow/assist
    # loop below is guarded by ``allow_active or respect_assist`` — a structural
    # mirror of the resolver's guard, not a functional requirement: when Assist is
    # requested-but-unavailable and no allowlist is set, the loop runs but hides
    # nothing (both sub-checks are inert). Kept identical so the two paths match.
    assist_active = respect_assist and assist_available
    allow_active = bool(allow_areas or allow_labels or allow_entity_ids)

    registry_by_id = _registry_index_by_id(view)
    # states-only entity universe (YAML/template entities absent from the registry
    # that the allow / Assist dimensions must still be able to hide).
    state_ids = {
        eid for eid in (getattr(s, "entity_id", None) for s in states or []) if eid
    }

    # Fail-open guard (mirrors the resolver): an area/label allowlist needs registry
    # data to match; if the registry is empty but there are states-only candidates,
    # restrict mode would blank everything, so drop those allow dimensions.
    if (allow_areas or allow_labels) and not registry_by_id and state_ids:
        allow_areas = set()
        allow_labels = set()
        allow_active = bool(allow_entity_ids)

    hidden: set[str] = set(denied)
    _apply_visibility_excludes(
        view,
        registry_by_id,
        denied,
        categories,
        exclude_hidden,
        exclude_areas,
        exclude_labels,
        hidden,
    )
    if allow_active or respect_assist:
        _apply_visibility_allow_assist(
            view,
            registry_by_id,
            state_ids,
            allow_active,
            allow_entity_ids,
            allow_areas,
            allow_labels,
            assist_active,
            should_expose_fn,
            hidden,
        )
    return hidden


def _visibility_warnings(
    view: _RegistryView,
    states: Any,
    visibility: Mapping[str, Any],
    *,
    assist_available: bool = True,
) -> list[str]:
    """Degradation warnings for a visibility computation, mirroring the resolver.

    Companion to :func:`_visibility_hidden_set`: the hidden-set function silently
    fails open on a degraded dimension (an unknown ``exclude_category``, an
    area/label allowlist against an empty registry, or a requested-but-unavailable
    Assist dimension), so this returns the operator-facing warnings the server's
    ``load_hidden_set`` would emit for the same config. The ha_search consumer
    merges them into the response so the component fast path is no longer silent
    about incomplete filtering. Byte-identical to ``visibility.resolver``'s warning
    text (pinned by the cross-seam contract test). Kept a standalone pure function
    so each degraded dimension is unit-testable.

    The resolver's registry-unavailable warning has no analog here: the component
    reads HA's live in-process registry, which is never the failed-WS payload the
    server can receive.
    """
    warnings: list[str] = []

    exclude_categories = set(visibility.get("exclude_categories") or [])
    unknown = exclude_categories - _KNOWN_ENTITY_CATEGORIES
    if unknown:
        warnings.append(_unknown_categories_warning(unknown))

    allow_areas = set(visibility.get("allow_areas") or [])
    allow_labels = set(visibility.get("allow_labels") or [])
    registry_by_id = _registry_index_by_id(view)
    state_ids = {
        eid for eid in (getattr(s, "entity_id", None) for s in states or []) if eid
    }
    # Empty-registry allowlist fail-open: same guard as _visibility_hidden_set —
    # only fires when there are states-only candidates the restrict mode would
    # otherwise blank.
    if (allow_areas or allow_labels) and not registry_by_id and state_ids:
        warnings.append(_ALLOWLIST_REGISTRY_EMPTY_WARNING)

    if bool(visibility.get("respect_assist_exposure")) and not assist_available:
        warnings.append(_ASSIST_UNAVAILABLE_WARNING)

    return warnings


def _registry_index_by_id(view: _RegistryView) -> dict[str, Any]:
    """Index the entity-registry entries by entity_id."""
    index: dict[str, Any] = {}
    for entry in _all_entity_entries(view):
        eid = getattr(entry, "entity_id", None)
        if eid:
            index[eid] = entry
    return index


def _apply_visibility_excludes(
    view: _RegistryView,
    registry_by_id: dict[str, Any],
    denied: set[str],
    categories: set[str],
    exclude_hidden: bool,
    exclude_areas: set[str],
    exclude_labels: set[str],
    hidden: set[str],
) -> None:
    """Add the exclude-dimension hits to ``hidden`` (registry-derived, deny-first).

    Mirrors the resolver's exclude loop. The empty-set dimensions are inert (``x in
    set()`` / ``set() & x`` are falsy), so an inactive dimension hides nothing
    without a guard; only ``exclude_hidden`` is a bool flag and keeps its guard.
    """
    for eid, entry in registry_by_id.items():
        if eid in denied:
            continue
        if _enum_value(getattr(entry, "entity_category", None)) in categories:
            hidden.add(eid)
            continue
        if exclude_hidden and getattr(entry, "hidden_by", None) is not None:
            hidden.add(eid)
            continue
        if _effective_area_for_entry(view, entry) in exclude_areas:
            hidden.add(eid)
            continue
        if exclude_labels & _effective_labels_for_entry(view, entry):
            hidden.add(eid)


def _apply_visibility_allow_assist(
    view: _RegistryView,
    registry_by_id: dict[str, Any],
    state_ids: set[str],
    allow_active: bool,
    allow_entity_ids: set[str],
    allow_areas: set[str],
    allow_labels: set[str],
    assist_active: bool,
    should_expose_fn: Any,
    hidden: set[str],
) -> None:
    """Add allow-restrict + Assist hits to ``hidden`` over registry + states.

    These conjunctive filters must reach states-only entities, so they iterate the
    full candidate universe. Mirrors the resolver's allow/assist loop. ``should_expose_fn``
    is consulted only when ``assist_active`` (Assist requested AND its data
    available); an unavailable-Assist request degrades to no Assist hiding here,
    with the warning surfaced separately.
    """
    for eid in registry_by_id.keys() | state_ids:
        if eid in hidden:
            continue
        entry = registry_by_id.get(eid)
        if allow_active and not _entity_allowed(
            view, eid, entry, allow_entity_ids, allow_areas, allow_labels
        ):
            hidden.add(eid)
            continue
        if assist_active and not should_expose_fn(eid):
            hidden.add(eid)


def _entity_allowed(
    view: _RegistryView,
    eid: str,
    entry: Any,
    allow_entity_ids: set[str],
    allow_areas: set[str],
    allow_labels: set[str],
) -> bool:
    """Whether an entity satisfies the allowlist (restrict mode) — matched, so kept."""
    if eid in allow_entity_ids:
        return True
    if entry is None:
        return False
    if _effective_area_for_entry(view, entry) in allow_areas:
        return True
    return bool(allow_labels & _effective_labels_for_entry(view, entry))


def _effective_area_for_entry(view: _RegistryView, entry: Any) -> str | None:
    """An entity's ``area_id`` falling back to its device's (HA area inheritance)."""
    area_id = getattr(entry, "area_id", None)
    if isinstance(area_id, str) and area_id:
        return area_id
    device_id = getattr(entry, "device_id", None)
    if isinstance(device_id, str) and device_id:
        dev = _device(view, device_id)
        dev_area = getattr(dev, "area_id", None) if dev is not None else None
        return dev_area if isinstance(dev_area, str) and dev_area else None
    return None


def _effective_labels_for_entry(view: _RegistryView, entry: Any) -> set[str]:
    """An entity's labels plus its device's labels (device labels apply to entities)."""
    labels = set(getattr(entry, "labels", None) or [])
    device_id = getattr(entry, "device_id", None)
    if isinstance(device_id, str) and device_id:
        dev = _device(view, device_id)
        if dev is not None:
            labels |= set(getattr(dev, "labels", None) or [])
    return labels


def _assist_should_expose(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether ``entity_id`` is exposed to the ``conversation`` assistant — READ-ONLY.

    A read-only replication of core's ``async_should_expose`` for the conversation
    assistant. core's real function is NOT called because it has a WRITE side effect:
    for an entity with no explicit stored exposure it computes the default and
    persists it back (``entity_registry.async_update_entity_options`` for a registry
    entity, or the exposed-entities store for a legacy one — see core
    ``exposed_entities.py``). Consulting it once per candidate entity from a
    ``readOnlyHint`` search would stamp exposure onto the whole entity universe and
    pin those defaults, which violates this module's pure-read contract. So this
    reconstructs the SAME precedence (matching the server resolver's
    ``_is_assist_exposed``) with no write: an explicit per-entity ``should_expose``
    wins; otherwise the "expose new entities" flag gates the default-exposure check.

    Composed from three read-only, individually monkeypatchable seams. Fails OPEN
    (returns ``True`` — do not hide) on any error, matching the resolver's "skip the
    Assist dimension when its data is unavailable" behaviour.
    """
    try:
        explicit = _explicit_assist_exposure(hass, entity_id)
        if explicit is not None:
            return explicit
        if not _assist_expose_new_entities(hass):
            return False
        return _assist_default_exposed(hass, entity_id)
    except Exception:  # fail open (do not hide) on any error, mirroring the resolver
        return True


def _explicit_assist_exposure(hass: HomeAssistant, entity_id: str) -> bool | None:
    """The entity's explicit ``conversation`` ``should_expose`` (True/False), else None.

    Reads the SAME read-only surface :func:`_do_exposure`'s list mode mirrors — core's
    ``async_get_entity_settings`` (registry ``options`` for a registry entity, else
    the legacy exposed-entities store), which never writes. Returns ``None`` when there
    is no explicit override (an id in neither the registry nor the store, or no
    ``conversation.should_expose`` key), so the caller falls through to the
    expose-new default. A non-``Unknown entity`` raise propagates (fails open upstream).
    """
    try:
        settings = _async_get_entity_settings(hass, entity_id)
    except Exception as exc:
        if _is_unknown_entity_error(exc):
            return None
        raise
    conv = settings.get("conversation") if isinstance(settings, Mapping) else None
    if isinstance(conv, Mapping) and "should_expose" in conv:
        return bool(conv["should_expose"])
    return None


def _assist_expose_new_entities(hass: HomeAssistant) -> bool:
    """core's ``ExposedEntities.async_get_expose_new_entities("conversation")`` — read-only.

    Function-local import + a standalone seam so the fake-hass suite can monkeypatch
    it. A ``@callback`` that only reads the assistant preferences (no store write).
    """
    from homeassistant.components.homeassistant.exposed_entities import (
        DATA_EXPOSED_ENTITIES,
    )

    exposed = hass.data[DATA_EXPOSED_ENTITIES]
    return bool(exposed.async_get_expose_new_entities("conversation"))


def _assist_default_exposed(hass: HomeAssistant, entity_id: str) -> bool:
    """Read-only mirror of ``ExposedEntities._is_default_exposed`` for ``conversation``.

    core's ``async_should_expose`` calls the private ``_is_default_exposed`` and then
    WRITES the result back; this recomputes it WITHOUT the write. Imports core's own
    default-exposure constants (no drift — the running core defines them) and uses
    core's ``get_device_class``, so the domain / device-class verdict matches core
    exactly. entity_category / hidden_by entities are never a default exposure. A
    standalone seam so the fake-hass suite can monkeypatch it.
    """
    from homeassistant.components.homeassistant.exposed_entities import (
        DEFAULT_EXPOSED_BINARY_SENSOR_DEVICE_CLASSES,
        DEFAULT_EXPOSED_DOMAINS,
        DEFAULT_EXPOSED_SENSOR_DEVICE_CLASSES,
    )
    from homeassistant.helpers import entity_registry as er

    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and (
        getattr(entry, "entity_category", None) is not None
        or getattr(entry, "hidden_by", None) is not None
    ):
        return False
    domain = entity_id.split(".")[0] if "." in entity_id else entity_id
    if domain in DEFAULT_EXPOSED_DOMAINS:
        return True
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.entity import get_device_class

    try:
        device_class = get_device_class(hass, entity_id)
    except HomeAssistantError:  # the entity no longer exists — matches core
        return False
    if domain == "binary_sensor":
        return device_class in DEFAULT_EXPOSED_BINARY_SENSOR_DEVICE_CLASSES
    if domain == "sensor":
        return device_class in DEFAULT_EXPOSED_SENSOR_DEVICE_CLASSES
    return False


def _assist_exposure_available(hass: HomeAssistant) -> bool:
    """Whether core's Assist exposure machinery can be consulted for this request.

    The resolver emits ``_ASSIST_UNAVAILABLE_WARNING`` when its expose-list fetch
    fails wholesale; the component's analog is core's ``async_should_expose`` being
    unavailable — it reads ``hass.data[DATA_EXPOSED_ENTITIES]`` and raises when the
    exposed_entities store isn't set up (``_assist_should_expose`` then fails open
    per entity, hiding nothing but warning about nothing either). Probing the store
    once lets the caller skip the Assist dimension AND surface the resolver-parity
    degradation warning instead of degrading silently. Imported lazily and a test
    seam (monkeypatched alongside ``_assist_should_expose``).
    """
    try:
        from homeassistant.components.homeassistant.exposed_entities import (
            DATA_EXPOSED_ENTITIES,
        )
    except Exception:
        return False
    data = getattr(hass, "data", None)
    return isinstance(data, Mapping) and DATA_EXPOSED_ENTITIES in data


# =============================================================================
# ha_mcp_tools/server_entry
# =============================================================================
def _do_server_entry(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Locate the component's OWN server config entry and return its identity.

    Iterates the component's ``DOMAIN`` config entries and picks the server-type
    one by the explicit ``entry.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_SERVER`` marker
    the component's own config flow stamps — the single ``entry.data`` key this
    reads (the documented data-minimization exception; nothing else from
    ``entry.data`` is emitted). ``channel`` / ``pip_spec`` come from
    ``entry.options`` (``None`` when absent); ``entry_id`` is ``None`` when no
    server entry exists.
    """
    for entry in _iter_config_entries(hass):
        if getattr(entry, "domain", None) != DOMAIN:
            continue
        if _entry_marker_type(entry) != ENTRY_TYPE_SERVER:
            continue
        options = getattr(entry, "options", None)
        opts = options if isinstance(options, Mapping) else {}
        return {
            "entry_id": getattr(entry, "entry_id", None),
            "channel": opts.get(OPT_CHANNEL),
            "pip_spec": opts.get(OPT_PIP_SPEC),
        }
    return {"entry_id": None, "channel": None, "pip_spec": None}


def _entry_marker_type(entry: Any) -> Any:
    """Read ONLY ``entry.data[CONF_ENTRY_TYPE]`` — the entry-type marker key."""
    data = getattr(entry, "data", None)
    if isinstance(data, Mapping):
        return data.get(CONF_ENTRY_TYPE)
    return None


# =============================================================================
# ha_mcp_tools/call_service  (the first WRITE capability — Phase 3, issue #1813)
# =============================================================================
def _do_call_service(
    hass: HomeAssistant, params: dict[str, Any], *, result: dict[str, Any]
) -> dict[str, Any]:
    """Pure sync formatter for ``call_service``.

    ALL of the work — the authoritative domain block, the ``ServiceNotFound``
    check, the pre-state capture, the expected-aware register-before-fire listener,
    the single ``async_call`` dispatch, the immediate-match, and the bounded
    confirmation wait — happens in the async :func:`_call_service_prep`, which hands
    the finished result dict in as ``result``. This function only returns that
    envelope (mirroring every other ``_do_*``: the WS wrapper's ``send_result`` adds
    the outer success frame), so no awaiting / blocking work ever runs in a ``_do_*``
    step (D2).
    """
    return result


async def _call_service_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Do all of ``call_service``'s async work; return ``{"result": <envelope>}``.

    The order is load-bearing:

    1. **D1 — authoritative domain block (security-critical).** Refuse
       ``domain == "ha_mcp_tools"`` (case/whitespace-normalized) BEFORE any
       ``has_service`` / dispatch. This is enforced HERE, in the component that
       fires the call, independent of (and in addition to) the server-side guard:
       a component ``call_service`` that skipped it would let a caller invoke the
       admin-gated ``ha_mcp_tools.get_caller_token`` in-process (the server IS
       admin) and then every file/YAML service. The block keys off the RESOLVED
       domain, so it holds no matter which path reaches this function.
    2. **ServiceNotFound** before dispatch, so an unknown service is a clean
       ``SERVICE_NOT_FOUND`` and never a landed-but-unreported write.
    3. Pre-state capture for each ``entity_id`` (a synchronous in-memory read).
    4. Register the expected-aware ``EVENT_STATE_CHANGED`` waiter BEFORE the dispatch
       (D5) so a fast entity's event can't arrive before the listener exists. The
       waiter confirms only on reaching the server's ``expected_state`` hint (skipping
       intermediate/noise events); a ``None`` hint keeps any-first-event confirmation.
    5. Fire exactly ONE ``async_call`` (``blocking=True``); flip ``dispatched``
       immediately after so a post-dispatch problem is never retried as a failed
       call (D3/D9).
    6. Immediate-match (:func:`_match_immediate`) for an idempotent no-op — a target
       whose CURRENT state already equals its hint confirms with NO wait — then a
       bounded wait for whatever is still unconfirmed (D4); expiry is ``partial``.
    7. Diff pre→post into the real transition(s) — the REAL observed transition, never
       the hint value.

    Raised exceptions PROPAGATE — the WS handler turns them into a command error the
    server maps (D7). Two distinct classes propagate: the D1 domain block and
    ``ServiceNotFound`` are PRE-dispatch (they raise before ``async_call``, so nothing
    landed); an ``async_call`` / ``return_response`` validation error is MID-dispatch
    (the handler may mutate state and THEN raise — the documented D9 at-most-once
    residual, NOT pre-dispatch). A confirmation timeout is caught and reported as
    ``partial`` (never re-raised): the call already landed.

    The whole POST-dispatch section — the immediate-match re-read, the wait, and the
    pre→post diff — runs inside ONE ``try`` that, once ``dispatched`` is True, never
    lets a raise escape (I1): a raise mapped to a command error would re-POST an
    already-landed write. A pre-confirmation ``async_call`` failure (``dispatched``
    still False) re-raises so the server can map it; any post-dispatch failure degrades
    to a minimal dispatched-but-unconfirmed envelope. The immediate-match re-read is
    itself raise-proof (:func:`_match_immediate`), the attribute diff is raise-proofed
    (:func:`_values_differ`), and ``unsub`` always runs in the ``finally``.
    Serialization residual (bounded, no sanitize pass): the transition embeds each
    state's ``as_dict()`` and the WS transport re-encodes it; HA core enforces
    JSON-serializable state attributes for its own REST/WS/recorder APIs, so a state
    that reached the component already serializes — re-encoding it here is safe.
    """
    domain = msg["domain"]
    service = msg["service"]

    # 1./2. Pre-dispatch guards (D1 domain block + ServiceNotFound) — both raise
    # BEFORE any listener registration or dispatch, so a refused call is never a
    # landed-but-unreported write and ``async_call`` is provably never reached.
    _guard_call_service_target(hass, domain, service)

    service_data = msg.get("service_data") or {}
    entity_ids = list(msg.get("entity_ids") or [])
    wait = msg.get("wait", True)
    timeout = msg.get("timeout", CALL_SERVICE_DEFAULT_TIMEOUT)
    return_response = msg.get("return_response", False)
    should_confirm = bool(wait and entity_ids)
    # The server's confirmation HINT (``_SERVICE_TO_STATE.get(service)``), applied to
    # every confirmation target. Absent / None keeps any-first-event confirmation.
    expected_state = msg.get("expected_state")
    expected_by_entity = dict.fromkeys(entity_ids, expected_state)

    # 3. Pre-state capture (synchronous in-memory reads, guarded against drift).
    pre = {eid: _state_as_dict(_state_get(hass, eid)) for eid in entity_ids}

    # 4. Register-before-fire (D5): only when there is something to confirm.
    evt: Any = None
    captured: dict[str, Any] = {}
    unsub: Any = None
    if should_confirm:
        evt, captured, unsub = _register_transition_waiter(
            hass, set(entity_ids), expected_by_entity
        )

    # 5. Dispatch exactly once. 6. Immediate-match + bounded wait. 7. Build the diff.
    # Everything after ``dispatched = True`` is inside this ONE try so no post-dispatch
    # raise (a drifted re-read, an exotic-attribute diff) escapes (I1) — that would be
    # mapped to legacy and re-POST an already-landed write. ``unsub`` always runs.
    response: Any = None
    dispatched = False
    result: dict[str, Any]
    try:
        response = await hass.services.async_call(
            domain,
            service,
            dict(service_data),
            blocking=True,
            return_response=return_response,
        )
        dispatched = True
        if should_confirm:
            await _await_confirmation(
                hass, entity_ids, expected_by_entity, captured, evt, timeout
            )
        result = _build_call_service_result(
            hass,
            domain,
            service,
            entity_ids,
            pre,
            captured,
            should_confirm=should_confirm,
            dispatched=dispatched,
            return_response=return_response,
            response=response,
        )
    except Exception:
        # PRE-confirmation ``async_call`` failure (never dispatched) → re-raise so the
        # server maps it (D7/D9 MID-dispatch residual). Any POST-dispatch failure →
        # degrade to a minimal dispatched-but-unconfirmed envelope (never re-POSTed).
        if not dispatched:
            raise
        _LOGGER.exception(
            "call_service post-dispatch step failed after dispatch; returning "
            "dispatched-but-unconfirmed envelope (%s.%s)",
            domain,
            service,
        )
        result = _dispatched_unconfirmed_result(domain, service)
    finally:
        if unsub is not None:
            unsub()
    return {"result": result}


def _guard_call_service_target(hass: HomeAssistant, domain: str, service: str) -> None:
    """Pre-dispatch refusals for ``call_service`` — raise BEFORE any dispatch.

    * **D1 (security-critical)** — refuse ``domain == "ha_mcp_tools"``
      (case/whitespace-normalized). Enforced HERE, in the component that fires the
      call, independent of (and in addition to) the server-side guard: a component
      ``call_service`` that skipped it would let a caller invoke the admin-gated
      ``ha_mcp_tools.get_caller_token`` in-process (the server IS admin) and then
      every file/YAML service. The block keys off the RESOLVED domain, so it holds
      no matter which path reaches this function.
    * **ServiceNotFound** — an unknown service is a clean ``SERVICE_NOT_FOUND``, not
      a phantom write. Both raises propagate to the WS handler (D7).
    """
    if str(domain).strip().lower() == DOMAIN:
        from homeassistant.exceptions import HomeAssistantError

        raise HomeAssistantError(
            "the ha_mcp_tools domain is not callable through call_service; "
            "use the dedicated ha_* tools"
        )
    if not hass.services.has_service(domain, service):
        from homeassistant.exceptions import ServiceNotFound

        raise ServiceNotFound(domain, service)


def _event_state_value(state: Any) -> Any:
    """The primary state string from a ``State`` object OR an ``as_dict()`` mapping.

    Raise-proof: an exotic/stub shape (or a ``.state`` accessor that raises) degrades
    to ``None`` so the expected-aware waiter and the post-dispatch immediate-match can
    never propagate past the dispatch (I1). ``None`` never equals a (str) expected
    hint, so an unreadable state simply does not confirm — it keeps waiting.
    """
    try:
        if isinstance(state, Mapping):
            return state.get("state")
        return getattr(state, "state", None)
    except Exception:  # pragma: no cover - defensive; exotic/stub shapes
        return None


def _register_transition_waiter(
    hass: HomeAssistant, target_set: set[str], expected_by_entity: Mapping[str, Any]
) -> tuple[Any, dict[str, Any], Any]:
    """Register the ``EVENT_STATE_CHANGED`` listener BEFORE the dispatch (D5).

    Returns ``(evt, captured, unsub)``: ``evt`` is set once every id in
    ``target_set`` has reported a CONFIRMING ``new_state``; ``captured`` maps each id
    to its raw new_state; ``unsub`` tears the listener down. Registering before the
    dispatch closes the race where a fast entity's event arrives before the
    listener exists.

    ``expected_by_entity`` supplies each target's server-computed expected-state HINT
    (``_SERVICE_TO_STATE``). With a hint, ONLY the event that reaches that state
    confirms — a multi-phase service's intermediate states (``lock``:
    unlocked→locking→locked) and attribute-only noise (a ``media_player`` position
    tick while ``state`` stays "playing") are skipped, NOT captured. With no hint
    (``None``, e.g. ``set_temperature``) any first ``new_state`` confirms — today's
    unchanged behavior.
    """
    import asyncio

    from homeassistant.const import EVENT_STATE_CHANGED

    evt = asyncio.Event()
    captured: dict[str, Any] = {}

    def _on_change(event: Any) -> None:
        data = getattr(event, "data", None) or {}
        eid = data.get("entity_id")
        new = data.get("new_state")
        # M-newstate-none: a state_changed with new_state=None means the entity was
        # REMOVED mid-wait — that is not a confirmed transition, so do NOT capture it
        # (leaving the target uncaptured keeps the op ``partial``, not falsely
        # confirmed). ``_post_state`` still re-reads a best-available current state.
        if eid in target_set and new is not None:
            exp = expected_by_entity.get(eid)
            # Hint present → confirm ONLY on reaching the expected state (skip
            # intermediate/noise events). Hint None → any first event confirms.
            if exp is None or _event_state_value(new) == exp:
                captured[eid] = new
                if target_set <= set(captured):
                    evt.set()

    # Mark the listener a HA callback so ``EventBus.async_listen`` classifies its
    # ``HassJob`` as ``HassJobType.Callback`` and runs it INLINE on the event loop
    # (``is_callback`` reads exactly ``getattr(func, "_hass_callback", False)``).
    # Without this a plain function is ``HassJobType.Executor``: every instance-wide
    # ``state_changed`` gets thrown at the thread pool, and ``evt.set()`` then runs
    # cross-thread on a non-thread-safe ``asyncio.Event`` → delayed/spurious
    # ``partial`` confirmations and an ``InvalidStateError`` race with the
    # timeout-cancel. We set the attribute directly rather than using ``@callback``:
    # the unit-test harness MagicMock-stubs ``homeassistant.core``, so the decorator
    # would be a MagicMock and break the listener, whereas the plain attribute set is
    # exactly what ``callback`` does (``func.__dict__["_hass_callback"] = True``) and
    # is inert under the stub.
    _on_change._hass_callback = True  # type: ignore[attr-defined]

    unsub = hass.bus.async_listen(EVENT_STATE_CHANGED, _on_change)
    return evt, captured, unsub


def _match_immediate(
    hass: HomeAssistant,
    entity_ids: list[str],
    expected_by_entity: Mapping[str, Any],
    captured: dict[str, Any],
) -> None:
    """Capture a target whose CURRENT state already equals its expected hint.

    Mirrors legacy's "sample current state first": for each not-yet-captured target
    with a known expected state, re-read the live state right after ``async_call``
    returns and, if it already equals the expected value, capture it as confirmation
    so NO wait is needed — a ``turn_on`` on an already-on light confirms instantly
    (pre == expected == "on") instead of timing out to a false ``partial``. No-hint
    targets (``exp is None``) are left for the any-first-event waiter — unchanged.

    Called AFTER the dispatch, so it MUST be raise-proof (I1): a re-read that raised
    and reached the WS handler would be mapped to legacy and re-POST an already-landed
    write. ``_state_get`` is guarded (``None`` on drift) and ``_event_state_value`` is
    raise-proof, so this never propagates past dispatch. ``captured`` is mutated in
    place with the raw ``State`` (``_post_state``/``_state_as_dict`` normalize it, the
    same shape the waiter captures).
    """
    for eid in entity_ids:
        exp = expected_by_entity.get(eid)
        if exp is None or eid in captured:
            continue
        cur = _state_get(hass, eid)
        if cur is not None and _event_state_value(cur) == exp:
            captured[eid] = cur


async def _await_confirmation(
    hass: HomeAssistant,
    entity_ids: list[str],
    expected_by_entity: Mapping[str, Any],
    captured: dict[str, Any],
    evt: Any,
    timeout: float,
) -> None:
    """Immediate-match the idempotent no-ops, then bounded-wait whatever remains (D4).

    Runs AFTER the single ``async_call``: :func:`_match_immediate` captures a target
    whose current state already equals its hint (a ``turn_on`` on an already-on light)
    with NO wait; the bounded wait runs only if some target is still unconfirmed and
    its expiry is swallowed (``partial``, never a failure). Kept as its own helper so
    :func:`_call_service_prep` stays under the complexity gate; raise-proof re-read
    (``_match_immediate``), so nothing here escapes past the dispatch (I1).
    """
    import asyncio

    _match_immediate(hass, entity_ids, expected_by_entity, captured)
    if set(entity_ids) <= set(captured):
        return  # every target confirmed by the immediate-match — no wait needed
    try:
        await asyncio.wait_for(evt.wait(), timeout)
    except TimeoutError:
        pass  # partial confirmation, not a failure (D4)


def _build_call_service_result(
    hass: HomeAssistant,
    domain: str,
    service: str,
    entity_ids: list[str],
    pre: Mapping[str, Any],
    captured: Mapping[str, Any],
    *,
    should_confirm: bool,
    dispatched: bool,
    return_response: bool,
    response: Any,
) -> dict[str, Any]:
    """Assemble the ``call_service`` response envelope from the captured transition.

    ``confirmed`` is True only when every target reported within the wait;
    ``partial`` is a confirmation that lapsed (never a failure). ``dispatched`` is
    only ever ``True`` here (a pre-dispatch problem raised out of the prep);
    reporting the flag rather than a literal keeps the D9 at-most-once boundary —
    "reached this shape ⇒ the single async_call fired" — explicit for the server,
    which never retries a dispatched write. ``service_response`` is present only
    when it was both requested AND non-``None``.
    """
    transitions = [
        _call_service_transition(eid, pre.get(eid), _post_state(hass, eid, captured))
        for eid in entity_ids
    ]
    confirmed = bool(should_confirm and set(entity_ids) <= set(captured))
    result: dict[str, Any] = {
        "domain": domain,
        "service": service,
        "dispatched": dispatched,
        "confirmed": confirmed,
        "partial": bool(should_confirm and not confirmed),
        "transitions": transitions,
    }
    if return_response and response is not None:
        result["service_response"] = response
    return result


def _dispatched_unconfirmed_result(domain: str, service: str) -> dict[str, Any]:
    """Minimal ``call_service`` envelope when post-dispatch formatting raised (I1).

    The single ``async_call`` already fired; building the rich transition raised (an
    exotic captured state / serialization edge). Report dispatched-but-unconfirmed with
    no transitions rather than propagating — a propagated raise would become a command
    error the server maps to legacy and re-POST an already-landed write (double-apply).
    Reads only the plain domain/service strings, so it cannot itself raise.
    """
    return {
        "domain": domain,
        "service": service,
        "dispatched": True,
        "confirmed": False,
        "partial": True,
        "transitions": [],
    }


def _post_state(
    hass: HomeAssistant, entity_id: str, captured: Mapping[str, Any]
) -> Any:
    """The post-dispatch state for ``entity_id`` as a plain dict (or ``None``).

    Prefers the listener-captured ``new_state`` (the event that confirmed the
    transition, normalized through the shared :func:`_state_as_dict`); falls back
    to a fresh guarded ``hass.states`` re-read when the target did not report within
    the wait (the ``partial`` case), so a transition row is still populated with the
    best-available current state. Both ``None`` (a vanished/stateless entity) and
    core drift degrade to ``None`` rather than raising.
    """
    if entity_id in captured:
        as_dict = _state_as_dict(captured[entity_id])
        if as_dict is not None:
            return as_dict
    return _state_as_dict(_state_get(hass, entity_id))


def _values_differ(a: Any, b: Any) -> bool:
    """Whether two attribute values differ, raise-proof for array-like values.

    A plain ``a != b`` raises "truth value ... is ambiguous" for numpy arrays and
    other exotic ``__ne__`` results that aren't a bool. Post-dispatch formatting MUST
    NOT raise (a raise here would surface as a command error the server maps to legacy
    → a re-POST of an already-landed write), so fall back to a ``repr`` compare when
    the direct compare's truthiness is not a plain bool.
    """
    try:
        return bool(a != b)
    except Exception:  # array-like / exotic __ne__ whose result isn't a plain bool
        return repr(a) != repr(b)


def _call_service_transition(
    entity_id: str,
    old_state: dict[str, Any] | None,
    new_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """The real pre→post transition for one target entity.

    ``changed`` compares the top-level ``state`` (always a plain string — safe);
    ``attributes_changed`` lists the attribute keys whose values differ (added/removed
    keys included), compared through :func:`_values_differ` so an array-like attribute
    value cannot raise. Both sides may be ``None`` (a stateless or vanished entity),
    which the ``or {}`` guards fold into an all-``None`` comparison rather than raising.
    """
    old = old_state or {}
    new = new_state or {}
    old_attrs = old.get("attributes") or {}
    new_attrs = new.get("attributes") or {}
    attributes_changed = sorted(
        key
        for key in set(old_attrs) | set(new_attrs)
        if _values_differ(old_attrs.get(key), new_attrs.get(key))
    )
    return {
        "entity_id": entity_id,
        "old_state": old_state,
        "new_state": new_state,
        "changed": old.get("state") != new.get("state"),
        "attributes_changed": attributes_changed,
    }


# =============================================================================
# ha_mcp_tools/bulk_call_service  (the BATCH write capability — Phase 3, D5a)
# =============================================================================
def _do_bulk_call_service(
    hass: HomeAssistant, params: dict[str, Any], *, result: dict[str, Any]
) -> dict[str, Any]:
    """Pure sync formatter for ``bulk_call_service``.

    Like :func:`_do_call_service`, ALL of the work — the per-op D1 domain block,
    the ``ServiceNotFound`` checks, the expected-aware register-before-fire pass, the
    dispatches, the per-op immediate-match, and the one bounded batch wait — happens
    in the async :func:`_bulk_call_service_prep`, which hands the finished envelope in
    as ``result``. This function only returns it, so no awaiting / blocking work runs
    in a ``_do_*`` step (D2).
    """
    return result


async def _bulk_call_service_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Do all of ``bulk_call_service``'s async work; return ``{"result": ...}``.

    Register-before-fire is trivially correct for the batch: every listener is
    registered in one synchronous pass BEFORE any ``async_call`` is issued, so no
    op's confirming event can arrive before its listener exists. The order is
    load-bearing:

    1. **D1 batch fail-closed (security-critical).** Run
       :func:`_guard_call_service_target` for EVERY operation FIRST — before any
       pre-state read, listener, or dispatch. A single op targeting the
       ``ha_mcp_tools`` domain (or an unknown service) makes the WHOLE frame raise:
       no partial batch is dispatched, so no ``ha_mcp_tools.*`` op can ever slip
       through in a batch (register-before-fire + all-guards-first means a refused
       op aborts before any real write lands).
    2. Pre-state capture for every op's ``entity_ids`` (synchronous in-memory).
    3. Register ALL expected-aware confirmation listeners in one pass BEFORE any
       dispatch (each op's ``expected_state`` hint governs its waiter); every
       ``unsub`` is torn down in the ``finally``.
    4. Dispatch: ``parallel`` fans the ``async_call``s out through
       :func:`asyncio.gather` with ``return_exceptions=True`` so one op's failure
       does not abort the others (its exception is recorded on that op, NOT raised —
       UNLIKE the step-1 guards, which DO raise the whole frame pre-dispatch);
       ``parallel=False`` awaits them in order. Each op flips its own ``dispatched``
       flag the moment its ``async_call`` returns.
    5. Immediate-match per dispatched op (:func:`_bulk_match_immediate`) — an
       idempotent no-op whose current state already equals its hint confirms with no
       wait — then ONE shared bounded deadline (D4, not per-op serial timeouts) for
       every op still unconfirmed.
    6. Per-op pre→post diff, reusing the single-call transition/build helpers.
    7. Return every op's result plus batch counts.
    """
    operations = list(msg.get("operations") or [])
    parallel = bool(msg.get("parallel", True))
    wait = bool(msg.get("wait", True))
    timeout = msg.get("timeout", CALL_SERVICE_DEFAULT_TIMEOUT)

    # 1. D1 batch fail-closed: guard EVERY op before ANY pre-state / listener /
    #    dispatch. A refused op (``ha_mcp_tools`` domain or unknown service) raises
    #    the whole frame here, so nothing in the batch is ever dispatched partially.
    for op in operations:
        _guard_call_service_target(hass, op["domain"], op["service"])

    # 2. Normalize + pre-state capture (synchronous in-memory reads) per op.
    ops = [_bulk_op_record(hass, op, wait=wait) for op in operations]

    # 3. Register-before-fire (D5): every confirmable op's listener is registered in
    #    one synchronous pass BEFORE any dispatch; ALL unsubs torn down in finally.
    unsubs = _bulk_register_all(hass, ops)
    try:
        await _bulk_dispatch_all(hass, ops, parallel=parallel)  # 4
        _bulk_match_immediate(hass, ops)  # 5a immediate-match idempotent no-ops
        await _bulk_wait_all(ops, timeout)  # 5b (one shared deadline; expiry=partial)
    finally:
        for unsub in unsubs:
            unsub()

    # 6./7. Per-op pre→post diff + batch counts. Post-dispatch assembly MUST be total
    # (I1): the ops already fired, so a raise here would be mapped to legacy and
    # re-dispatch every landed op (double-fire). On any failure return a minimal
    # envelope reporting each op dispatched-but-unconfirmed (its real dispatched/error
    # preserved) instead of propagating.
    try:
        result = _build_bulk_result(hass, ops)
    except Exception:
        _LOGGER.exception(
            "bulk_call_service post-dispatch assembly failed after dispatch; "
            "returning dispatched-but-unconfirmed batch envelope"
        )
        result = _dispatched_unconfirmed_bulk_result(ops)
    return {"result": result}


def _bulk_op_record(
    hass: HomeAssistant, op: Mapping[str, Any], *, wait: bool
) -> dict[str, Any]:
    """A mutable working record for one batch operation (incl. its pre-state).

    Reads the resolved ``{domain, service, service_data?, entity_ids?,
    expected_state?}`` row defensively (the direct-prep tests pass raw dicts that
    never went through the schema, so the mutable defaults are re-applied here).
    ``pre`` is the synchronous in-memory pre-state per target; ``expected_by_entity``
    maps every target to this op's confirmation hint (``_SERVICE_TO_STATE``) so the
    waiter + immediate-match key off it; ``dispatched`` / ``error`` / ``response``
    start empty and are filled during dispatch; ``should_confirm`` is true only when
    the batch is waiting AND this op names targets to confirm.
    """
    entity_ids = list(op.get("entity_ids") or [])
    expected_state = op.get("expected_state")
    return {
        "domain": op["domain"],
        "service": op["service"],
        "service_data": op.get("service_data") or {},
        "entity_ids": entity_ids,
        "expected_by_entity": dict.fromkeys(entity_ids, expected_state),
        "should_confirm": bool(wait and entity_ids),
        "pre": {eid: _state_as_dict(_state_get(hass, eid)) for eid in entity_ids},
        "evt": None,
        "captured": {},
        "dispatched": False,
        "response": None,
        "error": None,
    }


async def _bulk_dispatch_one(hass: HomeAssistant, op: dict[str, Any]) -> None:
    """Fire exactly one op's ``async_call`` and flip its ``dispatched`` flag.

    Mirrors the single-call dispatch (``blocking=True``), but bulk never requests a
    per-op ``return_response`` (D5a keeps the batch simple — the single
    ``call_service`` covers response-returning calls). ``dispatched`` is set only
    AFTER ``async_call`` returns, so a raise (captured per-op by the caller) leaves
    it ``False`` and the op is never counted as a landed write.
    """
    op["response"] = await hass.services.async_call(
        op["domain"],
        op["service"],
        dict(op["service_data"]),
        blocking=True,
        return_response=False,
    )
    op["dispatched"] = True


def _bulk_register_all(hass: HomeAssistant, ops: list[dict[str, Any]]) -> list[Any]:
    """Register every confirmable op's transition listener in one pass (D5).

    One synchronous sweep BEFORE any dispatch, so no op's confirming event can
    arrive before its listener exists. Each confirmable op is handed its own ``evt``
    / ``captured`` (mutating the op record); the returned ``unsub`` list is torn down
    in the prep's ``finally``. Non-confirmable ops register nothing.

    If ``async_listen`` raises mid-sweep (near-impossible in practice), every listener
    already registered in this pass is unsubbed before re-raising — the prep's
    ``try/finally`` has not been entered yet, so those would otherwise leak on the bus.
    """
    unsubs: list[Any] = []
    try:
        for op in ops:
            if op["should_confirm"]:
                evt, captured, unsub = _register_transition_waiter(
                    hass, set(op["entity_ids"]), op["expected_by_entity"]
                )
                op["evt"] = evt
                op["captured"] = captured
                unsubs.append(unsub)
    except Exception:
        for unsub in unsubs:
            unsub()
        raise
    return unsubs


async def _bulk_dispatch_all(
    hass: HomeAssistant, ops: list[dict[str, Any]], *, parallel: bool
) -> None:
    """Fire every op's dispatch, recording a per-op failure without aborting the batch.

    ``parallel`` fans the dispatches out through :func:`asyncio.gather` with
    ``return_exceptions=True`` so one op's ``async_call`` raising is captured on THAT
    op (``error`` set, ``dispatched`` left ``False``) and the others still run;
    ``parallel=False`` awaits them in order, catching each op's failure the same way.
    Neither mode propagates a per-op dispatch error — that is the whole point of the
    batch (UNLIKE the pre-dispatch D1/ServiceNotFound guards, which DO raise).
    """
    import asyncio

    if parallel:
        outcomes = await asyncio.gather(
            *(_bulk_dispatch_one(hass, op) for op in ops),
            return_exceptions=True,
        )
        for op, outcome in zip(ops, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                op["error"] = _bulk_op_error(outcome)
    else:
        for op in ops:
            try:
                await _bulk_dispatch_one(hass, op)
            except Exception as err:
                op["error"] = _bulk_op_error(err)


def _bulk_match_immediate(hass: HomeAssistant, ops: list[dict[str, Any]]) -> None:
    """Immediate-match every dispatched, confirmable op (idempotent no-ops).

    Runs the SAME raise-proof :func:`_match_immediate` per op AFTER the batch
    dispatch and BEFORE the shared wait: an op whose target already sits at its
    expected hint (a ``turn_on`` on an already-on light) is captured here so
    :func:`_bulk_wait_all` skips it — no full-timeout stall for a batch of no-ops.
    Raise-proof (``_match_immediate`` guards each re-read), so it cannot propagate
    past the batch dispatch (I1).
    """
    for op in ops:
        if op["should_confirm"] and op["dispatched"]:
            _match_immediate(
                hass, op["entity_ids"], op["expected_by_entity"], op["captured"]
            )


async def _bulk_wait_all(ops: list[dict[str, Any]], timeout: float) -> None:
    """Bounded confirmation wait for the batch: ONE shared deadline (D4).

    Waits up to ``timeout`` for every dispatched, confirmable op's transition on a
    single shared deadline (not per-op serial timeouts). An op already FULLY captured
    by the immediate-match (:func:`_bulk_match_immediate`) is skipped — its ``evt`` was
    never ``set`` (the match populates ``captured`` directly), so waiting on it would
    stall the whole batch to the timeout. Expiry is swallowed — whichever ops did not
    report are ``partial`` (never a failure); the ops that did report stay confirmed.
    A batch with nothing left to confirm returns immediately.
    """
    import asyncio

    waiters = [
        op["evt"].wait()
        for op in ops
        if op["should_confirm"]
        and op["dispatched"]
        and not (set(op["entity_ids"]) <= set(op["captured"]))
    ]
    if not waiters:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*waiters), timeout)
    except TimeoutError:
        pass  # partial confirmation for whichever ops did not report (D4)


def _bulk_op_error(exc: BaseException) -> str:
    """A short, stable error string for a per-op dispatch failure.

    A per-op ``async_call`` exception under the batch is recorded here (never
    propagated past the frame — the other ops still return their results), so the
    server can surface which op failed and why without the whole batch aborting.
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _build_bulk_op_result(hass: HomeAssistant, op: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble one op's result envelope from its captured transition.

    Reuses the single-call :func:`_call_service_transition` / :func:`_post_state`
    diff helpers so the per-op transition shape is byte-identical to
    ``call_service``. ``confirmed`` requires the op to have DISPATCHED and every
    target to have reported within the shared wait; ``partial`` is a dispatched-but
    -unconfirmed op (never a failure); an op whose ``async_call`` raised carries
    ``error`` with ``dispatched: false`` and is neither confirmed nor partial.
    """
    entity_ids = list(op["entity_ids"])
    captured = op["captured"]
    should_confirm = op["should_confirm"]
    dispatched = op["dispatched"]
    transitions = [
        _call_service_transition(
            eid, op["pre"].get(eid), _post_state(hass, eid, captured)
        )
        for eid in entity_ids
    ]
    confirmed = bool(should_confirm and dispatched and set(entity_ids) <= set(captured))
    result: dict[str, Any] = {
        "domain": op["domain"],
        "service": op["service"],
        "entity_ids": entity_ids,
        "dispatched": dispatched,
        "confirmed": confirmed,
        "partial": bool(should_confirm and dispatched and not confirmed),
        "transitions": transitions,
    }
    if op["error"] is not None:
        result["error"] = op["error"]
    return result


def _build_bulk_result(
    hass: HomeAssistant, ops: list[dict[str, Any]]
) -> dict[str, Any]:
    """The batch envelope: every op's result plus the batch counts.

    ``dispatched`` counts ops whose single ``async_call`` fired; ``failed`` counts
    ops that recorded a per-op ``error`` (dispatch raised). ``total`` is the batch
    size, so ``total - dispatched`` is the refused/failed-before-landing count.
    """
    op_results = [_build_bulk_op_result(hass, op) for op in ops]
    return {
        "operations": op_results,
        "total": len(op_results),
        "dispatched": sum(1 for r in op_results if r["dispatched"]),
        "failed": sum(1 for r in op_results if r.get("error") is not None),
    }


def _dispatched_unconfirmed_bulk_result(
    ops: list[dict[str, Any]],
) -> dict[str, Any]:
    """Minimal batch envelope when post-dispatch assembly raised (I1 total-formatting).

    Every op already fired (or recorded a pre-landing ``error``); building the rich
    transition rows raised (an exotic attribute value / serialization edge). Report
    each op with empty transitions rather than propagating — a propagated raise would
    become a command error the server maps to legacy and re-dispatch every landed op
    (double-fire). Reads only each record's plain ``domain``/``service``/``entity_ids``
    /``dispatched``/``should_confirm``/``error`` fields (never the rich captured
    state), so it cannot itself raise; the real per-op ``dispatched``/``error`` are
    preserved so a genuinely-failed op is not misreported as landed.
    """
    op_results = [
        {
            "domain": op["domain"],
            "service": op["service"],
            "entity_ids": list(op["entity_ids"]),
            "dispatched": op["dispatched"],
            "confirmed": False,
            "partial": bool(op["should_confirm"] and op["dispatched"]),
            "transitions": [],
            **({"error": op["error"]} if op["error"] is not None else {}),
        }
        for op in ops
    ]
    return {
        "operations": op_results,
        "total": len(op_results),
        "dispatched": sum(1 for r in op_results if r["dispatched"]),
        "failed": sum(1 for r in op_results if r.get("error") is not None),
    }
