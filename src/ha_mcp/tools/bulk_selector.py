"""Resolve deterministic structural selectors into exact bulk-control leaves."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, NamedTuple, NotRequired, TypedDict

from pydantic import ConfigDict, Field

from ..utils.domain_handlers import get_domain_handler
from ..utils.entity_membership import normalize_member_entity_ids
from ..visibility.resolver import VisibilityDataUnavailable, load_hidden_set

logger = logging.getLogger(__name__)

MAX_SELECTOR_ENTITIES = 100

# HA's native scene platform sets its `entity_id` attribute to the entities
# the scene CONFIGURES (homeassistant/components/homeassistant/scene.py:
# `{ATTR_ENTITY_ID: list(self.scene_config.states)}`), not entities it is
# structurally composed of -- those routinely live in other areas or none at
# all. `normalize_member_entity_ids` cannot tell that shape apart from a real
# aggregate's member list (both are just an `entity_id` collection), so a
# scene assigned to a selected area must never be admitted as an expansion
# root; see `resolve_bulk_selector`'s `matching_roots`. This is a domain
# whose entity_id-attribute has a fundamentally different HA-core meaning,
# not a manufacturer/integration heuristic -- it does not reintroduce the
# per-integration checks (is_hue_group, hue_type, ...) this feature
# deliberately avoids.
_NON_AGGREGATE_ROOT_DOMAINS = frozenset({"scene"})


class BulkControlSelector(TypedDict):
    """Exact structural scope for one deterministic bulk action.

    Lives here (not in ``tools_service.py``) so ``_SELECTOR_KEYS`` below can
    derive from this single field set instead of duplicating it as an
    independent literal -- the import direction (``tools_service`` already
    imports from this module) makes that safe without a cycle.
    """

    domain: Annotated[
        str,
        Field(description="Exact Home Assistant domain, e.g. 'light'."),
    ]
    area_ids: NotRequired[
        Annotated[
            list[str],
            Field(description="Exact Home Assistant area IDs to include."),
        ]
    ]
    floor_ids: NotRequired[
        Annotated[
            list[str],
            Field(description="Exact Home Assistant floor IDs to include."),
        ]
    ]
    exclude_entity_ids: NotRequired[
        Annotated[
            list[str],
            Field(
                description=(
                    "Exact entity or aggregate IDs to exclude after recursive "
                    "membership expansion."
                )
            ),
        ]
    ]


BulkControlSelector.__pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[attr-defined]
_SELECTOR_KEYS = frozenset(BulkControlSelector.__annotations__)


class _ExpansionEntry(NamedTuple):
    """One memoized membership-expansion result. Named to keep the two
    ``frozenset[str]`` slots (leaves vs. groups) from being interchangeable
    at the write sites -- a positional bare tuple lets a swapped write
    type-check silently."""

    leaves: frozenset[str]
    groups: frozenset[str]


class _ValidatedSelector(NamedTuple):
    """Normalized selector fields, so ``_validate_selector`` doesn't return a
    bare 5-tuple with three ``list[str]`` slots that a reordering at the call
    site could swap silently."""

    domain: str
    action: str
    area_ids: list[str]
    floor_ids: list[str]
    excluded_roots: list[str]


class _Topology(NamedTuple):
    """The five HA registry/state views one resolution reads, named instead
    of positionally unpacked -- a reordered ``gather()`` call would otherwise
    mislabel a registry in every downstream error message without mypy or a
    test catching it."""

    states: Any
    entities: Any
    devices: Any
    areas: Any
    floors: Any


@dataclass(frozen=True)
class BulkSelectorResolution:
    """One frozen selector resolution ready for preview or dispatch."""

    resolved_entity_ids: tuple[str, ...]
    excluded_entity_ids: tuple[str, ...]
    selected_area_ids: tuple[str, ...]
    expanded_group_ids: tuple[str, ...]
    hidden_entity_count: int
    warnings: tuple[str, ...] = field(default_factory=tuple)
    _operation_common: dict[str, Any] = field(default_factory=dict)

    @property
    def operations(self) -> list[dict[str, Any]]:
        """Derive the dispatch rows from ``resolved_entity_ids`` on demand.

        Computed rather than stored so ``operations`` and
        ``resolved_entity_ids`` cannot drift apart -- a stored, separately
        constructed ``operations`` list could desync from the entity list
        the dry-run preview reports, silently making the preview disagree
        with what actually dispatches.
        """
        operations: list[dict[str, Any]] = []
        for entity_id in self.resolved_entity_ids:
            row = {"entity_id": entity_id, **self._operation_common}
            if "parameters" in row:
                # `**self._operation_common` only shallow-copies: every row
                # would otherwise share the SAME "parameters" dict object,
                # so an in-place mutation of one row's parameters (e.g. by
                # the dispatcher) would silently leak into every other row.
                row["parameters"] = dict(row["parameters"])
            operations.append(row)
        return operations

    def summary(self) -> dict[str, Any]:
        """Return the stable, visibility-safe resolution report."""
        result: dict[str, Any] = {
            "resolved_entity_ids": list(self.resolved_entity_ids),
            "excluded_entity_ids": list(self.excluded_entity_ids),
            "selected_area_ids": list(self.selected_area_ids),
            "expanded_group_ids": list(self.expanded_group_ids),
            "counts": {
                "resolved": len(self.resolved_entity_ids),
                "excluded": len(self.excluded_entity_ids),
                "groups_expanded": len(self.expanded_group_ids),
                "hidden": self.hidden_entity_count,
            },
        }
        if self.warnings:
            result["warnings"] = list(self.warnings)
        return result


class BulkSelectorValidationError(ValueError):
    """Report a caller-fixable selector error that must prevent every dispatch."""

    def __init__(self, message: str, *, parameter: str = "selector") -> None:
        super().__init__(message)
        self.parameter = parameter


class BulkSelectorInfrastructureError(RuntimeError):
    """Report a Home Assistant infrastructure failure hit while resolving a
    selector -- NOT the caller's fault, and never the caller's to fix by
    editing the selector. Kept a distinct type (not a
    ``BulkSelectorValidationError`` subclass) so ``tools_service.py`` routes
    it through the connection/infrastructure error path instead of
    ``VALIDATION_FAILED``: an agent that sees "selector is invalid" will
    rewrite a selector that was fine and retry against an HA outage no
    selector can fix.
    """


def _registry_rows(result: Any, label: str) -> list[dict[str, Any]]:
    """Unwrap one required Home Assistant registry response."""
    if not isinstance(result, Mapping) or result.get("success") is not True:
        raise BulkSelectorInfrastructureError(f"Home Assistant {label} is unavailable")
    rows = result.get("result")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise BulkSelectorInfrastructureError(
            f"Home Assistant {label} returned an unexpected response"
        )
    return rows


def _validate_device_registry_rows(device_rows: list[dict[str, Any]]) -> None:
    """Fail closed on a device row missing ``id``.

    ``visibility.resolver._parse_device_registry`` silently skips any device
    entry without an ``id`` even when the hidden-set resolver runs
    ``strict=True`` (see ``_load_hidden_entities``), so a malformed row would
    otherwise let a device-derived exclusion (area/label inherited by its
    entities) drop out of the hidden set without warning. Mirrors the
    per-entry validation ``_refresh_hidden_set`` in
    ``visibility/enforcement.py`` applies before calling the same strict
    resolver. Infrastructure-class (HA's own registry data is malformed),
    not the caller's selector to fix.
    """
    if any(not row.get("id") for row in device_rows):
        raise BulkSelectorInfrastructureError(
            "Home Assistant device registry returned a malformed entry"
        )


def _string_list(selector: Mapping[str, Any], key: str) -> list[str]:
    """Read one optional exact-ID list without accepting scalar coercion."""
    raw = selector.get(key, [])
    if not isinstance(raw, list) or not all(
        isinstance(value, str) and value.strip() for value in raw
    ):
        raise BulkSelectorValidationError(
            f"selector.{key} must be a list of non-empty exact identifiers",
            parameter=f"selector.{key}",
        )
    # Strip so a padded id ("light.kitchen " passed the non-empty check
    # above but would otherwise be used verbatim -- entity/area/floor IDs
    # are never legitimately whitespace-padded, so this can only fix a typo,
    # never mask a real identifier.
    return sorted({value.strip() for value in raw})


def _entity_area_id(
    entity_id: str,
    entity_registry: Mapping[str, Mapping[str, Any]],
    device_areas: Mapping[str, str | None],
) -> str | None:
    """Resolve HA's entity-area override followed by device-area inheritance."""
    entry = entity_registry.get(entity_id)
    if not entry:
        return None
    if area_id := entry.get("area_id"):
        return str(area_id)
    device_id = entry.get("device_id")
    return device_areas.get(str(device_id)) if device_id else None


def _expand_entity(
    entity_id: str,
    states: Mapping[str, Mapping[str, Any]],
    *,
    path: tuple[str, ...],
    expanded_groups: set[str],
    cache: dict[str, _ExpansionEntry],
    parameter: str,
) -> set[str]:
    """Expand generic membership recursively and reject incomplete graph walks."""
    if entity_id in path:
        cycle_start = path.index(entity_id)
        cycle = " -> ".join((*path[cycle_start:], entity_id))
        raise BulkSelectorValidationError(
            f"Aggregate membership cycle detected: {cycle}",
            parameter=parameter,
        )
    if cached := cache.get(entity_id):
        expanded_groups.update(cached.groups)
        return set(cached.leaves)
    state = states.get(entity_id)
    if state is None:
        # `path[-1]`, when present, is the aggregate whose member list named
        # this entity_id -- without it, "light.gone does not exist" gives no
        # way to find which entity's member list is stale.
        referenced_by = f", referenced by '{path[-1]}'" if path else ""
        raise BulkSelectorValidationError(
            f"Entity '{entity_id}' does not exist or has no live state{referenced_by}",
            parameter=parameter,
        )
    members = normalize_member_entity_ids(state.get("attributes"))
    if members is None:
        cache[entity_id] = _ExpansionEntry(frozenset({entity_id}), frozenset())
        return {entity_id}
    expanded_groups.add(entity_id)
    if not members:
        cache[entity_id] = _ExpansionEntry(frozenset(), frozenset({entity_id}))
        return set()
    leaves: set[str] = set()
    nested_groups: set[str] = {entity_id}
    for member_id in members:
        member_groups: set[str] = set()
        leaves.update(
            _expand_entity(
                member_id,
                states,
                path=(*path, entity_id),
                expanded_groups=member_groups,
                cache=cache,
                parameter=parameter,
            )
        )
        nested_groups.update(member_groups)
    expanded_groups.update(nested_groups)
    cache[entity_id] = _ExpansionEntry(frozenset(leaves), frozenset(nested_groups))
    return leaves


def _validate_selector(selector: Mapping[str, Any], action: str) -> _ValidatedSelector:
    """Validate the public selector and return normalized exact identifiers."""
    if not isinstance(selector, Mapping):
        raise BulkSelectorValidationError("selector must be a JSON object")
    extra_keys = sorted(set(selector) - _SELECTOR_KEYS)
    if extra_keys:
        raise BulkSelectorValidationError(
            f"selector contains unsupported fields: {', '.join(extra_keys)}"
        )
    domain = selector.get("domain")
    if not isinstance(domain, str) or not domain.strip() or "." in domain:
        raise BulkSelectorValidationError(
            "selector.domain must be one exact Home Assistant domain",
            parameter="selector.domain",
        )
    domain = domain.strip().lower()
    normalized_action = action.strip().lower() if isinstance(action, str) else ""
    valid_actions = get_domain_handler(domain).get(
        "valid_actions", ["on", "off", "toggle"]
    )
    if not normalized_action or normalized_action not in valid_actions:
        raise BulkSelectorValidationError(
            f"Invalid action '{normalized_action}' for domain '{domain}'; "
            f"valid actions: {', '.join(valid_actions)}",
            parameter="action",
        )
    area_ids = _string_list(selector, "area_ids")
    floor_ids = _string_list(selector, "floor_ids")
    if not area_ids and not floor_ids:
        raise BulkSelectorValidationError(
            "selector requires at least one exact area_id or floor_id"
        )
    return _ValidatedSelector(
        domain=domain,
        action=normalized_action,
        area_ids=area_ids,
        floor_ids=floor_ids,
        excluded_roots=_string_list(selector, "exclude_entity_ids"),
    )


async def _load_topology(client: Any) -> _Topology:
    """Load the HA state and registry views used by one resolution.

    ``return_exceptions=True`` plus an explicit re-raise guard, mirroring
    ``tools_areas.py``'s registry fetch: a bare ``asyncio.gather`` lets the
    first exception propagate while the other four awaitables are neither
    cancelled nor retrieved, so their eventual exceptions surface only as an
    unattributed "Task exception was never retrieved" with no tool or call
    context once the event loop garbage-collects them.
    """
    results = await asyncio.gather(
        client.get_states(),
        client.send_websocket_message({"type": "config/entity_registry/list"}),
        client.send_websocket_message({"type": "config/device_registry/list"}),
        client.send_websocket_message({"type": "config/area_registry/list"}),
        client.send_websocket_message({"type": "config/floor_registry/list"}),
        return_exceptions=True,
    )
    for item in results:
        if isinstance(item, BaseException):
            raise item
    return _Topology(*results)


def _select_area_ids(
    area_rows: list[dict[str, Any]],
    floor_rows: list[dict[str, Any]],
    area_ids: list[str],
    floor_ids: list[str],
) -> set[str]:
    """Validate exact topology IDs and expand floors to their areas."""
    areas = {str(row["area_id"]): row for row in area_rows if row.get("area_id")}
    floors = {str(row["floor_id"]) for row in floor_rows if row.get("floor_id")}
    unknown_areas = sorted(set(area_ids) - set(areas))
    unknown_floors = sorted(set(floor_ids) - floors)
    if unknown_areas or unknown_floors:
        details = []
        if unknown_areas:
            details.append(f"unknown area_ids: {', '.join(unknown_areas)}")
        if unknown_floors:
            details.append(f"unknown floor_ids: {', '.join(unknown_floors)}")
        raise BulkSelectorValidationError("; ".join(details))
    selected = set(area_ids)
    selected.update(
        area_id for area_id, row in areas.items() if row.get("floor_id") in floor_ids
    )
    return selected


def _expand_roots(
    roots: list[str],
    states: Mapping[str, Mapping[str, Any]],
    expanded_groups: set[str],
    cache: dict[str, _ExpansionEntry],
    parameter: str,
) -> set[str]:
    """Expand a list of aggregate or leaf roots into a deduplicated leaf set."""
    leaves: set[str] = set()
    for entity_id in roots:
        leaves.update(
            _expand_entity(
                entity_id,
                states,
                path=(),
                expanded_groups=expanded_groups,
                cache=cache,
                parameter=parameter,
            )
        )
    return leaves


async def _load_hidden_entities(
    client: Any,
    entity_result: Any,
    states_result: list[dict[str, Any]],
    device_result: Any,
    entity_registry: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    """Load visibility policy strictly and include registry-hidden entities."""
    try:
        visibility_hidden, _warnings = await load_hidden_set(
            entity_result,
            states_result,
            client,
            device_result,
            strict=True,
        )
    except VisibilityDataUnavailable as exc:
        # Preserve and log the underlying cause: VisibilityDataUnavailable
        # covers four distinct degradations (registry unusable, allowlist
        # registry empty, Assist data unavailable, config load failure) --
        # collapsing them to one fixed sentence here would erase which one
        # actually happened, and nothing else records it server-side.
        logger.warning("bulk selector: visibility resolution failed: %s", exc)
        raise BulkSelectorInfrastructureError(
            f"Entity visibility could not be resolved safely: {exc}"
        ) from exc
    registry_hidden = {
        entity_id
        for entity_id, row in entity_registry.items()
        if row.get("hidden_by") is not None
    }
    return visibility_hidden | registry_hidden


async def resolve_bulk_selector(
    client: Any,
    selector: Mapping[str, Any],
    *,
    action: str,
    parameters: dict[str, Any] | None,
    timeout_seconds: float | None,
    validate_first: bool,
) -> BulkSelectorResolution:
    """Resolve exact HA topology to visible, non-excluded leaf operations.

    All registry/state reads and validation finish before the caller receives any
    operation row, so a malformed selector can never produce a partial dispatch.
    Membership is integration-neutral and uses the shared normalized attributes
    supplied by Home Assistant; no manufacturer, model, or integration heuristics
    apply. The one domain-level exception is ``scene`` (see
    ``_NON_AGGREGATE_ROOT_DOMAINS``): HA core gives that domain's ``entity_id``
    attribute a different meaning (controlled targets, not structural members),
    so it is excluded from aggregate-root admission on architectural grounds,
    not integration identity.
    """
    validated = _validate_selector(selector, action)
    domain, action, area_ids, floor_ids, excluded_roots = validated
    try:
        topology = await _load_topology(client)
    except Exception as exc:
        logger.warning("bulk selector: topology fetch failed: %s", exc)
        raise
    if not isinstance(topology.states, list) or not all(
        isinstance(state, dict) for state in topology.states
    ):
        raise BulkSelectorInfrastructureError(
            "Home Assistant states returned an unexpected response"
        )
    entity_rows = _registry_rows(topology.entities, "entity registry")
    device_rows = _registry_rows(topology.devices, "device registry")
    _validate_device_registry_rows(device_rows)
    selected_areas = _select_area_ids(
        _registry_rows(topology.areas, "area registry"),
        _registry_rows(topology.floors, "floor registry"),
        area_ids,
        floor_ids,
    )
    states = {
        str(state["entity_id"]): state
        for state in topology.states
        if isinstance(state.get("entity_id"), str)
    }
    if not any(entity_id.startswith(f"{domain}.") for entity_id in states):
        raise BulkSelectorValidationError(
            f"No entities of domain '{domain}' exist in this Home Assistant instance",
            parameter="selector.domain",
        )
    entity_registry = {
        str(row["entity_id"]): row
        for row in entity_rows
        if isinstance(row.get("entity_id"), str)
    }
    device_areas = {
        str(row["id"]): row.get("area_id") for row in device_rows if row.get("id")
    }
    hidden = await _load_hidden_entities(
        client, topology.entities, topology.states, topology.devices, entity_registry
    )
    # A root only needs to LIVE in the selected area; it does not need to be
    # of the target domain itself. A `group`/other-domain aggregate assigned
    # to the area (e.g. `group.living_room_lights`) is included here too, so
    # its membership expansion below can surface `light.*` leaves that have
    # no individual area assignment of their own. `scene.*` is excluded (see
    # module docstring note / `_NON_AGGREGATE_ROOT_DOMAINS`): its entity_id
    # attribute lists controlled targets, not structural members, and
    # admitting it here would let a scene assigned to the area pull entities
    # from anywhere in the house into the dispatch. The target-domain filter
    # is re-applied to the expanded leaves further down.
    matching_roots = {
        entity_id
        for entity_id, state in states.items()
        if _entity_area_id(entity_id, entity_registry, device_areas) in selected_areas
        and (
            entity_id.startswith(f"{domain}.")
            or (
                not any(
                    entity_id.startswith(f"{d}.") for d in _NON_AGGREGATE_ROOT_DOMAINS
                )
                and normalize_member_entity_ids(state.get("attributes")) is not None
            )
        )
    }
    if not matching_roots:
        raise BulkSelectorValidationError(
            f"No entities of domain '{domain}' exist in the selected area(s)"
        )
    directly_hidden = matching_roots & hidden
    candidate_roots = sorted(matching_roots - hidden)
    # Separate accumulators: expanded_group_ids in the final resolution must
    # report only aggregates discovered while expanding the SELECTION side.
    # An excluded aggregate (and any of its own nested sub-groups) is not
    # something that fed the dispatch -- reporting it in the same set would
    # misleadingly suggest it did. The memoization cache stays shared: an
    # entity referenced by both sides should still only be expanded once.
    expanded_groups: set[str] = set()
    excluded_expanded_groups: set[str] = set()
    expansion_cache: dict[str, _ExpansionEntry] = {}
    selected_leaves = _expand_roots(
        candidate_roots, states, expanded_groups, expansion_cache, "selector"
    )
    excluded_leaves = _expand_roots(
        excluded_roots,
        states,
        excluded_expanded_groups,
        expansion_cache,
        "selector.exclude_entity_ids",
    )
    selected_leaves = {
        entity_id for entity_id in selected_leaves if entity_id.startswith(f"{domain}.")
    }
    effective_excluded = selected_leaves & excluded_leaves
    selected_leaves.difference_update(excluded_leaves)
    hidden_selected = selected_leaves & hidden
    selected_leaves.difference_update(hidden)
    resolved_entity_ids = sorted(selected_leaves)
    if not resolved_entity_ids:
        raise BulkSelectorValidationError(
            "All matching entities were excluded or hidden"
        )
    if len(resolved_entity_ids) > MAX_SELECTOR_ENTITIES:
        raise BulkSelectorValidationError(
            f"The selector resolved to {len(resolved_entity_ids)} entities; "
            f"the maximum is {MAX_SELECTOR_ENTITIES}"
        )
    operation_common: dict[str, Any] = {
        "action": action,
        "validate_first": validate_first,
    }
    if parameters is not None:
        operation_common["parameters"] = parameters
    if timeout_seconds is not None:
        operation_common["timeout_seconds"] = timeout_seconds
    excluded_and_hidden = effective_excluded & hidden
    hidden_count = len(directly_hidden | hidden_selected | excluded_and_hidden)
    warnings: list[str] = []
    if hidden_count:
        # AGENTS.md "Return Values": a degraded result belongs in the
        # top-level warnings list, not just a bare count the caller has no
        # actionable text for. `counts.hidden` alone cannot tell a user
        # "three entities you wanted controlled were skipped" from "your
        # exclusion was redundant" -- this at least names that something
        # was hidden without leaking which entity (visibility-safe).
        warnings.append(
            f"{hidden_count} matching entit{'y was' if hidden_count == 1 else 'ies were'} "
            "hidden by the entity visibility filter and excluded from this result."
        )
    return BulkSelectorResolution(
        resolved_entity_ids=tuple(resolved_entity_ids),
        excluded_entity_ids=tuple(sorted(effective_excluded - hidden)),
        selected_area_ids=tuple(sorted(selected_areas)),
        expanded_group_ids=tuple(sorted(expanded_groups - hidden)),
        hidden_entity_count=hidden_count,
        warnings=tuple(warnings),
        _operation_common=operation_common,
    )
