"""Keyword-argument entry to the dashboard card search for unit tests."""

from __future__ import annotations

from typing import Any

from ha_mcp.tools.tools_config_dashboards import (
    _find_cards_matching,
    _SearchCriteria,
    _SearchGaps,
)


def _find_cards_in_config(
    config: dict[str, Any],
    entity_id: str | None = None,
    card_type: str | None = None,
    heading: str | None = None,
    truncation: list[str] | None = None,
    uncovered: list[str] | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    return _find_cards_matching(
        config,
        _SearchCriteria(
            entity_id=entity_id, card_type=card_type, heading=heading, query=query
        ),
        _SearchGaps(
            truncation=[] if truncation is None else truncation,
            uncovered=[] if uncovered is None else uncovered,
        ),
    )
