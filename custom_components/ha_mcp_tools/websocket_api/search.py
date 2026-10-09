"""The ``ha_mcp_tools/search`` command: entity search and result assembly."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from ..search_locations import (
    add_location_metadata,
    add_registry_failures,
    resolve_search_location,
)
from .assist import _assist_exposure_available, _assist_should_expose
from .constants import (
    _SPLIT_RE,
    ALL_SEARCH_TYPES,
    CONFIG_SEARCH_TYPES,
    DEFAULT_LIMIT,
    FLOW_HELPER_DOMAINS,
    SEARCH_TYPE_AUTOMATION,
    SEARCH_TYPE_ENTITY,
    SEARCH_TYPE_HELPER,
    SEARCH_TYPE_SCENE,
    SEARCH_TYPE_SCRIPT,
)
from .flow_domains import _flow_helper_domains
from .registry import (
    _area_name,
    _device_dict_repr,
    _effective_area_for_entry,
    _floor_name_for_area,
    _iter_states,
    _label_names,
    _reg_entity,
    _RegistryView,
    _resolve_registries,
    _unambiguous_device_entries,
)
from .search_config import _search_config_surface, _search_helpers
from .search_score import _apply_hidden_penalty, _text_tier, _tokenize
from .secrets import SCRUB_DEGRADED_WARNING, _load_secret_scrub, _load_secret_values
from .visibility import (
    _visibility_hidden_set,
    _visibility_inventory,
    _visibility_warnings,
)


def _do_search(  # noqa: PLR0915
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    secret_values: frozenset[str] = frozenset(),
    secret_scrub_degraded: bool = False,
    flow_domains: frozenset[str] = FLOW_HELPER_DOMAINS,
    custom_domains: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Unified in-process search. Pure over ``hass`` — the WS wrapper is thin.

    Joins live registries + states, scores per the server's tiers, paginates
    per surface, and returns the ``ha_search``-shaped envelope.

    ``secret_values`` is the resolved-``!secret`` scrub set, loaded off the event
    loop by :func:`_search_prep` and passed in (default empty — the loader is
    skipped for an entity-only search, and direct callers/tests supply it
    explicitly). It keeps this function a pure, synchronous in-memory read.
    Flow-helper ``options`` are emitted under ``include_config``, so a degraded
    scrub (reported by the pre-step for a helper search) adds
    :data:`SCRUB_DEGRADED_WARNING` to ``warnings`` there.
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
    membership_requested = bool(params.get("result_fields"))
    # Opt-in visibility filter (search_visibility capability). A non-empty dict of
    # the server's raw VisibilityConfig fields; applied as a hard entity exclude.
    visibility = params.get("visibility")

    view = _resolve_registries(hass)
    diagnostics: dict[str, int] = {}
    partial_reasons: list[str] = []
    # Visibility degradation warnings (unknown category / conflicting device /
    # empty-registry allowlist / Assist unavailable), collected in the entity block
    # below when a visibility filter is applied. Surfaced additively so the fast
    # path isn't silent about incomplete filtering (parity with the server's
    # load_hidden_set warnings).
    visibility_warnings: list[str] = []
    hidden: set[str] = set()
    location = (
        resolve_search_location(view, area_filter)
        if area_filter and SEARCH_TYPE_ENTITY in search_types
        else None
    )

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
            area_filter=location.area_ids if location else None,
            state_filter=state_filter,
            include_membership=membership_requested,
        )
        # Opt-in visibility filter: a hard exclude applied BEFORE counts/pagination,
        # exactly where the legacy path drops ``visibility_hidden`` entities at the
        # top of ``_match_exact_search_entity`` (independent of ``include_hidden``,
        # which the ``_search_entities`` join already applied). See
        # :func:`_visibility_hidden_set`.
        if isinstance(visibility, Mapping) and visibility:
            states_list = _iter_states(hass)
            inventory = _visibility_inventory(view, states_list, visibility)
            assist_applies = (
                bool(visibility.get("respect_assist_exposure"))
                and not inventory.allowlist.authorized
            )
            # Short-circuit the probe unless Assist applies. True on the skipped
            # path means there is no Assist degradation to report; the hidden-set
            # helper does not consult Assist while an authorizing allowlist is
            # active (a legacy wire keeps Assist in play, so the probe still runs).
            assist_available = not assist_applies or _assist_exposure_available(hass)
            hidden = _visibility_hidden_set(
                view,
                states_list,
                visibility,
                lambda eid: _assist_should_expose(hass, eid),
                assist_available=assist_available,
                inventory=inventory,
            )
            visibility_warnings = _visibility_warnings(
                view,
                states_list,
                visibility,
                assist_available=assist_available,
                inventory=inventory,
            )
            if hidden:
                scored_entities = [
                    r for r in scored_entities if r["entity_id"] not in hidden
                ]
        scored_entities.sort(key=lambda r: (-r["score"], r["entity_id"]))
        entity_total = len(scored_entities)
        page = scored_entities[offset : offset + limit]
        _redact_hidden_members(
            page,
            hidden,
            view=view,
            include_hidden=include_hidden,
            enabled=membership_requested,
        )
        entity_has_more = offset + len(page) < entity_total
        entities = [
            _project_entity(r, include_membership=membership_requested) for r in page
        ]
        add_registry_failures(
            location, view._access_failures, diagnostics, partial_reasons
        )

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
                flow_domains=flow_domains,
                custom_domains=custom_domains,
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
    _add_optional_keys(
        result,
        diagnostics,
        visibility_warnings,
        scrub_degraded=secret_scrub_degraded and include_config,
    )
    return add_location_metadata(result, location)


def _add_optional_keys(
    result: dict[str, Any],
    diagnostics: dict[str, int],
    visibility_warnings: list[str],
    *,
    scrub_degraded: bool,
) -> None:
    """Set the keys a search response carries only when they have content."""
    if diagnostics:
        result["diagnostics"] = diagnostics
    if scrub_degraded:
        result.setdefault("warnings", []).append(SCRUB_DEGRADED_WARNING)
    # Additive (present only when non-empty, no schema_version bump): the server's
    # ha_search consumer merges these into the response's top-level warnings.
    if visibility_warnings:
        result["visibility_warnings"] = visibility_warnings


def _sort_key(rec: dict[str, Any]) -> str:
    """Stable tiebreak for combined config sorting."""
    return str(rec.get("entity_id") or rec.get("id") or rec.get("name") or "")


async def _search_prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Async pre-step for ``search``: load the secret-scrub set off the loop, and
    for a helper search ask Core which domains are helper flows
    (:func:`_flow_helper_domains`).

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
    if SEARCH_TYPE_HELPER not in search_types:
        values = await hass.async_add_executor_job(_load_secret_values, hass)
        return {"secret_values": values}
    values, degraded = await hass.async_add_executor_job(_load_secret_scrub, hass)
    return {
        "secret_values": values,
        "secret_scrub_degraded": degraded,
        **await _flow_helper_domains(hass),
    }


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
    area_filter: set[str] | None,
    state_filter: str | None,
    include_membership: bool = False,
) -> list[dict[str, Any]]:
    """Score every state against the query over the joined registry view."""
    results: list[dict[str, Any]] = []
    # Lower the state filter once; the entity state is lowered per record so the
    # compare is case-insensitive (e.g. an input_select holding "Vacation"
    # matches state_filter="vacation").
    state_filter_lower = state_filter.lower() if state_filter is not None else None
    for state in _iter_states(hass):
        rec = _entity_record(state, view, include_membership=include_membership)
        if domain_filter and rec["domain"] != domain_filter:
            continue
        if rec["_hidden"] and not include_hidden:
            continue
        if (
            state_filter_lower is not None
            and (rec["state"] or "").lower() != state_filter_lower
        ):
            continue
        if area_filter is not None and rec["_area_id"] not in area_filter:
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
    area_id = _effective_area_for_entry(view, reg) if reg else None
    device_id = getattr(reg, "device_id", None) if reg else None
    labels = set(getattr(reg, "labels", None) or []) if reg else set()
    hidden = bool(getattr(reg, "hidden_by", None)) if reg else False

    dev = (
        _unambiguous_device_entries(view).get(device_id)
        if isinstance(device_id, str) and device_id
        else None
    )
    dev_texts: list[str] = []
    if dev is not None:
        dev_row = _device_dict_repr(dev) or {}
        labels |= set(dev_row.get("labels") or [])
        for attr in ("name_by_user", "name", "manufacturer", "model"):
            val = dev_row.get(attr)
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


def _entity_record(
    state: Any, view: _RegistryView, *, include_membership: bool = False
) -> dict[str, Any]:
    """Join a state with the entity/device/area/floor/label registries."""
    entity_id = getattr(state, "entity_id", "") or ""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    attrs = getattr(state, "attributes", None) or {}
    friendly = attrs.get("friendly_name", entity_id)
    members = _normalize_member_entity_ids(attrs) if include_membership else None

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
        **(
            {
                "is_group": members is not None,
                **({"member_entity_ids": members} if members is not None else {}),
            }
            if include_membership
            else {}
        ),
        "_hidden": join["_hidden"],
        "_area_id": join["_area_id"],
        "_match_texts": match_texts,
    }


def _project_entity(
    rec: dict[str, Any], *, include_membership: bool = False
) -> dict[str, Any]:
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
        **(
            {"is_group": rec["is_group"]}
            if include_membership and "is_group" in rec
            else {}
        ),
        **(
            {"member_entity_ids": rec["member_entity_ids"]}
            if include_membership and "member_entity_ids" in rec
            else {}
        ),
    }


def _redact_hidden_members(
    records: list[dict[str, Any]],
    hidden: set[str],
    *,
    view: _RegistryView | None = None,
    include_hidden: bool = True,
    enabled: bool = True,
) -> None:
    """Withhold members excluded by visibility or include_hidden."""
    if not enabled:
        return
    for record in records:
        members = record.get("member_entity_ids")
        denied = bool(members and hidden.intersection(members))
        if members and not include_hidden and view is not None:
            denied = denied or any(
                getattr(_reg_entity(view, member), "hidden_by", None) is not None
                for member in members
            )
        if denied:
            record.pop("member_entity_ids", None)


def _normalize_member_entity_ids(attributes: Any) -> list[str] | None:
    """Normalize HA's modern or historical explicit group membership."""
    if not isinstance(attributes, Mapping):
        return None
    for key in ("group_entities", "entity_id"):
        raw = attributes.get(key)
        if isinstance(raw, (str, bytes, bytearray, Mapping)):
            continue
        if not isinstance(raw, Collection):
            continue
        members: set[str] = set()
        valid = True
        for value in raw:
            if not _is_entity_id(value):
                valid = False
                break
            members.add(value)
        if valid:
            return sorted(members)
    return None


def _is_entity_id(value: Any) -> bool:
    """Return whether a value has the Home Assistant entity ID shape."""
    if not isinstance(value, str) or value.count(".") != 1:
        return False
    domain, object_id = value.split(".", 1)
    return bool(
        domain
        and object_id
        and value == value.lower()
        and all(
            char in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for char in domain + object_id
        )
    )
