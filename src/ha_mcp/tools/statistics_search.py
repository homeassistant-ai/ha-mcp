"""The ``query`` of ``ha_manage_logs(action="recorder/list_statistic_ids")``.

Core has no search over its statistics listing: like the Marketplace search of
``ha_get_hacs_info``, each word of the query must be a substring of one of a
row's values, and with the component also of the names Core gives the
statistic's entity (``er.async_get_entity_aliases``).
"""

from __future__ import annotations

from typing import Any

from ..client.rest_client import HomeAssistantConnectionError
from .component_api import ask_component, component_unusable_warning, get_component_caps


def _texts(row: dict[str, Any], aliases: list[str]) -> list[str]:
    values = [value for value in row.values() if isinstance(value, str)]
    return [text.lower() for text in (*values, *aliases)]


async def matching_statistics(
    client: Any, rows: list[dict[str, Any]], query: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """The rows every word of ``query`` matches, and a warning when the
    entities' aliases could not be searched."""
    terms = query.lower().split()
    if not terms:
        return rows, []
    try:
        reply = await ask_component(
            client, "entity_aliases", entity_ids=[row["statistic_id"] for row in rows]
        )
    except HomeAssistantConnectionError:
        reply = None  # a failed component read counts as no component
    warnings: list[str] = []
    if reply is None:
        warnings.append(
            component_unusable_warning(
                await get_component_caps(client),
                "the query did not search the entities' aliases",
            )
        )
    aliases: dict[str, list[str]] = (reply or {}).get("aliases") or {}
    matches = [
        row
        for row in rows
        if all(
            any(
                term in text
                for text in _texts(row, aliases.get(row["statistic_id"], []))
            )
            for term in terms
        )
    ]
    return matches, warnings
