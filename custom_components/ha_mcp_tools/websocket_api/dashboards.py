"""The ``dashboards`` read command and the ``dashboard_edit`` prep step."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .registry import _plainify

# =============================================================================
# ha_mcp_tools/dashboards
# =============================================================================
# LovelaceConfig.mode returns these wire strings (MODE_STORAGE / MODE_YAML in
# core's lovelace const). Compared as strings so a MagicMock-stubbed core in the
# unit suite (no real constants) still exercises the branches.
_LOVELACE_MODE_STORAGE = "storage"
_LOVELACE_MODE_YAML = "yaml"

# Keys of a stored dashboard collection item (core's STORAGE_DASHBOARD_*_FIELDS),
# echoed by ``lovelace/dashboards/list`` — the row shape ``list`` mode mirrors.
_DASHBOARD_ROW_KEYS = (
    "id",
    "url_path",
    "title",
    "icon",
    "show_in_sidebar",
    "require_admin",
)

# Cap on ``search``-mode matches per call so one WS frame stays bounded. ``docs``
# is not capped: the server's card search needs whole configs, and the frame
# stays far below the client's 64 MiB message limit on real installs.
_DASHBOARD_MATCH_CAP = 200

# Keys that hold card configs at any depth inside a card: ``cards`` (a list),
# ``card`` (one card), and the named ``custom_fields`` / ``states`` maps. Custom
# cards file these under keys of their own (``groups[].cards[].card``), so the
# search reads every node and only uses these keys to attribute a string to the
# card it lives on.
_CARD_LIST_KEY = "cards"
_CARD_KEY = "card"
_NAMED_CARD_MAP_KEYS = frozenset({"custom_fields", "states"})
# Bound on non-card nesting inside one card, against pathological configs.
_MAX_NODE_DEPTH = 100


def _do_dashboards(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    prepped: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return Lovelace dashboards read in-process (``list``/``get``/``search``/``docs``).

    Pure assembler over the plain dicts :func:`_dashboards_prep` loads off the
    event loop — every Store load (``async_load``) happens in the prep, so this
    function only shapes / walks already-materialized config. ``available`` is
    ``False`` when the lovelace integration is not set up (no ``LOVELACE_DATA``);
    the server falls back to its legacy ``lovelace/*`` path in that case.

    YAML-mode dashboard bodies are NEVER emitted (``get`` returns a ``yaml_excluded``
    status; ``search``/``docs`` skip them) — their config may carry resolved ``!secret``
    plaintext, so body emission for YAML belongs to a future file-based tool.
    """
    mode = params.get("mode", "list")
    prepped = prepped or {}
    if not prepped.get("available"):
        result: dict[str, Any] = {"mode": mode, "available": False}
        if mode == "list":
            result["dashboards"] = []
        elif mode == "search":
            result["matches"] = []
            result["truncated"] = False
        elif mode == "docs":
            result["docs"] = []
        return result

    if mode == "get":
        return {
            "mode": "get",
            "available": True,
            "status": prepped.get("status"),
            "url_path": prepped.get("url_path"),
            "config": prepped.get("config"),
        }
    if mode == "docs":
        return {
            "mode": "docs",
            "available": True,
            "docs": [
                {"url_path": doc.get("url_path"), "config": doc.get("config")}
                for doc in prepped.get("docs") or []
            ],
            "load_failed": prepped.get("load_failed", 0),
        }
    if mode == "search":
        query_lower = (params.get("query") or "").strip().lower()
        matches, truncated = _search_dashboard_docs(
            prepped.get("docs") or [], query_lower
        )
        return {
            "mode": "search",
            "available": True,
            "matches": matches,
            "truncated": truncated,
            # ``dashboards_doc_search`` additions (issue #2008): the whole-
            # document per-dashboard verdicts + honesty counters the server's
            # ha_search dashboard bucket needs. Additive — a pre-#2008 server
            # ignores them.
            "document_matches": _dashboard_document_matches(
                prepped.get("docs") or [], query_lower
            ),
            "yaml_skipped": prepped.get("yaml_skipped", 0),
            "load_failed": prepped.get("load_failed", 0),
        }
    return {"mode": "list", "available": True, "dashboards": prepped.get("rows") or []}


