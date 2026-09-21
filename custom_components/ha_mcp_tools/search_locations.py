"""Resolve search locations from live registries without a server dependency.

Matching mirrors smart_search._entities.EntitySearchMixin._resolve_area_query;
the component also ships independently of the ha_mcp Python package.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


@dataclass
class SearchLocation:
    """Resolved areas and the evidence needed to interpret incomplete matches."""

    area_ids: set[str]
    area_names: list[str]
    warnings: list[str]
    unavailable: list[str]

    def add_unavailable(self, names: set[str]) -> None:
        """Record registry failures discovered while applying the filter."""
        added = sorted(names.difference(self.unavailable))
        if not added:
            return
        self.unavailable.extend(added)
        self.warnings.append(
            "Location filtering is incomplete: unavailable "
            f"{', '.join(added)} registry data; matches may be missing."
        )


def add_location_metadata(
    result: dict[str, Any], location: SearchLocation | None
) -> dict[str, Any]:
    """Attach resolution evidence without hiding another surface's degradation."""
    if location is None:
        return result
    result["area_names"] = location.area_names
    if location.warnings:
        result.setdefault("warnings", []).extend(location.warnings)
    if location.unavailable:
        result["partial"] = True
        reason = "Location registry data unavailable: " + ", ".join(
            location.unavailable
        )
        result["partial_reason"] = " ; ".join(
            part for part in (result.get("partial_reason"), reason) if part
        )
        result.setdefault("diagnostics", {})["location_registries_unavailable"] = len(
            location.unavailable
        )
    return result


def add_registry_failures(
    location: SearchLocation | None,
    names: set[str],
    diagnostics: dict[str, int],
    partial_reasons: list[str],
) -> None:
    """Report accessor failures for filtered and unfiltered entity searches."""
    if location is not None:
        location.add_unavailable(names)
    elif names:
        partial_reasons.append(
            "Entity search registry data unavailable: " + ", ".join(sorted(names))
        )
        diagnostics["entity_registries_unavailable"] = len(names)


def resolve_search_location(view: Any, query: str) -> SearchLocation:
    """Resolve an area or floor once for the entire search result window."""
    areas, areas_available = _registry_rows(view.area, "area")
    floors, floors_available = _registry_rows(view.floor, "floor")
    unavailable = [
        name
        for name, available in (
            ("area", areas_available),
            ("floor", floors_available),
            ("entity", view.entity is not None),
            ("device", view.device is not None),
        )
        if not available
    ]
    area_ids, warnings = _resolve_area_query(
        areas, floors, query, floor_registry_available=floors_available
    )
    location = SearchLocation(
        area_ids=area_ids,
        area_names=sorted(
            str(areas[area_id].get("name") or area_id) for area_id in area_ids
        ),
        warnings=warnings,
        unavailable=[],
    )
    location.add_unavailable(set(unavailable))
    return location


def _registry_rows(registry: Any, kind: str) -> tuple[dict[str, dict[str, Any]], bool]:
    """Distinguish an empty registry from an unavailable or broken accessor."""
    if registry is None:
        return {}, False
    try:
        lister = getattr(registry, f"async_list_{kind}s", None)
        if callable(lister):
            entries = list(lister())
        else:
            mapping = getattr(registry, f"{kind}s", None)
            if not isinstance(mapping, Mapping):
                return {}, False
            entries = list(mapping.values())
        return dict(_registry_row(entry, kind) for entry in entries), True
    except Exception:
        # This is a boundary around HA's evolving registry accessors. Preserve
        # the unavailable signal rather than converting an outage to no matches.
        return {}, False


def _registry_row(entry: Any, kind: str) -> tuple[str, dict[str, Any]]:
    registry_id = getattr(entry, f"{kind}_id", None) or getattr(entry, "id", None)
    if not isinstance(registry_id, str) or not registry_id:
        raise ValueError(f"Invalid {kind} registry ID")
    return registry_id, {
        f"{kind}_id": registry_id,
        "name": getattr(entry, "name", None),
        "aliases": getattr(entry, "aliases", None) or [],
        **({"floor_id": getattr(entry, "floor_id", None)} if kind == "area" else {}),
    }


def _values(registry_id: str, row: dict[str, Any]) -> list[str]:
    return [
        registry_id.lower(),
        str(row.get("name") or "").lower(),
        *(alias.lower() for alias in row.get("aliases", []) if isinstance(alias, str)),
    ]


def _ratio(query: str, value: str) -> int:
    return int(SequenceMatcher(None, query, value, autojunk=False).ratio() * 100)


def _partial_ratio(query: str, value: str) -> int:
    if not query or not value:
        return 0
    shorter, longer = sorted((query, value), key=len)
    return max(
        _ratio(shorter, longer[start : start + len(shorter)])
        for start in range(len(longer) - len(shorter) + 1)
    )


def _best_matches(
    registry: dict[str, dict[str, Any]], query: str
) -> tuple[set[str], int]:
    scores = {
        registry_id: max(
            (_ratio(query, value) for value in _values(registry_id, row) if value),
            default=0,
        )
        for registry_id, row in registry.items()
    }
    best = max(scores.values(), default=0)
    return (
        {key for key, score in scores.items() if score == best} if best >= 80 else set()
    ), best


def _resolve_area_query(
    areas: dict[str, dict[str, Any]],
    floors: dict[str, dict[str, Any]],
    query: str,
    *,
    floor_registry_available: bool,
) -> tuple[set[str], list[str]]:
    """Prefer exact areas, then exact/unique close floors, then fuzzy areas."""
    normalized = query.lower().strip()
    exact_areas = {key for key, row in areas.items() if normalized in _values(key, row)}
    exact_floors = {
        key for key, row in floors.items() if normalized in _values(key, row)
    }
    if exact_areas:
        warnings = (
            [
                f"'{query}' matches both an area and a floor; the exact area match was used."
            ]
            if exact_floors
            else []
        )
        return exact_areas, warnings
    if exact_floors:
        return _expand_floors(areas, floors, exact_floors, query, fuzzy=False)
    if not floor_registry_available:
        return set(), [
            f"Floor data is unavailable, so '{query}' was matched only "
            "against exact area IDs, names, and aliases; close-spelling "
            "matching was skipped. Retry, or pass an exact area_id from "
            "ha_list_floors_areas."
        ]
    near_floors, floor_score = _best_matches(floors, normalized)
    _, area_score = _best_matches(areas, normalized)
    if len(near_floors) > 1 and floor_score > area_score:
        names = sorted(str(floors[key].get("name") or key) for key in near_floors)
        return set(), [
            f"'{query}' ambiguously matches multiple floors: "
            f"{', '.join(names)}. Use an exact floor name or ID."
        ]
    if len(near_floors) == 1 and floor_score > area_score:
        return _expand_floors(areas, floors, near_floors, query, fuzzy=True)
    return {
        key
        for key, row in areas.items()
        if any(_partial_ratio(normalized, value) >= 80 for value in _values(key, row))
    }, []


def _expand_floors(
    areas: dict[str, dict[str, Any]],
    floors: dict[str, dict[str, Any]],
    floor_ids: set[str],
    query: str,
    *,
    fuzzy: bool,
) -> tuple[set[str], list[str]]:
    area_ids = {key for key, row in areas.items() if row.get("floor_id") in floor_ids}
    names = ", ".join(str(floors[key].get("name") or key) for key in sorted(floor_ids))
    suffix = " from a close spelling" if fuzzy else ""
    return area_ids, [
        f"'{query}' is a floor, not an area — expanded to "
        f"{len(area_ids)} area(s) on the matched floor(s) ({names}){suffix}."
    ]
