"""What Home Assistant can create: the helper types and integrations behind
its Create helper and Add integration dialogs.

Both are read from Core's ``integration/descriptions``, the frontend's
catalog: Core's and the custom integrations' entries (a brand's integrations
included), each with its name and ``categories`` as Core sends them
(``helpers.calculate_value``, ``energy.energy_prices``), and a name Core
translates read from its ``title`` strings. Matching a query, a category and
paging are this server's own, as in ``ha_get_integration``'s list mode.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, NamedTuple, NoReturn

from ..errors import ErrorCode, create_error_response
from .config_helpers.schemas import SIMPLE_HELPER_TYPES
from .flow_translations import configured_language, fetch_flow_strings
from .helper_flows import integration_descriptions
from .helpers import raise_tool_error

# ha_get_integration's parameters that list or read configured entries, which
# a catalog call does not.
CATALOG_UNUSED = (
    "entry_id",
    "domain",
    "integration_type",
    "include_options",
    "include_schema",
    "include_subentries",
    "include_subentry_schema",
    "subentry_type",
    "subentry_id",
    "include_diagnostics",
    "include_ws_commands",
    "device_id",
)


class _Row(NamedTuple):
    domain: str
    meta: dict[str, Any]


def filter_by_query(
    entries: list[dict[str, Any]],
    query: str,
    exact_match: bool | None,
    title_key: str = "title",
) -> list[dict[str, Any]]:
    """Entries whose domain or ``title_key`` holds ``query``, or with
    ``exact_match`` off one of them close to it, best match first."""
    matches: list[tuple[int, dict[str, Any]]] = []
    query_lower = query.strip().lower()

    for entry in entries:
        domain_lower = (entry.get("domain") or "").lower()
        title_lower = (entry.get(title_key) or "").lower()

        # Check for exact substring matches first (highest priority)
        if query_lower in domain_lower or query_lower in title_lower:
            matches.append((100, entry))
        elif not exact_match:
            # Fuzzy matching only when exact_match is disabled
            from ..utils.fuzzy_search import calculate_ratio

            domain_score = calculate_ratio(query_lower, domain_lower)
            title_score = calculate_ratio(query_lower, title_lower)
            best_score = max(domain_score, title_score)

            if best_score >= 70:  # threshold for fuzzy matches
                matches.append((best_score, entry))

    # Sort by score descending
    matches.sort(key=lambda x: x[0], reverse=True)
    return [match[1] for match in matches]


def _entries(descriptions: dict[str, Any], kind: str) -> Iterator[_Row]:
    """Each ``kind`` (``integration`` or ``helper``) entry, the custom ones
    first: a custom integration replaces Core's of its domain, and Core drops
    only a top-level entry for it, not one nested under a brand."""
    for origin in ("custom", "core"):
        entries = (descriptions.get(origin) or {}).get(kind) or {}
        for key, meta in entries.items():
            if not isinstance(meta, dict):
                continue
            if "integration_type" in meta or kind == "helper":
                yield _Row(key, meta)
            elif isinstance(sub := meta.get("integrations"), dict):
                # A brand groups its integrations, as the Add dialog shows them.
                yield from (_Row(d, m) for d, m in sub.items() if isinstance(m, dict))


async def _names(
    client: Any, descriptions: dict[str, Any], rows: list[_Row]
) -> tuple[dict[str, str], list[str]]:
    """The name Core shows for each row, and a warning when the names Core
    translates could not be read."""
    translated = set((descriptions.get("core") or {}).get("translated_name") or ())
    wanted = sorted({r.domain for r in rows if not r.meta.get("name")} & translated)
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
        r.domain: str(
            r.meta.get("name") or titles.get(f"component.{r.domain}.title") or r.domain
        )
        for r in rows
    }
    return names, warnings


def _in_category(row: _Row, category: str | None) -> bool:
    """A category matches itself and its subcategories (``helpers`` finds
    ``helpers.store_value``); a subcategory matches only itself. This is the
    server's own filter: Core's search matches a top-level category only."""
    if category is None:
        return True
    categories = row.meta.get("categories") or []
    return any(
        c == category or str(c).partition(".")[0] == category for c in categories
    )


def _category_hint(rows: list[_Row], category: str | None) -> list[str]:
    """Name the categories there are, when ``category`` itself matches none."""
    if category is None or any(_in_category(r, category) for r in rows):
        return []
    known = sorted(
        {str(c).partition(".")[0] for r in rows for c in r.meta.get("categories") or []}
    )
    return [f"No entry has category {category!r}. Categories here: {', '.join(known)}"]


async def helper_catalog(
    client: Any, helper_type: str, category: str | None
) -> dict[str, Any]:
    """The helper types Home Assistant's Create helper dialog offers that
    ha_config_set_helper creates (a helper flow, or a storage helper),
    ``helper_type`` one of them or ``all``."""
    descriptions = await integration_descriptions(client)
    rows = [
        r
        for r in _entries(descriptions, "helper")
        if r.meta.get("config_flow") or r.domain in SIMPLE_HELPER_TYPES
    ]
    matched = [
        r
        for r in rows
        if _in_category(r, category) and helper_type in ("all", r.domain)
    ]
    names, warnings = await _names(client, descriptions, matched)
    catalog = sorted(
        (
            {
                "helper_type": r.domain,
                "name": names[r.domain],
                "categories": r.meta.get("categories") or [],
                "config_flow": bool(r.meta.get("config_flow")),
            }
            for r in matched
        ),
        key=lambda row: str(row["name"]).casefold(),
    )
    warnings += _category_hint(rows, category)
    if helper_type != "all" and not any(r.domain == helper_type for r in rows):
        warnings.append(
            f"{helper_type} is not in Home Assistant's Create helper dialog; "
            "ha_config_set_helper creates it all the same."
        )
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
    exact_match: bool,
    category: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """The integrations set up through a config flow, by name, matched by
    ``query`` as ``ha_get_integration``'s list mode matches, one page."""
    descriptions = await integration_descriptions(client)
    seen: set[str] = set()
    rows: list[_Row] = []
    for row in _entries(descriptions, "integration"):
        # A domain can sit under more than one brand; its first row wins.
        if row.domain in seen:
            continue
        seen.add(row.domain)
        if row.meta.get("config_flow"):
            rows.append(row)
    in_category = [r for r in rows if _in_category(r, category)]
    names, warnings = await _names(client, descriptions, in_category)
    listed = sorted(
        (
            {
                "domain": r.domain,
                "name": names[r.domain],
                "categories": r.meta.get("categories") or [],
            }
            for r in in_category
        ),
        key=lambda row: str(row["name"]).casefold(),
    )
    if query and query.strip():
        listed = filter_by_query(listed, query, exact_match, title_key="name")
    warnings += _category_hint(rows, category)
    response: dict[str, Any] = {
        "success": True,
        "total_count": len(listed),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(listed),
        "catalog": listed[offset : offset + limit],
    }
    if warnings:
        response["warnings"] = warnings
    return response


def refuse_category_without_catalog() -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            "category applies only with catalog=True.",
            suggestions=["Pass catalog=True, or drop category"],
        )
    )


def warn_ignored(response: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """Add a warning naming each parameter set alongside catalog=True that a
    catalog does not use."""
    ignored = [name for name, value in given.items() if value not in (None, False)]
    if ignored:
        response.setdefault("warnings", []).append(
            f"{', '.join(ignored)} ignored: catalog=True lists what can be set "
            "up, not what is configured"
        )
    return response


async def integration_catalog_response(
    client: Any,
    *,
    catalog: bool,
    given: dict[str, Any],
    **params: Any,
) -> dict[str, Any]:
    """``ha_get_integration(catalog=True)``; ``category`` without it is
    refused. ``given`` holds the entry parameters the call set, which a
    catalog ignores with a warning."""
    if not catalog:
        refuse_category_without_catalog()
    return warn_ignored(await integration_catalog(client, **params), given)
