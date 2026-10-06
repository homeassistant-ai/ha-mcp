"""In-process WebSocket command surface for the ha_mcp_tools component.

This module registers versioned ``ha_mcp_tools/*`` WebSocket commands that the
ha-mcp server calls in-process (same HA core, no REST/WS round-trips) behind a
capability gate. It advertises command capabilities and additive flags
(dashboards_doc_search, device_registry_child_semantics, search_visibility,
search_entity_membership, search_visibility_allowlist_authorization, and
search_unified);
the info handshake carries no capability entry:

* ``ha_mcp_tools/info`` — the handshake: ``schema_version`` + ``capabilities[]``
  + ``component_version`` + advisory ``limits`` + the instance ``timezone``
  (an additive field consumers detect by presence — no capability entry). One
  cached probe tells the server which commands are live (capability
  negotiation, NOT a version floor).
* ``ha_mcp_tools/search`` — a unified in-process search over live registries and
  states, joined and scored, mirroring today's ``ha_search`` response envelope.
  The search_entity_membership flag gates opt-in generic group metadata.
  The search_unified flag covers area/floor resolution, state filtering before
  pagination, queryless listing, and search windows beyond the advisory limit.
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
  (``{metadata, config, yaml}``), which core's ``blueprint/list`` never returns (it
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
  second ``search_visibility_allowlist_authorization`` capability declares that
  this component understands the ``allowlist_authorization`` wire key, which the
  server sends only to a component advertising it and which selects the revised
  precedence (an allowlist match authorizes past category, HA-hidden, and Assist
  filters). Without the key the component keeps the legacy conjunctive
  precedence, so it stays correct against an older released server that still
  resolves the old way in its own outbound scan. A degraded dimension (unknown
  ``exclude_category`` / empty-registry allowlist / unavailable Assist) fails
  open, and :func:`_visibility_warnings` returns the resolver-parity
  ``visibility_warnings`` carried by the response so filtering is not silently
  incomplete.
* ``ha_mcp_tools/server_entry`` — the component locates its OWN server config
  entry (``entry.data[CONF_ENTRY_TYPE] == server``, the one marker key it reads
  from ``entry.data``) and returns ``{entry_id, channel, pip_spec}`` (channel /
  pip_spec from ``entry.options``), so ``ha_dev_manage_server`` need not probe
  every ``ha_mcp_tools``-domain entry's options-flow schema from the outside.
* ``ha_mcp_tools/server_entry_update`` — the WRITE counterpart of ``server_entry``:
  applies a ``channel`` / ``pip_spec`` delta to the server entry via
  ``hass.config_entries.async_update_entry`` DIRECTLY (what core's finish-flow
  does), collapsing ``ha_dev_manage_server(update_source)``'s options-flow start +
  submit round-trip in embedded mode. The delta is MERGED against the LIVE
  ``entry.options`` AT APPLY time (every existing key preserved — a concurrent
  change to another key during the flush window is not clobbered — no blanking of
  the URL/secret overrides), so no preserved-key resend is needed. The
  ``async_update_entry`` fires the entry's own update listener → reload → the
  hardened reinstall; because that reload tears down the very server thread
  answering this frame, the call is NOT made inline — it is scheduled on a
  HASS-level background task (:func:`hass.async_create_background_task`, NOT the
  entry's, so the reload can't cancel it) after :data:`SERVER_ENTRY_UPDATE_FLUSH_DELAY_S`,
  and the prep returns ``{scheduled: True, ...}`` immediately so the WS response
  flushes first. A no-op (merged options equal the current ones) returns
  ``{scheduled: False, unchanged: True, ...}`` without scheduling; no server entry
  raises ``HomeAssistantError`` (→ the server's command-error fallback to its legacy
  options-flow path). All awaiting-adjacent work (the deferred apply) lives in
  :func:`_server_entry_update_prep`; :func:`_do_server_entry_update` is a pure formatter.
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
* ``ha_mcp_tools/template_diagnose`` — renders a template in-process and reports
  the stage (``compile`` / ``render`` / ``timeout`` / ``none``) and, for a
  failure, the error with the template ``line`` and ``source_line`` it points
  at, which Core's ``render_template`` drops (#2522). The render is guarded by
  Core's own ``async_render_will_timeout`` before the template is rendered on
  the loop; see
  :mod:`.template_diagnose`.

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

Module layout. This package's ``__init__`` holds the registration seam
(``async_register_commands``, ``_command_specs`` and ``_build_handler``). The command
code lives in the submodules:

* ``constants`` and ``schemas``: the wire contract, capability list and request schemas.
* ``registry``, ``secrets`` and ``assist``: shared registry, secret-scrub and Assist exposure helpers.
* ``search``, ``search_config``, ``search_score`` and ``visibility``: ``ha_mcp_tools/search``.
* ``overview``, ``services``, ``lookups``, ``config_entries``, ``registries`` and ``dashboards``: the read commands.
* ``system``: ``info``, ``system_snapshot``, ``backup_prep``, ``server_entry``, ``server_entry_update`` and ``template_diagnose``.
* ``call_service`` and ``bulk``: the write commands.
* ``oauth_callbacks``: read and replace the none-mode OAuth callback allowlist.

Extension point — to add another command later: write ``_do_<name>(hass,
params)``, append its capability to :data:`CAPABILITIES`, and add one row to
:func:`_command_specs`. ``info`` enumerates the rest.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .. import helper_collections
from .bulk import _bulk_call_service_prep, _do_bulk_call_service
from .call_service import _call_service_prep, _do_call_service
from .config_entries import _config_entries_prep, _do_config_entries
from .constants import CAPABILITIES, SCHEMA_VERSION
from .dashboards import (
    _dashboard_edit_prep,
    _dashboards_prep,
    _do_dashboard_edit,
    _do_dashboards,
)
from .lookups import (
    _blueprint_get_prep,
    _do_blueprint_get,
    _do_device_get,
    _do_device_list,
    _do_entity_enrich,
    _do_entity_lookup,
    _do_exposure,
    _do_registry_lookup,
)
from .oauth_callbacks import command_specs as _oauth_callback_specs
from .overview import _do_helpers_list, _do_overview, _do_states, _helpers_list_prep
from .registries import _do_registries
from .schemas import (
    _backup_prep_schema,
    _blueprint_get_schema,
    _bulk_call_service_schema,
    _call_service_schema,
    _config_entries_schema,
    _dashboard_edit_schema,
    _dashboards_schema,
    _device_get_schema,
    _device_list_schema,
    _entity_enrich_schema,
    _entity_lookup_schema,
    _exposure_schema,
    _helpers_list_schema,
    _info_schema,
    _overview_schema,
    _reference_data_schema,
    _registries_schema,
    _registry_lookup_schema,
    _search_schema,
    _server_entry_schema,
    _server_entry_update_schema,
    _services_list_schema,
    _states_schema,
    _system_snapshot_schema,
    _template_diagnose_schema,
)
from .search import _do_search, _search_prep
from .services import _do_reference_data, _do_services_list, _services_list_prep
from .system import (
    _do_backup_prep,
    _do_info,
    _do_server_entry,
    _do_server_entry_update,
    _do_system_snapshot,
    _do_template_diagnose,
    _server_entry_update_prep,
    _template_diagnose_prep,
)

_LOGGER = logging.getLogger(__name__)


# =============================================================================
# Registration (thin @websocket_command wrappers over the pure `_do_*` funcs)
# =============================================================================
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the ``ha_mcp_tools/*`` WebSocket commands.

    Called from BOTH config-entry setups since component 2.1.0: the tools entry
    (alongside its service registrations) and the server entry (#2289/#2291).
    The surface is entry-agnostic — every handler reads HA-core state, none of
    it the tools entry's ``hass.data`` — so a server-entry-only install gets it
    too; only the filesystem/YAML HA *services* remain tools-entry-only.

    Idempotent: HA's ``async_register_command`` overwrites an existing handler,
    so re-running on a config-entry reload, or from both entries on a dual-entry
    install, is harmless.

    There is NO unregister. HA's ``websocket_api`` exposes no counterpart to
    ``async_register_command``, so the commands outlive an entry unload and stay
    on the connection surface until Home Assistant restarts. That is deliberate
    and accepted (#2292). Unloading one entry must not strip the surface the
    other entry still serves from, and a surface left behind by a FULLY unloaded
    component holds no privilege of its own: every handler works off live
    HA-core state rather than anything the unloaded entry cached, HA core
    authenticates the connection, and ``@require_admin`` gates each command — so
    a caller reaching it can already do the same through HA's own WS API. The
    service-dispatching writes enforce D1: they refuse
    ``domain == "ha_mcp_tools"`` unconditionally. Dashboard edits do not dispatch
    services: they use Core's Lovelace storage API under the same admin gate as
    ``lovelace/config/save``. Server-entry updates retain their own live-entry
    and option validation. These commands do not grant access to the privileged
    filesystem/YAML services, which
    :func:`~custom_components.ha_mcp_tools._async_unload_tools_entry` does remove
    on unload. Admin-gated commands answering from live core state until the
    next restart is the trade this makes.
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
        (_dashboard_edit_schema(), _do_dashboard_edit, _dashboard_edit_prep),
        (_services_list_schema(), _do_services_list, _services_list_prep),
        (_reference_data_schema(), _do_reference_data, None),
        (_server_entry_schema(), _do_server_entry, None),
        # The WRITE counterpart of server_entry: the deferred ``async_update_entry``
        # scheduling is inherently async, so it lives in ``_server_entry_update_prep``
        # and ``_do_server_entry_update`` is a pure formatter (same seam as the
        # call_service write below).
        (
            _server_entry_update_schema(),
            _do_server_entry_update,
            _server_entry_update_prep,
        ),
        # The service WRITE command: dispatch + the bounded confirmation wait are
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
        (_template_diagnose_schema(), _do_template_diagnose, _template_diagnose_prep),
        *helper_collections.command_specs(vol, er),
        *_oauth_callback_specs(),
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
        msg["ha_mcp_context"] = connection.context(msg)
        extra = await prep(hass, msg) if prep is not None else {}
        connection.send_result(msg["id"], do_fn(hass, msg, **extra))

    return _handler