async def _dashboards_prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Async pre-step for ``dashboards``: do ALL the Store loading off the loop.

    Reaching ``hass.data[LOVELACE_DATA].dashboards`` is a pure in-memory read, but
    loading a dashboard's config (``LovelaceConfig.async_load``) awaits a Store
    read, so every mode's loading lives here and :func:`_do_dashboards` gets plain
    dicts. Returns ``{"prepped": {...}}`` with ``available=False`` when lovelace is
    not set up (the server falls back to legacy).
    """
    mode = msg.get("mode", "list")
    dashboards_map = _lovelace_dashboards_map(hass)
    if dashboards_map is None:
        return {"prepped": {"available": False}}

    prepped: dict[str, Any] = {"available": True}
    if mode == "get":
        prepped.update(await _dashboard_get_config(dashboards_map, msg.get("url_path")))
    elif mode in ("search", "docs"):
        (
            prepped["docs"],
            prepped["yaml_skipped"],
            prepped["load_failed"],
        ) = await _dashboard_search_docs(dashboards_map, force=mode == "docs")
    else:
        prepped["rows"] = _dashboard_list_rows(dashboards_map)
    return {"prepped": prepped}


def _lovelace_dashboards_map(hass: HomeAssistant) -> Mapping[Any, Any] | None:
    """The ``{url_path|None: LovelaceConfig}`` map, or ``None`` if lovelace is absent."""
    container = _lovelace_container(hass)
    if container is None:
        return None
    dashboards = getattr(container, "dashboards", None)
    if dashboards is None and isinstance(container, Mapping):
        dashboards = container.get("dashboards")
    return dashboards if isinstance(dashboards, Mapping) else None


def _lovelace_container(hass: HomeAssistant) -> Any:
    """``hass.data[LOVELACE_DATA]``, or ``None`` if lovelace is absent.

    Uses a function-local import of core's key (older cores keyed
    ``hass.data["lovelace"]``, so both are tried), guarded so a missing key /
    core drift degrades to ``None`` (the server keeps its legacy path) rather
    than raising.
    """
    try:
        from homeassistant.components.lovelace import LOVELACE_DATA

        key: Any = LOVELACE_DATA
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive; core drift / older core
        key = "lovelace"
    data = getattr(hass, "data", None)
    if not isinstance(data, Mapping):
        return None
    container = data.get(key)
    if container is None and key != "lovelace":
        container = data.get("lovelace")
    return container


def _dashboard_list_rows(dashboards_map: Mapping[Any, Any]) -> list[dict[str, Any]]:
    """One metadata row per non-default dashboard, tagged with ``mode``.

    Mirrors the ``lovelace/dashboards/list`` row shape (the stored collection item
    for storage dashboards, read from ``LovelaceConfig.config``) plus an additive
    ``mode`` so the server can exclude YAML dashboards. The default dashboard
    (``url_path`` key ``None``) is omitted — the legacy list omits it too and the
    server special-cases the built-in dashboard as always-existing; ``get`` mode
    resolves ``None`` to that default instead.
    """
    rows: list[dict[str, Any]] = []
    for url_path, dash in dashboards_map.items():
        if url_path is None:
            continue
        config = getattr(dash, "config", None)
        meta = config if isinstance(config, Mapping) else {}
        row: dict[str, Any] = {key: meta.get(key) for key in _DASHBOARD_ROW_KEYS}
        # The dict key is the authoritative url_path (the metadata may lack it).
        row["url_path"] = url_path
        row["mode"] = _dashboard_mode(dash)
        rows.append(row)
    return rows


async def _dashboard_get_config(
    dashboards_map: Mapping[Any, Any], url_path: Any
) -> dict[str, Any]:
    """Load one dashboard's config body; ``status`` names the outcome.

    ``url_path`` ``None``/absent resolves to the default dashboard. A YAML-mode
    dashboard returns ``status="yaml_excluded"`` with no body (its config may
    carry resolved ``!secret`` plaintext — storage-only emission). A missing
    dashboard or a load error returns ``status="not_found"``. Storage freshness is
    safe: ``LovelaceStorage.async_save`` mutates the in-memory object
    synchronously, so this read never lags a save (audit-verified).
    """
    dash = dashboards_map.get(url_path)
    resolved = getattr(dash, "url_path", None) if dash is not None else url_path
    if dash is None:
        return {"status": "not_found", "url_path": url_path, "config": None}
    if _dashboard_mode(dash) == _LOVELACE_MODE_YAML:
        return {"status": "yaml_excluded", "url_path": resolved, "config": None}
    loader = getattr(dash, "async_load", None)
    if not callable(loader):
        return {"status": "not_found", "url_path": resolved, "config": None}
    try:
        config = await loader(False)
    except Exception:  # noqa: BLE001  # any load failure degrades to not_found (fail-soft)
        return {"status": "not_found", "url_path": resolved, "config": None}
    if not isinstance(config, dict):
        return {"status": "not_found", "url_path": resolved, "config": None}
    return {"status": "ok", "url_path": resolved, "config": _plainify(config)}


async def _dashboard_search_docs(
    dashboards_map: Mapping[Any, Any], *, force: bool = False
) -> tuple[list[dict[str, Any]], int, int]:
    """Load every STORAGE dashboard's config for ``search`` and ``docs``.

    ``force`` bypasses Lovelace's config cache, as the server's card search
    (``docs``) promises fresh config like its per-dashboard reads.

    Only storage dashboards are loaded — YAML bodies are never searched/emitted.
    Returns ``(docs, yaml_skipped, load_failed)``.

    ``docs`` are ``[{url_path, title, registry_title, config}, ...]`` plain
    dicts. ``title`` stays the config body's, carried by the card-scoped
    ``matches`` records that servers predating ``dashboards_docs`` read;
    ``registry_title`` carries the list-row metadata title ``document_matches``
    emits (what the legacy ha_search bucket records carry).

    ``yaml_skipped`` counts the YAML-mode entries this walk never reads,
    INCLUDING a default dashboard forced to YAML (``lovelace: mode: yaml``),
    which has no ``list`` row for the server to count. The server's ha_search
    treats a non-zero count as its fall-back-to-legacy signal, since its legacy
    walk DOES read YAML bodies and coverage must not depend on which path
    served (issue #2008 review).

    ``load_failed`` counts storage dashboards whose config load raised or
    returned a non-dict — real gaps the caller must surface as partial rather
    than fail-soft into a clean-looking result. A ``ConfigNotFound`` load is a
    clean skip, not a failure: an auto-generated (never taken control of)
    dashboard has no stored config to scan. If core drift breaks the guarded
    ``ConfigNotFound`` import, those loads degrade to ``load_failed`` —
    over-reported as partial, never silent.
    """
    try:
        from homeassistant.components.lovelace.const import ConfigNotFound
    except Exception:  # pragma: no cover - defensive; core drift  # noqa: BLE001
        ConfigNotFound = None

    docs: list[dict[str, Any]] = []
    yaml_skipped = 0
    load_failed = 0
    for url_path, dash in dashboards_map.items():
        if _dashboard_mode(dash) == _LOVELACE_MODE_YAML:
            yaml_skipped += 1
            continue
        if _dashboard_mode(dash) != _LOVELACE_MODE_STORAGE:
            continue
        loader = getattr(dash, "async_load", None)
        if not callable(loader):
            continue
        try:
            config = await loader(force)
        except Exception as err:  # noqa: BLE001
            if ConfigNotFound is not None and isinstance(err, ConfigNotFound):
                # Auto-generated dashboard: nothing stored, nothing to scan.
                continue
            load_failed += 1
            continue
        if not isinstance(config, dict):
            load_failed += 1
            continue
        meta = getattr(dash, "config", None)
        registry_title = meta.get("title") if isinstance(meta, Mapping) else None
        title = config.get("title")
        docs.append(
            {
                "url_path": url_path,
                "title": str(title) if title is not None else None,
                "registry_title": (
                    str(registry_title) if registry_title is not None else None
                ),
                "config": config,
            }
        )
    return docs, yaml_skipped, load_failed


def _dashboard_mode(dash: Any) -> str | None:
    """A dashboard's ``mode`` (``storage``/``yaml``), guarded against core drift."""
    mode = getattr(dash, "mode", None)
    return str(mode) if isinstance(mode, str) else None


def _dashboard_document_matches(
    docs: list[dict[str, Any]], query_lower: str
) -> list[dict[str, Any]]:
    """Per-dashboard whole-document verdicts: ``[{url_path, title}, ...]``.

    One entry per doc whose ENTIRE config contains ``query_lower`` — the
    coverage the server's legacy ``_search_in_dict`` walk provides (view
    titles, dashboard-level keys, every leaf), which the card-scoped
    ``matches`` walk deliberately narrows to. ``title`` is the registry
    metadata's (falling back to the body's) — what the legacy ha_search
    bucket records carry. An empty query matches nothing. Bounded by the
    dashboard count, so no cap/truncation applies.
    """
    if not query_lower:
        return []
    return [
        {
            "url_path": doc.get("url_path"),
            "title": doc.get("registry_title") or doc.get("title"),
        }
        for doc in docs
        if _doc_contains(doc.get("config"), query_lower)
    ]


def _doc_contains(data: Any, query_lower: str, depth: int = 0) -> bool:
    """Case-insensitive substring test over keys and every leaf of a config.

    Exact port of the server's ``_search_in_dict_exact`` (keys + string
    leaves + ``str()`` of non-None scalars) so the component-served verdict
    matches the legacy walk's, leaf for leaf. Containers below
    ``_MAX_NODE_DEPTH`` levels are not read, so a pathological config cannot
    exhaust the recursion limit.
    """
    if isinstance(data, (dict, list)) and depth > _MAX_NODE_DEPTH:
        return False
    if isinstance(data, dict):
        return any(
            query_lower in str(key).lower()
            or _doc_contains(value, query_lower, depth + 1)
            for key, value in data.items()
        )
    if isinstance(data, list):
        return any(_doc_contains(item, query_lower, depth + 1) for item in data)
    if isinstance(data, str):
        return query_lower in data.lower()
    if data is not None:
        return query_lower in str(data).lower()
    return False


def _search_dashboard_docs(
    docs: list[dict[str, Any]], query_lower: str
) -> tuple[list[dict[str, Any]], bool]:
    """Walk each dashboard config for ``query_lower``; return ``(matches, truncated)``.

    An empty query matches nothing (a bare substring would match every string).
    Matches are capped at :data:`_DASHBOARD_MATCH_CAP` with a ``truncated`` flag.
    """
    if not query_lower:
        return [], False
    matches: list[dict[str, Any]] = []
    for doc in docs:
        _collect_dashboard_matches(doc, query_lower, matches)
    truncated = len(matches) > _DASHBOARD_MATCH_CAP
    return matches[:_DASHBOARD_MATCH_CAP], truncated


def _collect_dashboard_matches(
    doc: dict[str, Any], query_lower: str, matches: list[dict[str, Any]]
) -> None:
    """Append every ``query_lower`` hit in one dashboard config to ``matches``.

    Walks each view's card containers (``cards`` + sections-view ``sections.cards``,
    nested cards recursed), plus the two view-level containers the card walk never
    visits: ``badges`` and a sections-view ``header.card``, so a query answered
    "no match" here is a real absence, not a blind spot for entities referenced
    only as a badge or in a header card. Servers predating ``dashboards_docs``
    read these matches; newer ones search the configs ``docs`` returns.
    """
    config = doc.get("config")
    if not isinstance(config, dict):
        return
    views = config.get("views")
    if not isinstance(views, list):
        return
    url_path = doc.get("url_path")
    dash_title = doc.get("title")
    for view_index, view in enumerate(views):
        if not isinstance(view, dict):
            continue
        view_title = view.get("title")
        for cards, base_path in _view_card_containers(view, view_index):
            _collect_card_matches(
                cards,
                base_path,
                url_path,
                dash_title,
                view_index,
                view_title,
                query_lower,
                matches,
            )
        _collect_badge_matches(
            view, view_index, url_path, dash_title, view_title, query_lower, matches
        )
        _collect_header_card_matches(
            view, view_index, url_path, dash_title, view_title, query_lower, matches
        )


def _view_card_containers(
    view: dict[str, Any], view_index: int
) -> list[tuple[Any, str]]:
    """The card lists in a view: top-level ``cards`` plus each section's ``cards``."""
    containers: list[tuple[Any, str]] = []
    if isinstance(view.get("cards"), list):
        containers.append((view["cards"], f"views[{view_index}].cards"))
    sections = view.get("sections")
    if isinstance(sections, list):
        for si, section in enumerate(sections):
            if isinstance(section, dict) and isinstance(section.get("cards"), list):
                containers.append(
                    (section["cards"], f"views[{view_index}].sections[{si}].cards")
                )
    return containers


def _dashboard_match(
    url_path: Any,
    dash_title: Any,
    view_index: int,
    view_title: Any,
    card_path: str,
    card_type: Any,
    matched_field: str,
    matched_value: str,
) -> dict[str, Any]:
    """One ``search``-mode match record (shared, fixed shape).

    Every match site — cards, badges, header cards — builds its record here so the
    wire shape servers predating ``dashboards_docs`` read stays identical.
    """
    return {
        "url_path": url_path,
        "title": dash_title,
        "view_index": view_index,
        "view_title": view_title,
        "card_path": card_path,
        "card_type": card_type,
        "matched_field": matched_field,
        "matched_value": matched_value,
    }


def _collect_card_matches(
    cards: Any,
    base_path: str,
    url_path: Any,
    dash_title: Any,
    view_index: int,
    view_title: Any,
    query_lower: str,
    matches: list[dict[str, Any]],
) -> None:
    """Record one match per string leaf containing the query, card by card.

    ``matched_field`` is the leaf's immediate key (``entity`` / ``entities`` /
    ``camera_image`` / any plain-string field). Cards nested in any card slot
    below (see :func:`_walk_card_nodes`) are walked as their own cards, so
    ``card_path`` / ``card_type`` always name the card the string lives on.
    """
    if not isinstance(cards, list):
        return
    for card_index, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        _collect_one_card_matches(
            card,
            f"{base_path}[{card_index}]",
            url_path,
            dash_title,
            view_index,
            view_title,
            query_lower,
            matches,
        )


def _collect_one_card_matches(
    card: dict[str, Any],
    card_path: str,
    url_path: Any,
    dash_title: Any,
    view_index: int,
    view_title: Any,
    query_lower: str,
    matches: list[dict[str, Any]],
) -> None:
    """Record matches for a SINGLE card at ``card_path`` and every card below it.

    Shared by :func:`_collect_card_matches` (list-indexed cards) and
    :func:`_collect_header_card_matches` (a header card is a single card, not
    list-indexed).
    """
    leaves: list[tuple[str, str]] = []
    nested: list[tuple[str, dict[str, Any]]] = []
    _walk_card_nodes(card, card_path, "", leaves, nested)
    card_type = card.get("type")
    for field, value in leaves:
        if query_lower in value.lower():
            matches.append(
                _dashboard_match(
                    url_path,
                    dash_title,
                    view_index,
                    view_title,
                    card_path,
                    card_type,
                    field,
                    value,
                )
            )
    for child_path, child in nested:
        _collect_one_card_matches(
            child,
            child_path,
            url_path,
            dash_title,
            view_index,
            view_title,
            query_lower,
            matches,
        )


def _collect_badge_matches(
    view: dict[str, Any],
    view_index: int,
    url_path: Any,
    dash_title: Any,
    view_title: Any,
    query_lower: str,
    matches: list[dict[str, Any]],
) -> None:
    """Record query hits in a view's ``badges`` — entity refs the card walk misses.

    View-level badges are entity references by construction: a bare string
    (``sensor.x``) or a dict (``{type: entity, entity: sensor.x}``). A bare-string
    badge is recorded as a ``badges`` leaf; a dict badge's string leaves are walked
    like a card's.
    """
    badges = view.get("badges")
    if not isinstance(badges, list):
        return
    for badge_index, badge in enumerate(badges):
        badge_path = f"views[{view_index}].badges[{badge_index}]"
        if isinstance(badge, str):
            if badge and query_lower in badge.lower():
                matches.append(
                    _dashboard_match(
                        url_path,
                        dash_title,
                        view_index,
                        view_title,
                        badge_path,
                        "badge",
                        "badges",
                        badge,
                    )
                )
        elif isinstance(badge, dict):
            badge_type = badge.get("type") or "badge"
            for field, value in _card_string_leaves(badge):
                if query_lower in value.lower():
                    matches.append(
                        _dashboard_match(
                            url_path,
                            dash_title,
                            view_index,
                            view_title,
                            badge_path,
                            badge_type,
                            field,
                            value,
                        )
                    )


def _collect_header_card_matches(
    view: dict[str, Any],
    view_index: int,
    url_path: Any,
    dash_title: Any,
    view_title: Any,
    query_lower: str,
    matches: list[dict[str, Any]],
) -> None:
    """Record query hits in a sections-view header card (``views[n].header.card``).

    The header accepts a card (typically Markdown) that can carry entity refs; the
    card walk never visits it.
    """
    header = view.get("header")
    if not isinstance(header, dict):
        return
    header_card = header.get("card")
    if not isinstance(header_card, dict):
        return
    _collect_one_card_matches(
        header_card,
        f"views[{view_index}].header.card",
        url_path,
        dash_title,
        view_index,
        view_title,
        query_lower,
        matches,
    )


def _card_string_leaves(card: dict[str, Any]) -> list[tuple[str, str]]:
    """``(immediate_key, string)`` for every string leaf a card owns.

    Leaves of cards nested below it are left out (callers that need them walk
    those cards separately). The key attributed to a leaf is the nearest dict
    key, so ``entities: [{entity: light.a}]`` yields ``("entity", "light.a")``
    and ``entities: [light.a]`` yields ``("entities", "light.a")``.
    """
    out: list[tuple[str, str]] = []
    _walk_card_nodes(card, "", "", out, [])
    return out


def _walk_card_nodes(
    value: Any,
    path: str,
    key: str,
    leaves: list[tuple[str, str]],
    nested: list[tuple[str, dict[str, Any]]],
    depth: int = 0,
) -> None:
    """Split a card's subtree into its own string leaves and its nested cards.

    Every dict and list is descended. A typed dict in a card slot (a ``cards``
    item, a ``card`` value or a ``custom_fields``/``states`` value) is a nested
    card: it goes to ``nested`` with its path instead of contributing leaves.
    Typed dicts elsewhere (tile ``features``, entity rows) stay leaves of the
    card that holds them. Descent stops ``_MAX_NODE_DEPTH`` levels down, far
    below Python's recursion limit.
    """
    if isinstance(value, str):
        if value:
            leaves.append((key, value))
    elif depth > _MAX_NODE_DEPTH:
        return
    elif isinstance(value, dict):
        for k, v in value.items():
            _walk_card_entry(k, v, f"{path}{_path_key(k)}", leaves, nested, depth + 1)
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _walk_card_nodes(item, f"{path}[{i}]", key, leaves, nested, depth + 1)


def _walk_card_entry(
    key: Any,
    value: Any,
    path: str,
    leaves: list[tuple[str, str]],
    nested: list[tuple[str, dict[str, Any]]],
    depth: int,
) -> None:
    """``_walk_card_nodes`` for one ``key: value`` entry found at ``path``."""
    slots = _card_slot_items(key, value)
    if slots is None:
        _walk_card_nodes(value, path, str(key), leaves, nested, depth)
        return
    for segment, leaf_key, item in slots:
        if _is_card(item):
            nested.append((f"{path}{segment}", item))
        else:
            _walk_card_nodes(item, f"{path}{segment}", leaf_key, leaves, nested, depth)


def _card_slot_items(key: Any, value: Any) -> list[tuple[str, str, Any]] | None:
    """``(path segment, leaf key, item)`` per card slot under ``key``, else ``None``."""
    if key == _CARD_LIST_KEY and isinstance(value, list):
        return [(f"[{i}]", key, item) for i, item in enumerate(value)]
    if key == _CARD_KEY:
        return [("", key, value)]
    if key in _NAMED_CARD_MAP_KEYS and isinstance(value, dict):
        return [(_path_key(name), str(name), item) for name, item in value.items()]
    return None


def _is_card(value: Any) -> bool:
    return isinstance(value, dict) and "type" in value


def _path_key(key: Any) -> str:
    """``.key`` for a plain identifier, else a JSON-quoted ``["key"]`` segment."""
    text = str(key)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
        return f".{text}"
    return f"[{json.dumps(text)}]"


async def _dashboard_edit_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    from ..dashboard_edit import async_edit_dashboard

    return {"result": await async_edit_dashboard(hass, msg)}


def _do_dashboard_edit(
    hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
) -> dict[str, Any]:
    """Preserve the write outcome assembled by the async edit lifecycle."""
    return result
