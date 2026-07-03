"""Pure resolver: compute the hidden entity_id set from a registry list + config."""

from __future__ import annotations

from typing import Any

from .model import VisibilityConfig


def hidden_entity_ids(registry_result: object, config: VisibilityConfig) -> set[str]:
    """Return the set of entity_ids to hide. Empty when disabled or the
    registry payload is unusable (fail-open — never hide on bad input)."""
    if not config.enabled:
        return set()
    if not isinstance(registry_result, dict) or not registry_result.get("success"):
        return set()

    categories = set(config.exclude_categories)
    denied = set(config.deny_entity_ids)
    areas = set(config.exclude_areas)
    labels = set(config.exclude_labels)

    hidden: set[str] = set()
    entries: Any = registry_result.get("result", [])
    if not isinstance(entries, list):
        return set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        eid = entry.get("entity_id")
        if not eid:
            continue
        if eid in denied:
            hidden.add(eid)
            continue
        if categories and entry.get("entity_category") in categories:
            hidden.add(eid)
            continue
        if config.exclude_hidden and entry.get("hidden_by") is not None:
            hidden.add(eid)
            continue
        if areas and entry.get("area_id") in areas:
            hidden.add(eid)
            continue
        if labels and labels.intersection(entry.get("labels") or []):
            hidden.add(eid)
    return hidden
