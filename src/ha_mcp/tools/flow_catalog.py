"""What Home Assistant can create: the helper types and integrations behind
its Create helper and Add integration dialogs.

Both are read from Core's ``integration/descriptions``, the frontend's
catalog, as Core's own ``config__search_integrations`` tool reads it: Core's
and the custom integrations' entries (a brand's integrations included), each
with its name and ``categories`` as Core sends them (``helpers.calculate_value``,
``energy.energy_prices``), and a name Core translates read from its ``title``
strings.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..client.rest_client import HomeAssistantConnectionError
from .config_helpers.schemas import SIMPLE_HELPER_TYPES
from .flow_translations import configured_language, fetch_flow_strings

_Row = tuple[str, dict[str, Any], str]


async def _descriptions(client: Any) -> dict[str, Any]:
    answer = await client.send_websocket_message({"type": "integration/descriptions"})
    result = answer.get("result") if isinstance(answer, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("core"), dict):
        raise HomeAssistantConnectionError(
            "integration/descriptions returned an unexpected response shape: "
            f"{answer!r}"[:300]
        )
    return result


def _entries(descriptions: dict[str, Any], kind: str) -> Iterator[_Row]:
    """``(domain, metadata, brand)`` for each ``kind`` (``integration`` or
    ``helper``) entry, Core's and the custom ones."""
    for origin in ("core", "custom"):
        entries = (descriptions.get(origin) or {}).get(kind) or {}
        for key, meta in entries.items():
            if not isinstance(meta, dict):
                continue
            if "integration_type" in meta or kind == "helper":
                yield key, meta, ""
            elif isinstance(sub := meta.get("integrations"), dict):
                # A brand groups its integrations, as the Add dialog shows them.
                brand = str(meta.get("name", key)).casefold()
                yield from (
                    (d, m, brand) for d, m in sub.items() if isinstance(m, dict)
                )


async def _names(
    client: Any, descriptions: dict[str, Any], rows: list[_Row]
) -> tuple[dict[str, str], list[str]]:
    """The name Core shows for each row, and a warning when the names Core
    translates could not be read."""
    translated = set((descriptions.get("core") or {}).get("translated_name") or ())
    wanted = sorted({d for d, meta, _ in rows if not meta.get("name")} & translated)
    titles: dict[str, Any] = {}
    warnings: list[str] = []
    if wanted:
        language, unread = await configured_language(client, "Names")
        found = await fetch_flow_strings(client, wanted, "title", language=language)
        if unread:
            warnings.append(unread)
        if found is None:
            warnings.append(
                "Home Assistant's translated names could not be read; those "
                "rows are named by their domain"
            )
        titles = found or {}
    names = {
        d: str(meta.get("name") or titles.get(f"component.{d}.title") or d)
        for d, meta, _ in rows
    }
    return names, warnings


def _in_category(meta: dict[str, Any], category: str | None) -> bool:
    """A category matches itself and its subcategories, as Core's search
    matches a top-level category (``helpers`` finds ``helpers.store_value``)."""
    if category is None:
        return True
    categories = meta.get("categories") or []
    return any(
        c == category or str(c).partition(".")[0] == category for c in categories
    )


def _rank(query: str, domain: str, name: str, brand: str) -> int | None:
    """Core's search rank: 0 an exact domain or name, 1 every word in the
    domain or name, 2 every word there or in the brand; ``None`` no match."""
    head = f"{domain} {name}".replace("_", " ")
    words = query.replace("_", " ").split()
    if not all(word in f"{head} {brand}" for word in words):
        return None
    if query in (domain, name):
        return 0
    return 1 if all(word in head for word in words) else 2


def _category_hint(rows: list[_Row], category: str | None) -> list[str]:
    if category is None:
        return []
    known = sorted(
        {
            str(c).partition(".")[0]
            for _, meta, _ in rows
            for c in meta.get("categories") or []
        }
    )
    return [f"No entry has category {category!r}. Categories here: {', '.join(known)}"]


async def helper_catalog(
    client: Any, helper_type: str, category: str | None
) -> dict[str, Any]:
    """The helper types ha_config_set_helper can create (a helper flow, or a
    storage helper), ``helper_type`` one of them or ``all``."""
    descriptions = await _descriptions(client)
    rows = [
        row
        for row in _entries(descriptions, "helper")
        if row[1].get("config_flow") or row[0] in SIMPLE_HELPER_TYPES
    ]
    matched = [
        row
        for row in rows
        if _in_category(row[1], category) and helper_type in ("all", row[0])
    ]
    names, warnings = await _names(client, descriptions, matched)
    catalog = sorted(
        (
            {
                "helper_type": domain,
                "name": names[domain],
                "categories": meta.get("categories") or [],
                "config_flow": bool(meta.get("config_flow")),
            }
            for domain, meta, _ in matched
        ),
        key=lambda row: str(row["name"]).casefold(),
    )
    if not catalog:
        warnings += _category_hint(rows, category)
    response: dict[str, Any] = {
        "success": True,
        "count": len(catalog),
        "catalog": catalog,
    }
    if warnings:
        response["warnings"] = warnings
    return response


async def integration_catalog(
    client: Any,
    *,
    query: str | None,
    category: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """The integrations set up through a config flow, matched by ``query``
    against the domain, name and brand as Core's search matches, one page."""
    descriptions = await _descriptions(client)
    seen: set[str] = set()
    rows: list[_Row] = []
    for row in _entries(descriptions, "integration"):
        if row[0] not in seen and row[1].get("config_flow"):
            seen.add(row[0])
            rows.append(row)
    in_category = [row for row in rows if _in_category(row[1], category)]
    names, warnings = await _names(client, descriptions, in_category)
    needle = (query or "").strip().casefold()
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for domain, meta, brand in in_category:
        name = names[domain]
        rank = _rank(needle, domain, name.casefold(), brand) if needle else 0
        if rank is not None:
            entry = {
                "domain": domain,
                "name": name,
                "categories": meta.get("categories") or [],
            }
            ranked.append((rank, name.casefold(), entry))
    ranked.sort(key=lambda item: item[:2])
    page = [row for _, _, row in ranked[offset : offset + limit]]
    if not ranked:
        warnings += _category_hint(rows, category)
    response: dict[str, Any] = {
        "success": True,
        "total_count": len(ranked),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(ranked),
        "catalog": page,
    }
    if warnings:
        response["warnings"] = warnings
    return response
