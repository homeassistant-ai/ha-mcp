"""Pure resolver: compute the hidden entity_id set from a registry list + config."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..utils.data_paths import get_data_dir
from .model import VisibilityConfig
from .persistence import load_visibility_config

logger = logging.getLogger(__name__)


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


async def load_hidden_set(registry_result: object) -> set[str]:
    """Load the visibility config off-loop and resolve the hidden set.

    Fail-open: any error (missing/corrupt config, unexpected exception) returns
    an empty set so a config problem never blanks the instance from the agent.
    """
    try:
        config = await asyncio.to_thread(load_visibility_config, get_data_dir())
        return hidden_entity_ids(registry_result, config)
    except Exception:
        logger.warning(
            "entity visibility config load failed; filter disabled", exc_info=True
        )
        return set()
