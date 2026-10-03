"""Search visibility filter and its warnings for ``ha_mcp_tools/search``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from .ws_registry import (
    _all_entity_entries,
    _conflicting_device_ids,
    _effective_area_for_entry,
    _effective_labels_for_entry,
    _enum_value,
    _invalid_device_area_ids,
    _RegistryView,
)

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
_DEVICE_REGISTRY_CONFLICT_WARNING = (
    "Entity visibility filter is enabled with an area/label dimension but the "
    "device registry contained conflicting identities; ambiguous device-derived "
    "placement and labels were excluded."
)
_DEVICE_REGISTRY_INVALID_AREA_WARNING = (
    "Entity visibility filter is enabled with an area dimension but the device "
    "registry contained invalid area relationships; affected device-derived "
    "placement was excluded."
)


class _AllowlistState(NamedTuple):
    """Effective restrict-mode activity, registry degradation, and precedence.

    ``authorized`` is ``active`` plus the ``allowlist_authorization`` wire flag:
    an allow match then authorizes the entity past the category, HA-hidden, and
    Assist filters. Without the flag the legacy conjunctive semantics apply even
    while ``active`` is True (an older server resolves that way itself).
    """

    active: bool
    degraded: bool
    authorized: bool


class _VisibilityInventory(NamedTuple):
    """Registry/state indexes and allowlist state shared by one search."""

    registry_by_id: dict[str, Any]
    state_ids: set[str]
    allowlist: _AllowlistState


def _visibility_allowlist_state(
    registry_by_id: Mapping[str, Any],
    state_ids: set[str],
    visibility: Mapping[str, Any],
) -> _AllowlistState:
    """Resolve allowlist activity from one precomputed search inventory.

    ``degraded`` deliberately remains true when an explicit ``allow_entity_ids``
    list keeps restrict mode active: only the registry-derived allow dimensions
    degraded, so callers still warn while the explicit ID allowlist remains in
    force. This differs from the resolver's pre-fetch predicate, which asks only
    whether degradation would make Assist relevant again.

    ``authorized`` additionally requires the ``allowlist_authorization`` wire flag:
    an active allowlist alone keeps the legacy conjunctive precedence.
    """
    entity_ids_active = bool(visibility.get("allow_entity_ids"))
    registry_dimensions_active = bool(
        visibility.get("allow_areas") or visibility.get("allow_labels")
    )
    degraded = registry_dimensions_active and not registry_by_id and bool(state_ids)
    active = entity_ids_active or (registry_dimensions_active and not degraded)
    authorized = active and bool(visibility.get("allowlist_authorization"))
    return _AllowlistState(active, degraded, authorized)


def _visibility_inventory(
    view: _RegistryView, states: Any, visibility: Mapping[str, Any]
) -> _VisibilityInventory:
    """Build the registry/state inventory once for one visibility computation."""
    registry_by_id = _registry_index_by_id(view)
    state_ids = _state_entity_ids(states)
    return _VisibilityInventory(
        registry_by_id,
        state_ids,
        _visibility_allowlist_state(registry_by_id, state_ids, visibility),
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
    inventory: _VisibilityInventory | None = None,
) -> set[str]:
    """Compute the opt-in hidden entity_id set, mirroring the server's resolver.

    A pure replication of ``visibility.resolver.hidden_entity_ids`` over the live
    ``_RegistryView`` + ``states`` (rather than the WS ``{success, result}``
    payloads the server passes), including its precedence: concrete deny and
    area/label excludes win; an allowlist match authorizes past the broad category,
    Home Assistant hidden-state, and Assist filters when the wire opted into that
    revised precedence with ``allowlist_authorization`` (without the flag those
    filters still apply to a matched entity, matching the older server). The
    Assist dimension delegates to the injectable ``should_expose_fn(entity_id) ->
    bool`` (:func:`_assist_should_expose` in production — a READ-ONLY reconstruction
    of core's ``async_should_expose`` from the explicit exposure map + expose_new +
    domain defaults, matching the resolver; a fake in tests), the injection point the
    cross-seam contract test aligns with the server's own Assist result. The
    production seam is read-only by design: core's own ``async_should_expose`` writes
    computed defaults back, which this fast path must not do (see
    :func:`_assist_should_expose`).

    ``should_expose_fn`` is consulted only when ``respect_assist_exposure`` is set,
    no AUTHORIZING allowlist is active, and ``assist_available`` is True. When the config
    requests the Assist dimension but the exposure machinery is unavailable
    (``assist_available=False``), the dimension is SKIPPED — hiding nothing by
    Assist — mirroring the resolver's fail-open behavior (the paired
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

    if inventory is None:
        inventory = _visibility_inventory(view, states, visibility)
    registry_by_id = inventory.registry_by_id
    # states-only entity universe (YAML/template entities absent from the registry
    # that the allow / Assist dimensions must still be able to hide).
    state_ids = inventory.state_ids
    allow_active = inventory.allowlist.active
    authorized = inventory.allowlist.authorized
    # Fail-open guard: registry-derived allow dimensions cannot match when the
    # registry is empty but states-only candidates exist.
    if inventory.allowlist.degraded:
        allow_areas = set()
        allow_labels = set()

    # An authorizing allowlist skips the broad Assist filter; a legacy wire keeps
    # it. Only a full degradation (registry-derived dimensions dropped and no
    # allow_entity_ids left) makes ``active`` False and re-enables Assist.
    assist_active = respect_assist and assist_available and not authorized

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
        automatic_excludes_active=not authorized,
    )
    if allow_active or assist_active:
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
            allow_authorizes=authorized,
        )
    return hidden


