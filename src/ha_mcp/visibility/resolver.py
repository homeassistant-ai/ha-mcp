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
    # Enabled past this point, so an unusable registry is a real degradation the
    # operator should see (spec §7: degrade with a warning). Fail open regardless.
    if not isinstance(registry_result, dict) or not registry_result.get("success"):
        logger.warning(
            "entity visibility filter enabled but the registry payload was "
            "unusable; degrading to unfiltered for this request"
        )
        return set()

    categories = set(config.exclude_categories)
    denied = set(config.deny_entity_ids)
    areas = set(config.exclude_areas)
    labels = set(config.exclude_labels)

    # Seed with the explicit denylist: deny is a literal entity_id match and must
    # hide an entity even when it has no entity-registry entry (legacy YAML /
    # template entities live only in states, not the registry). The
    # registry-derived dimensions below still require a registry entry. On a
    # failed registry read the two early returns above still fail open — a
    # transient outage is not when a denylist should suddenly start mattering.
    hidden: set[str] = set(denied)
    entries: Any = registry_result.get("result", [])
    if not isinstance(entries, list):
        logger.warning(
            "entity visibility filter enabled but the registry 'result' was not "
            "a list; degrading to unfiltered for this request"
        )
        return set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        eid = entry.get("entity_id")
        if not eid:
            continue
        if eid in denied:
            continue  # already hidden via the seed above
        if categories and entry.get("entity_category") in categories:
            hidden.add(eid)
            continue
        if config.exclude_hidden and entry.get("hidden_by") is not None:
            hidden.add(eid)
            continue
        if areas and entry.get("area_id") in areas:
            hidden.add(eid)
            continue
        entry_labels = entry.get("labels") or []
        if isinstance(entry_labels, str):
            # A label served as a bare string (unexpected payload / mock) must
            # count as one label, not be char-iterated by set.intersection —
            # else a single-char exclude entry could spuriously hide it.
            entry_labels = [entry_labels]
        elif not isinstance(entry_labels, (list, tuple, set)):
            # An unexpected non-iterable payload (int/dict/…) skips just this
            # entry's label check instead of raising and fail-open-disabling the
            # whole filter for every other entity.
            entry_labels = []
        if labels and labels.intersection(entry_labels):
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
