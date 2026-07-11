"""In-process WebSocket command surface for the ha_mcp_tools component.

This module registers versioned ``ha_mcp_tools/*`` WebSocket commands that the
ha-mcp server calls in-process (same HA core, no REST/WS round-trips) behind a
capability gate. v1.1.0 ships four commands (three capabilities):

* ``ha_mcp_tools/info`` — the handshake: ``schema_version`` + ``capabilities[]``
  + ``component_version`` + advisory ``limits``. One cached probe tells the
  server which commands are live (capability negotiation, NOT a version floor).
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

from .const import COMPONENT_VERSION

_LOGGER = logging.getLogger(__name__)

# --- Wire contract -----------------------------------------------------------
WS_API_PREFIX = "ha_mcp_tools"
WS_INFO = f"{WS_API_PREFIX}/info"
WS_SEARCH = f"{WS_API_PREFIX}/search"
WS_OVERVIEW = f"{WS_API_PREFIX}/overview"
WS_HELPERS_LIST = f"{WS_API_PREFIX}/helpers_list"

# Wire-format generation of the request/response envelopes. Bumped only on an
# *incompatible* shape change to an existing command; additive fields do not
# bump it (the server checks ``schema_version >= N`` before using a new shape).
SCHEMA_VERSION = 1

# Which commands exist. Grows one entry per shipped command; the server gates
# each consumer on ``capability in caps.capabilities``. Never remove an entry
# without a major bump. (``info`` is always present in 1.1.0, so it carries no
# capability key of its own.)
CAPABILITIES: list[str] = ["search", "overview", "helpers_list"]

# Advisory caps advertised in ``info.limits`` so no single WS frame balloons.
MAX_RESULTS = 500
MAX_BODY_BYTES = 1_000_000
LIMITS = {"max_results": MAX_RESULTS, "max_body_bytes": MAX_BODY_BYTES}

DEFAULT_LIMIT = 10

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
        (_info_schema(), lambda hass, msg: _do_info(), None),
        (_search_schema(), _do_search, _search_prep),
        (_overview_schema(), _do_overview, None),
        (_helpers_list_schema(), _do_helpers_list, None),
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


# =============================================================================
# ha_mcp_tools/info
# =============================================================================
def _do_info() -> dict[str, Any]:
    """Return the handshake payload (pure; no hass access)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "component_version": COMPONENT_VERSION,
        "capabilities": list(CAPABILITIES),
        "limits": dict(LIMITS),
    }


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

    view = _resolve_registries(hass)
    diagnostics: dict[str, int] = {}
    partial_reasons: list[str] = []

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


def _load_secret_values(hass: HomeAssistant) -> frozenset[str]:
    """Load the string values from the instance's ``secrets.yaml``.

    These scrub resolved ``!secret`` plaintext out of the config-body match
    corpus: a YAML-loaded automation/script/scene body (or a flow-helper's
    options) can carry a secret already resolved to its plaintext value, and
    matching inside it would turn ``ha_search`` into a probe oracle — a query
    equal to a suspected secret confirmed via ``match_in_config``. Any body leaf
    that exactly equals one of these values is dropped before scoring.

    Defensive by design: an absent ``secrets.yaml`` (``FileNotFoundError`` — the
    common case) yields an empty set silently; a present-but-unreadable or
    malformed file logs one warning and also degrades to an empty set (the scrub
    turns OFF but never raises). Only string values are collected — a secret can
    be any YAML scalar, but a non-string can't be a plaintext-leak leaf and is
    skipped. Loaded off the event loop by :func:`_search_prep` once per search,
    never cached across calls, so an edited ``secrets.yaml`` applies on the next
    search. ``secrets.yaml`` is a flat ``key: value`` mapping with no custom
    tags, so the plain ``yaml.safe_load`` (not HA's ``!secret``/``!include``
    loader) reads it correctly.
    """
    config = getattr(hass, "config", None)
    path_fn = getattr(config, "path", None)
    if not callable(path_fn):
        return frozenset()
    try:
        path = path_fn("secrets.yaml")
        if not path:
            return frozenset()
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError:
        # Expected: many instances have no secrets.yaml — nothing to scrub.
        return frozenset()
    except Exception:
        # Present-but-unreadable / malformed / permission error: unexpected, so
        # warn once (this runs once per search) to surface a broken scrub, then
        # degrade OFF (empty set) rather than raising into the WS handler.
        _LOGGER.warning(
            "Could not read secrets.yaml for the search secret-scrub; "
            "continuing without it",
            exc_info=True,
        )
        return frozenset()
    if not isinstance(raw, dict):
        return frozenset()
    return frozenset(v for v in raw.values() if isinstance(v, str) and v)


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


def _entity_record(state: Any, view: _RegistryView) -> dict[str, Any]:
    """Join a state with the entity/device/area/floor/label registries."""
    entity_id = getattr(state, "entity_id", "") or ""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    attrs = getattr(state, "attributes", None) or {}
    friendly = attrs.get("friendly_name", entity_id)

    reg = _reg_entity(view, entity_id)
    aliases = (
        sorted(str(a) for a in (getattr(reg, "aliases", None) or [])) if reg else []
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

    area_name = _area_name(view, area_id)
    floor_name = _floor_name_for_area(view, area_id)
    label_names = _label_names(view, labels)

    # Scored texts extend the server's id + friendly-name pair with the specific
    # joined identifiers (alias / area / floor / label / device). The bare domain
    # is deliberately excluded: matching it would score every entity of a domain
    # at the exact tier (a "light" query flooding all lights), which the server
    # does not do — domain is a filter dimension, not a scored text.
    match_texts = [entity_id, friendly, *aliases, *label_names, *dev_texts]
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
        "_hidden": hidden,
        "_area_id": area_id,
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
    """Best-effort conversion of registry/state objects to plain JSON-able data."""
    if isinstance(value, dict):
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
    (or None) passes through unchanged.
    """
    if value is None or isinstance(value, str):
        return value
    return getattr(value, "value", str(value))


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
def _do_helpers_list(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """List collection helpers (live state bodies) + flow helpers (config-entry options).

    Flow-helper ``options`` come straight from ``ConfigEntry.options`` — no
    OptionsFlow start/abort dance, and NEVER ``entry.data`` (integration
    credentials). Every record carries the CURRENT entity_id + display name from
    the entity registry so a renamed helper shows current values (issue #1794),
    not the stale storage-collection name. No secret scrub: collection bodies come
    from the storage collection (or live state attributes) and flow options are
    storage-backed — neither is YAML-derived, so no resolved ``!secret`` plaintext
    can appear.

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
        helpers.extend(_flow_helpers_list(hass, view, type_filter))
        covered |= FLOW_HELPER_DOMAINS
    return {
        "helpers": helpers,
        "count": len(helpers),
        "covered_types": sorted(covered),
    }


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
    hass: HomeAssistant, view: _RegistryView, type_filter: frozenset[str] | None
) -> list[dict[str, Any]]:
    """Flow (config-entry-backed) helpers — options + title + entry_id, never data."""
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
            _plainify(dict(raw_options)) if isinstance(raw_options, Mapping) else {}
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
