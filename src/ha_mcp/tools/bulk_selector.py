"""Resolve deterministic structural selectors into exact bulk-control leaves."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..utils.domain_handlers import get_domain_handler
from ..utils.entity_membership import normalize_member_entity_ids
from ..visibility.resolver import VisibilityDataUnavailable, load_hidden_set

MAX_SELECTOR_ENTITIES = 100
_SELECTOR_KEYS = {"domain", "area_ids", "floor_ids", "exclude_entity_ids"}


@dataclass(frozen=True)
class BulkSelectorResolution:
    """One frozen selector resolution ready for preview or dispatch."""

    operations: list[dict[str, Any]]
    resolved_entity_ids: list[str]
    excluded_entity_ids: list[str]
    selected_area_ids: list[str]
    expanded_group_ids: list[str]
    hidden_entity_count: int

    def summary(self) -> dict[str, Any]:
        """Return the stable, visibility-safe resolution report."""
        return {
            "resolved_entity_ids": self.resolved_entity_ids,
            "excluded_entity_ids": self.excluded_entity_ids,
            "selected_area_ids": self.selected_area_ids,
            "expanded_group_ids": self.expanded_group_ids,
            "counts": {
                "resolved": len(self.resolved_entity_ids),
                "excluded": len(self.excluded_entity_ids),
                "groups_expanded": len(self.expanded_group_ids),
                "hidden": self.hidden_entity_count,
            },
        }


class BulkSelectorValidationError(ValueError):
    """Report a selector error that must prevent every dispatch."""

    def __init__(self, message: str, *, parameter: str = "selector") -> None:
        super().__init__(message)
        self.parameter = parameter


def _registry_rows(result: Any, label: str) -> list[dict[str, Any]]:
    """Unwrap one required Home Assistant registry response."""
    if not isinstance(result, Mapping) or result.get("success") is not True:
        raise BulkSelectorValidationError(f"Home Assistant {label} is unavailable")
    rows = result.get("result")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise BulkSelectorValidationError(
            f"Home Assistant {label} returned an unexpected response"
        )
    return rows


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
    return sorted(set(raw))


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
    cache: dict[str, tuple[frozenset[str], frozenset[str]]],
    parameter: str,
) -> set[str]:
    """Expand generic membership recursively and reject incomplete graph walks."""
    if entity_id in path:
        cycle_start = path.index(entity_id)
        cycle = " -> ".join((*path[cycle_start:], entity_id))
        raise BulkSelectorValidationError(
            f"Aggregate membership cycle detected: {cycle}"
        )
    if cached := cache.get(entity_id):
        cached_leaves, cached_groups = cached
        expanded_groups.update(cached_groups)
        return set(cached_leaves)
    state = states.get(entity_id)
    if state is None:
        raise BulkSelectorValidationError(
            f"Entity '{entity_id}' does not exist or has no live state",
            parameter=parameter,
        )
    members = normalize_member_entity_ids(state.get("attributes"))
    if members is None:
        cache[entity_id] = (frozenset({entity_id}), frozenset())
        return {entity_id}
    expanded_groups.add(entity_id)
    if not members:
        cache[entity_id] = (frozenset(), frozenset({entity_id}))
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
    cache[entity_id] = (frozenset(leaves), frozenset(nested_groups))
    return leaves


def _validate_selector(
    selector: Mapping[str, Any], action: str
) -> tuple[str, str, list[str], list[str], list[str]]:
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
    return (
        domain,
        normalized_action,
        area_ids,
        floor_ids,
        _string_list(selector, "exclude_entity_ids"),
    )


async def _load_topology(client: Any) -> tuple[Any, Any, Any, Any, Any]:
    """Load the HA state and registry views used by one resolution."""
    return await asyncio.gather(
        client.get_states(),
        client.send_websocket_message({"type": "config/entity_registry/list"}),
        client.send_websocket_message({"type": "config/device_registry/list"}),
        client.send_websocket_message({"type": "config/area_registry/list"}),
        client.send_websocket_message({"type": "config/floor_registry/list"}),
    )


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
    cache: dict[str, tuple[frozenset[str], frozenset[str]]],
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
        raise BulkSelectorValidationError(
            "Entity visibility could not be resolved safely"
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
    supplied by Home Assistant; no manufacturer, model, or domain heuristics apply.
    """
    domain, action, area_ids, floor_ids, excluded_roots = _validate_selector(
        selector, action
    )
    (
        states_result,
        entity_result,
        device_result,
        area_result,
        floor_result,
    ) = await _load_topology(client)
    if not isinstance(states_result, list) or not all(
        isinstance(state, dict) for state in states_result
    ):
        raise BulkSelectorValidationError(
            "Home Assistant states returned an unexpected response"
        )
    entity_rows = _registry_rows(entity_result, "entity registry")
    device_rows = _registry_rows(device_result, "device registry")
    selected_areas = _select_area_ids(
        _registry_rows(area_result, "area registry"),
        _registry_rows(floor_result, "floor registry"),
        area_ids,
        floor_ids,
    )
    states = {
        str(state["entity_id"]): state
        for state in states_result
        if isinstance(state.get("entity_id"), str)
    }
    entity_registry = {
        str(row["entity_id"]): row
        for row in entity_rows
        if isinstance(row.get("entity_id"), str)
    }
    device_areas = {
        str(row["id"]): row.get("area_id") for row in device_rows if row.get("id")
    }
    hidden = await _load_hidden_entities(
        client, entity_result, states_result, device_result, entity_registry
    )
    matching_roots = {
        entity_id
        for entity_id in states
        if entity_id.startswith(f"{domain}.")
        and _entity_area_id(entity_id, entity_registry, device_areas) in selected_areas
    }
    directly_hidden = matching_roots & hidden
    candidate_roots = sorted(matching_roots - hidden)
    expanded_groups: set[str] = set()
    expansion_cache: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    selected_leaves = _expand_roots(
        candidate_roots, states, expanded_groups, expansion_cache, "selector"
    )
    excluded_leaves = _expand_roots(
        excluded_roots,
        states,
        expanded_groups,
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
            "The selector resolved to no visible leaf entities after exclusions"
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
    operations = [
        {"entity_id": entity_id, **operation_common}
        for entity_id in resolved_entity_ids
    ]
    return BulkSelectorResolution(
        operations=operations,
        resolved_entity_ids=resolved_entity_ids,
        excluded_entity_ids=sorted(effective_excluded - hidden),
        selected_area_ids=sorted(selected_areas),
        expanded_group_ids=sorted(expanded_groups - hidden),
        hidden_entity_count=len(
            directly_hidden | hidden_selected | (effective_excluded & hidden)
        ),
    )
