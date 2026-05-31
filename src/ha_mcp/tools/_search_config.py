"""Shared constants and pure helpers for the smart-search modules.

This module imports nothing from the ``ha_mcp.tools`` package so the
smart-search mixins and the public ``smart_search`` shell can both depend
on it without creating an import cycle.
"""

import logging
import os

logger = logging.getLogger(__name__)


# Default concurrency limit for parallel operations
DEFAULT_CONCURRENCY_LIMIT = 20

# Bulk fetch timeouts (in seconds)
BULK_REST_TIMEOUT = 5.0  # Timeout for bulk REST endpoint calls
BULK_WEBSOCKET_TIMEOUT = 3.0  # Timeout for bulk WebSocket calls
INDIVIDUAL_CONFIG_TIMEOUT = 5.0  # Timeout for individual config fetches


# Time budgets for fallback individual fetching (in seconds).
# Configurable via env vars for instances with many automations/scripts.
def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning(f"Invalid value for {key}={raw!r}, using default {default}")
        return default


AUTOMATION_CONFIG_TIME_BUDGET = _env_float("HAMCP_AUTOMATION_CONFIG_TIME_BUDGET", 30.0)
SCRIPT_CONFIG_TIME_BUDGET = _env_float("HAMCP_SCRIPT_CONFIG_TIME_BUDGET", 20.0)
SCENE_CONFIG_TIME_BUDGET = _env_float("HAMCP_SCENE_CONFIG_TIME_BUDGET", 20.0)

# Batch size for parallel individual config fetches (Attempt C fallback)
INDIVIDUAL_FETCH_BATCH_SIZE = 10


def _simplify_states_summary(
    states_summary: dict[str, int],
    detail_level: str,
    max_states: int | None = None,
) -> dict[str, int]:
    """Keep only the most common states, aggregate the rest into _other.

    Args:
        states_summary: Original {state: count} mapping.
        detail_level: "minimal", "standard", or "full".
        max_states: Override cap (None = 5 for minimal, 10 for standard).

    Returns:
        Capped states_summary with ``_other`` count when truncated.
    """
    if detail_level == "full":
        return states_summary

    if max_states is None:
        max_states = 5 if detail_level == "minimal" else 10

    if len(states_summary) <= max_states:
        return states_summary

    sorted_states = sorted(states_summary.items(), key=lambda x: x[1], reverse=True)
    top = dict(sorted_states[:max_states])
    other_count = sum(count for _, count in sorted_states[max_states:])
    if other_count > 0:
        top["_other"] = other_count
    return top
