"""Pure resolver: compute the hidden entity_id set from a registry list + config."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..utils.data_paths import get_data_dir
from .model import VisibilityConfig
from .persistence import load_visibility_config

logger = logging.getLogger(__name__)

# HA's EntityCategory enum has exactly these two values (homeassistant.const).
# Unknown categories are dropped with a warning rather than hard-rejected, so a
# future HA category can never turn a config into a load failure that fails the
# whole filter open; the drop is surfaced through the response warnings channel.
KNOWN_ENTITY_CATEGORIES = frozenset({"config", "diagnostic"})

_REGISTRY_UNAVAILABLE_WARNING = (
    "Entity visibility filter is enabled but the entity registry was "
    "unavailable; results are unfiltered (the denylist still applies)."
)


def hidden_entity_ids(
    registry_result: object, config: VisibilityConfig
) -> tuple[set[str], list[str]]:
    """Return ``(hidden_entity_ids, warnings)``.

    ``hidden`` is empty when disabled or the registry payload is unusable
    (fail-open — never hide on bad input), except the denylist which needs no
    registry data and is honored regardless. ``warnings`` carries operator-facing
    notes (degraded registry, dropped unknown categories) for the caller to
    surface at the response level.
    """
    if not config.enabled:
        return set(), []

    warnings: list[str] = []
    # Categories are validated independently of the registry: an unknown value is
    # dropped with a warning (not hard-rejected), so a typo silently hides nothing
    # yet is still surfaced.
    requested_categories = set(config.exclude_categories)
    categories = requested_categories & KNOWN_ENTITY_CATEGORIES
    unknown_categories = requested_categories - KNOWN_ENTITY_CATEGORIES
    if unknown_categories:
        warnings.append(
            "Entity visibility: ignoring unknown exclude_categories "
            f"{sorted(unknown_categories)} (valid: config, diagnostic)."
        )

    denied = set(config.deny_entity_ids)

    # Enabled past this point, so an unusable registry is a real degradation the
    # operator should see as a warning. Honor the denylist regardless (it needs no
    # registry data); only the registry-derived dimensions degrade to open.
    if not isinstance(registry_result, dict) or not registry_result.get("success"):
        logger.warning(
            "entity visibility filter enabled but the registry payload was "
            "unusable; degrading to unfiltered for this request"
        )
        warnings.append(_REGISTRY_UNAVAILABLE_WARNING)
        return set(denied), warnings

    areas = set(config.exclude_areas)
    labels = set(config.exclude_labels)

    # Seed with the explicit denylist: deny is a literal entity_id match and must
    # hide an entity even when it has no entity-registry entry (legacy YAML /
    # template entities live only in states, not the registry). The
    # registry-derived dimensions below still require a registry entry. Because
    # deny needs no registry data, the degraded early returns (above and below)
    # still honor it (fail-fully-open was not the goal); only the
    # registry-derived dimensions degrade to open on a bad read.
    hidden: set[str] = set(denied)
    entries: Any = registry_result.get("result", [])
    if not isinstance(entries, list):
        logger.warning(
            "entity visibility filter enabled but the registry 'result' was not "
            "a list; degrading to unfiltered for this request"
        )
        warnings.append(_REGISTRY_UNAVAILABLE_WARNING)
        return set(denied), warnings
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
    return hidden, warnings


async def load_hidden_set(registry_result: object) -> tuple[set[str], list[str]]:
    """Load the visibility config off-loop and resolve ``(hidden, warnings)``.

    Fail-open: any error (missing/corrupt config, unexpected exception) yields an
    empty hidden set so a config problem never blanks the instance from the
    agent; a genuine load failure is surfaced as a warning.
    """
    try:
        config = await asyncio.to_thread(load_visibility_config, get_data_dir())
        return hidden_entity_ids(registry_result, config)
    except Exception:
        logger.warning(
            "entity visibility config load failed; filter disabled", exc_info=True
        )
        return set(), [
            "Entity visibility config could not be loaded; the filter is disabled."
        ]


def merge_visibility_warnings(
    response: dict[str, Any], warnings: list[str]
) -> dict[str, Any]:
    """Attach visibility ``warnings`` to a tool response's top-level ``warnings``
    list (create-or-extend). Returns ``response`` for ``return`` composition."""
    if warnings:
        response.setdefault("warnings", []).extend(warnings)
    return response