def _visibility_warnings(
    view: _RegistryView,
    states: Any,
    visibility: Mapping[str, Any],
    *,
    assist_available: bool = True,
    inventory: _VisibilityInventory | None = None,
) -> list[str]:
    """Degradation warnings for a visibility computation, mirroring the resolver.

    Companion to :func:`_visibility_hidden_set`: the hidden-set function silently
    fails open on a degraded dimension (an unknown ``exclude_category``, an
    area/label dimension against conflicting device identities, an area dimension
    against invalid device ancestry, an area/label allowlist against an empty
    registry, or a requested-but-unavailable Assist dimension), so this returns
    the operator-facing warnings the server's
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

    area_or_label_dimension_active = bool(
        visibility.get("exclude_areas")
        or visibility.get("exclude_labels")
        or visibility.get("allow_areas")
        or visibility.get("allow_labels")
    )
    if area_or_label_dimension_active and _conflicting_device_ids(view):
        warnings.append(_DEVICE_REGISTRY_CONFLICT_WARNING)
    if (
        visibility.get("exclude_areas") or visibility.get("allow_areas")
    ) and _invalid_device_area_ids(view):
        warnings.append(_DEVICE_REGISTRY_INVALID_AREA_WARNING)

    if inventory is None:
        inventory = _visibility_inventory(view, states, visibility)
    if inventory.allowlist.degraded:
        warnings.append(_ALLOWLIST_REGISTRY_EMPTY_WARNING)

    if (
        visibility.get("respect_assist_exposure")
        and not inventory.allowlist.authorized
        and not assist_available
    ):
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


def _state_entity_ids(states: Any) -> set[str]:
    """Entity IDs present in the live state-machine snapshot."""
    return {
        eid
        for eid in (getattr(state, "entity_id", None) for state in states or [])
        if eid
    }


def _apply_visibility_excludes(
    view: _RegistryView,
    registry_by_id: dict[str, Any],
    denied: set[str],
    categories: set[str],
    exclude_hidden: bool,
    exclude_areas: set[str],
    exclude_labels: set[str],
    hidden: set[str],
    *,
    automatic_excludes_active: bool,
) -> None:
    """Add automatic and hard exclude hits to ``hidden``.

    Category/HA-hidden filters are skipped for an AUTHORIZING allowlist; concrete
    area/label exclusions remain hard conflicts and always apply. The empty-set
    dimensions are inert (``x in set()`` / ``set() & x`` are falsy), so an inactive
    dimension hides nothing without a guard; only ``exclude_hidden`` is a bool flag
    and keeps its guard.
    """
    for eid, entry in registry_by_id.items():
        if eid in denied:
            continue
        if automatic_excludes_active:
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
    *,
    allow_authorizes: bool,
) -> None:
    """Add allow-restrict + Assist hits to ``hidden`` over registry + states.

    Both filters reach states-only entities. A nonmatching entity is always hidden
    while the allowlist is active. With ``allow_authorizes`` a match skips the
    Assist check; on a legacy wire a matched entity still faces it, so
    ``should_expose_fn`` is consulted whenever Assist is requested and available.
    """
    for eid in registry_by_id.keys() | state_ids:
        if eid in hidden:
            continue
        entry = registry_by_id.get(eid)
        if allow_active:
            if not _entity_allowed(
                view, eid, entry, allow_entity_ids, allow_areas, allow_labels
            ):
                hidden.add(eid)
                continue
            if allow_authorizes:
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
