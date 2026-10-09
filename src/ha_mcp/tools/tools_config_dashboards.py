"""
Configuration management tools for Home Assistant Lovelace dashboards.

This module provides tools for managing dashboard metadata and content.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal, NoReturn, cast, overload

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import ToolResult, tool

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..dashboard_screenshot.capture import (
    DEFAULT_HEIGHT,
    DEFAULT_RENDER_TIMEOUT_SECONDS,
    DEFAULT_WAIT_MS,
    DEFAULT_WIDTH,
    Orientation,
    ScreenshotFormat,
    ViewportPreset,
)
from ..dashboard_screenshot.content import (
    dashboard_image_content,
    dashboard_screenshot_metadata,
    dashboard_screenshot_warnings,
)
from ..dashboard_screenshot.paths import (
    dashboard_frontend_path,
    dashboard_render_paths,
    match_dashboard_view,
    resolve_dashboard_view,
)
from ..errors import ErrorCode, create_error_response, get_error_code, get_error_message
from ..strict_bps import BestPracticeKeyParam
from ..utils.config_hash import compute_config_hash
from ..utils.dashboard_patch import apply_dashboard_patch
from ..utils.python_sandbox import (
    PythonSandboxError,
    PythonSandboxExecutionError,
    format_sandbox_error,
    get_security_documentation,
    safe_execute,
)
from .auto_backup import with_auto_backup
from .coercion import JSON_STRING_COERCION, parse_json_param
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .component_dashboard_edit import edit_dashboard_via_component
from .config_write_helpers import (
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
)
from .dashboard_card_describe import describe_card_response
from .dashboard_edit_errors import (
    raise_dashboard_edit_error,
    raise_dashboard_edit_fetch_error,
    raise_known_dashboard_save_rejection,
)
from .dashboard_list_checks import (
    patch_writes_inside_card,
    reject_malformed_dashboard_config,
    reject_malformed_dashboard_lists,
    reject_malformed_dashboard_patch,
)
from .helpers import (
    exception_to_structured_error,
    extract_tool_error_message,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)

_LARGE_DASHBOARD_CONFIG_SIZE = 10000


def _large_dashboard_replacement_warning(size: int) -> str | None:
    """Keep the full-replacement guidance identical on both backends."""
    if size < _LARGE_DASHBOARD_CONFIG_SIZE:
        return None
    return (
        f"Replaced large config ({size:,} bytes). "
        "Consider patch for known paths or python_transform for pattern-based edits."
    )


@dataclass(frozen=True, slots=True)
class _DashboardScreenshotOptions:
    """Shared render options for get/set screenshot paths."""

    view_path: str | None = None
    width: int = DEFAULT_WIDTH
    height: int | Literal["auto"] = DEFAULT_HEIGHT
    viewport_presets: list[ViewportPreset] | None = None
    orientation: Orientation | None = None
    zoom: float = 1.0
    wait_ms: int = DEFAULT_WAIT_MS
    full_page: bool = False
    theme: str | None = None
    dark_mode: bool = False
    language: str | None = None
    image_format: ScreenshotFormat = "png"
    render_timeout_seconds: float = DEFAULT_RENDER_TIMEOUT_SECONDS


# Live card fields replace the static reference only on capable components.
_DASHBOARD_SKILL_FILES: tuple[str, ...] = ("references/dashboard-guide.md",)


async def _attach_dashboard_skill(
    response: dict[str, Any], MandatoryBPS: bool, client: Any
) -> None:
    """In-place attach skill_content to a dashboard response when applicable.

    Delegates to the shared :func:`attach_skill_content` so the
    missing-vendor-warning path is consistent across every write tool.
    """
    files = _DASHBOARD_SKILL_FILES
    if MandatoryBPS and not component_supports(
        await get_component_caps(client), "dashboard_cards"
    ):
        files += ("references/dashboard-cards.md",)
    attach_skill_content(
        response,
        MandatoryBPS=MandatoryBPS,
        canonical_files=files,
        referenced_files=None,
    )


async def _get_dashboard_config_internal(
    client: Any, url_path: str | None
) -> tuple[dict[str, Any], str]:
    """Fetch dashboard config from HA and compute its hash.

    Returns ``(config, config_hash)`` tuple where ``config`` is the
    authoritative Lovelace config dict returned by HA's ``lovelace/config``
    WebSocket call (with ``force=True`` to bypass any cache) and
    ``config_hash`` is computed from that config via ``compute_config_hash``.

    Used internally to obtain the authoritative post-save hash and as the
    fetch+hash building block for the optimistic-locking pre-read paths.
    Mirrors the ``_get_<entity>_config_internal`` helpers in the sibling
    files (``tools_config_scripts.py``, ``tools_config_automations.py``,
    ``tools_config_scenes.py``).

    Raises ``ToolError`` with ``ErrorCode.SERVICE_CALL_FAILED`` if the
    WebSocket call reports failure or the response is not a dict. HA's
    upstream error code is preserved as ``ha_error_code`` when present, so
    callers can distinguish confirmed absence from an unverifiable read.
    """
    get_data: dict[str, Any] = {"type": "lovelace/config", "force": True}
    if url_path:
        get_data["url_path"] = url_path

    response = await client.send_websocket_message(get_data)

    if isinstance(response, dict) and not response.get("success", True):
        error_msg = get_error_message(response)
        if error_msg is None:
            error_msg = str(response.get("error", {}))
        ha_error_code = response.get("error_code") or get_error_code(response)
        error_context: dict[str, Any] = {"url_path": url_path}
        if ha_error_code is not None:
            # Preserve HA's code so downstream safety decisions do not have to
            # infer an absent config from a multiply-prefixed message alone.
            error_context["ha_error_code"] = str(ha_error_code)
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Dashboard fetch failed: {error_msg}",
                context=error_context,
            )
        )

    config = response.get("result") if isinstance(response, dict) else response
    if not isinstance(config, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Dashboard config response was not a dict",
                context={"url_path": url_path},
            )
        )

    return cast(dict[str, Any], config), compute_config_hash(config)


def _badge_matches(badge: Any, entity_id: str) -> bool:
    """Check if a badge matches the entity_id search criteria.

    Badges can be simple strings (entity IDs) or dicts with an 'entity' field.
    Supports wildcard matching with *.
    """
    # Extract entity from badge
    if isinstance(badge, str):
        badge_entity = badge
    elif isinstance(badge, dict):
        badge_entity = badge.get("entity", "")
    else:
        return False

    if not badge_entity:
        return False

    # Support wildcard matching (same logic as _card_matches)
    if "*" in entity_id:
        pattern = entity_id.replace(".", r"\.").replace("*", ".*")
        return bool(re.match(pattern, badge_entity))

    return entity_id == badge_entity


# Card slots — keys whose typed values are card configs (issue #1599). They are
# recognised at ANY depth inside a card, because custom cards file sub-cards
# under keys of their own (``groups[].cards[].card``, ``tabs[].card``; #2694):
#   - ``cards`` (list): stacks, grids and custom wrappers.
#   - ``card`` (dict): conditional and wrapper cards.
#   - ``custom_fields`` / ``states`` (name -> card map): ``custom:button-card``
#     field cards and ``custom:state-switch`` state cards.
# Every other dict/list is descended for further slots and string leaves, but a
# typed dict outside a slot is never a card: tile ``features`` and view
# ``conditions`` also carry ``type`` and would false-match as cards.
# Picture-elements ``elements`` hold elements, not cards; a card carrying them
# is disclosed (``_UNTRAVERSED_NESTED_KEYS``) because entity-ref matching does
# not look inside them.
_NESTED_CARDS_KEY = "cards"
_NESTED_CARD_KEY = "card"
_NESTED_CUSTOM_FIELDS_KEY = "custom_fields"
_NESTED_STATES_KEY = "states"
# Child-bearing keys recognised but deliberately NOT traversed. A walked card
# carrying one of these (with a truthy value) cannot be fully covered, so it is
# its *presence* — not the absence of matches — that the response discloses
# (issue #1599: disclose by presence, not by absence-inference). picture-elements
# ``elements`` is the canonical case.
_UNTRAVERSED_NESTED_KEYS = ("elements",)
# Defensive bound against pathological/malformed configs. Real dashboards nest
# only a handful of levels; this guards recursion depth far above any real use.
_MAX_CARD_DEPTH = 50


def _py_key(name: str) -> str:
    """Render a mapping key as a Python subscript segment (``['name']``).

    ``repr`` quotes and escapes the key, so a name containing a quote (e.g.
    ``o'brien``) yields a valid literal; a raw ``['{name}']`` interpolation would
    splice an unterminated string into ``python_transform``.
    """
    return f"[{name!r}]"


def _jq_key(name: str) -> str:
    """Render a mapping key as a jq path segment.

    Identifier-safe keys use dot notation (``.name``); any other key (a dot, a
    space, a quote) is emitted as a bracketed JSON string (``["weird.key"]``) so
    jq does not read an embedded dot as further nesting.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return f".{name}"
    return f"[{json.dumps(name)}]"


def _log_non_str_key(container_key: str, name: object, jq_prefix: str) -> None:
    """Breadcrumb a non-string mapping key under a card-bearing container.

    Dashboard config arrives as JSON, so keys are normally strings; a non-string
    key (from a corrupted or hand-edited config) cannot form a valid path, so the
    entry is skipped rather than crashing the walk via ``_jq_key`` / ``_py_key``.
    """
    logger.debug(
        "Card-search skipping non-string %s key at %s (%r, %s)",
        container_key,
        jq_prefix,
        name,
        type(name).__name__,
    )


class _CardWalkFrame:
    """Bundles the parameters ``_walk_card`` threads through every recursive
    descent, so the per-container helpers below don't each need a 9-parameter
    signature just to forward context unchanged."""

    __slots__ = (
        "card_index",
        "card_type",
        "depth",
        "entity_id",
        "heading",
        "query",
        "section_index",
        "truncation",
        "uncovered",
        "view_index",
    )

    def __init__(
        self,
        entity_id: str | None,
        card_type: str | None,
        heading: str | None,
        *,
        view_index: int,
        section_index: int | None,
        card_index: int | None,
        depth: int,
        truncation: list[str] | None,
        uncovered: list[str] | None,
        query: str | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.query = query
        self.card_type = card_type
        self.heading = heading
        self.view_index = view_index
        self.section_index = section_index
        self.card_index = card_index
        self.depth = depth
        self.truncation = truncation
        self.uncovered = uncovered

    def with_indices(
        self, *, section_index: int | None, card_index: int | None
    ) -> "_CardWalkFrame":
        """A copy of this frame at the same depth with different top-level indices.

        For locating a *top-level* card within a view — not a recursive descent,
        so ``depth`` is unchanged (matches the pre-refactor behavior where the
        top-level ``_walk_card`` call used the default ``depth=0``).
        """
        return _CardWalkFrame(
            self.entity_id,
            self.card_type,
            self.heading,
            view_index=self.view_index,
            section_index=section_index,
            card_index=card_index,
            depth=self.depth,
            truncation=self.truncation,
            uncovered=self.uncovered,
            query=self.query,
        )

    def descend(self) -> "_CardWalkFrame":
        """A copy of this frame one level deeper (everything else unchanged)."""
        return _CardWalkFrame(
            self.entity_id,
            self.card_type,
            self.heading,
            view_index=self.view_index,
            section_index=self.section_index,
            card_index=self.card_index,
            depth=self.depth + 1,
            truncation=self.truncation,
            uncovered=self.uncovered,
            query=self.query,
        )


def _split_card_node(
    node: Any,
    path: tuple[str, str],
    key: str,
    leaves: list[tuple[str, str]],
    cards: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """Split a card's subtree into its own string leaves and the cards below it.

    ``path`` is the (jq, python) suffix of ``node``. Every dict and list is
    descended; a typed dict in a card slot goes to ``cards`` with its paths
    instead of contributing leaves. Leaves carry their nearest dict key.
    Mirrors the component's ``_walk_card_nodes``.
    """
    jq, py = path
    if isinstance(node, str):
        if node:
            leaves.append((key, node))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _split_card_node(item, (f"{jq}[{i}]", f"{py}[{i}]"), key, leaves, cards)
    elif isinstance(node, dict):
        for k, v in node.items():
            if not isinstance(k, str):
                _log_non_str_key("card", k, jq)
                continue
            child_jq, child_py = f"{jq}{_jq_key(k)}", f"{py}{_py_key(k)}"
            for jq_seg, py_seg, leaf_key, item in _card_slots(k, v, child_jq):
                item_path = (f"{child_jq}{jq_seg}", f"{child_py}{py_seg}")
                if leaf_key is not None and isinstance(item, dict) and "type" in item:
                    cards.append((*item_path, item))
                else:
                    _split_card_node(
                        item,
                        item_path,
                        k if leaf_key is None else leaf_key,
                        leaves,
                        cards,
                    )


def _card_slots(
    key: str, value: Any, jq: str
) -> list[tuple[str, str, str | None, Any]]:
    """``(jq segment, python segment, leaf key, item)`` for each slot under ``key``.

    A key that holds no card slots yields one entry for the value itself with a
    ``None`` leaf key, so the caller descends it as plain content.
    """
    if key == _NESTED_CARDS_KEY and isinstance(value, list):
        return [(f"[{i}]", f"[{i}]", key, item) for i, item in enumerate(value)]
    if key == _NESTED_CARD_KEY:
        return [("", "", key, value)]
    if key in (_NESTED_CUSTOM_FIELDS_KEY, _NESTED_STATES_KEY) and isinstance(
        value, dict
    ):
        slots: list[tuple[str, str, str | None, Any]] = []
        for name, item in value.items():
            if isinstance(name, str):
                slots.append((_jq_key(name), _py_key(name), name, item))
            else:
                _log_non_str_key(key, name, jq)
        return slots
    return [("", "", None, value)]


def _walk_nested_cards(
    card: dict[str, Any], *, jq_prefix: str, python_prefix: str, frame: _CardWalkFrame
) -> list[dict[str, Any]]:
    """Matches in every card nested anywhere below ``card``, one level deeper."""
    nested: list[tuple[str, str, dict[str, Any]]] = []
    _split_card_node(card, ("", ""), "", [], nested)
    child_frame = frame.descend()
    matches: list[dict[str, Any]] = []
    for jq_suffix, py_suffix, child in nested:
        matches.extend(
            _walk_card(
                child,
                jq_prefix=f"{jq_prefix}{jq_suffix}",
                python_prefix=f"{python_prefix}{py_suffix}",
                frame=child_frame,
            )
        )
    return matches


def _walk_card(
    card: Any,
    *,
    jq_prefix: str,
    python_prefix: str,
    frame: _CardWalkFrame,
) -> list[dict[str, Any]]:
    """Return matches for ``card`` and every card nested beneath it.

    Descends every card slot (``cards`` / ``card`` / ``custom_fields`` /
    ``states``) found at any depth inside the card, up to ``_MAX_CARD_DEPTH``
    card levels.

    ``jq_prefix`` / ``python_prefix`` locate ``card`` itself — the former in jq
    dot-notation, the latter as a Python subscript chain usable (appended after
    ``config``) directly in ``ha_config_set_dashboard(python_transform=...)``.
    Nested descendants extend both prefixes per level, so the path strings are
    the authoritative locator for nested cards (the flat ``view_index`` /
    ``section_index`` / ``card_index`` identify the top-level container only and
    are carried unchanged into nested matches for back-compat).

    Only a dict carrying a ``type`` key is treated as a card; this keeps non-card
    dicts reached under these keys (action targets, style blocks, entity rows)
    from matching. If ``frame.truncation`` is provided, the prefix of any subtree
    skipped at the depth bound is appended to it. If ``frame.uncovered`` is
    provided, the path of any walked card carrying a non-traversed child-bearing
    key (see ``_UNTRAVERSED_NESTED_KEYS``) is appended to it, so the caller can
    disclose the incompleteness regardless of whether the search matched anything.
    """
    if not isinstance(card, dict):
        # Structurally-present but malformed slot (e.g. a string where a card
        # dict is expected): skip, but breadcrumb so it is not a silent drop.
        if card is not None:
            logger.debug(
                "Card-search skipping non-dict node at %s (%s)",
                jq_prefix,
                type(card).__name__,
            )
        return []
    if frame.depth > _MAX_CARD_DEPTH:
        # Stop, but make the truncation visible rather than silently dropping
        # any cards nested below this point. Only reachable on pathological or
        # malformed configs (real dashboards nest a handful of levels).
        logger.warning(
            "Card-search depth bound (%d) exceeded at %s; not descending further",
            _MAX_CARD_DEPTH,
            jq_prefix,
        )
        if frame.truncation is not None:
            frame.truncation.append(jq_prefix)
        return []

    matches: list[dict[str, Any]] = []
    if "type" in card:
        match = _card_search_match(card, jq_prefix, python_prefix, frame)
        if match is not None:
            matches.append(match)
        # Disclose un-coverable nesting by presence during the walk, not by the
        # absence of matches: a card that carries e.g. picture-elements
        # ``elements`` hides content this search cannot reach whether or not it
        # (or anything else) matched.
        if frame.uncovered is not None:
            for key in _UNTRAVERSED_NESTED_KEYS:
                if card.get(key):
                    frame.uncovered.append(f"{jq_prefix}.{key}")
                    break

    matches.extend(
        _walk_nested_cards(
            card, jq_prefix=jq_prefix, python_prefix=python_prefix, frame=frame
        )
    )
    return matches


def _card_search_match(
    card: dict[str, Any], jq_prefix: str, python_prefix: str, frame: _CardWalkFrame
) -> dict[str, Any] | None:
    """The match record for ``card`` when it meets every criterion in ``frame``."""
    hits = _query_hits(card, "", frame.query)
    if hits == [] or not _card_matches(
        card, frame.entity_id, frame.card_type, frame.heading
    ):
        return None
    match: dict[str, Any] = {
        "view_index": frame.view_index,
        "section_index": frame.section_index,
        "card_index": frame.card_index,
        "jq_path": jq_prefix,
        "python_path": python_prefix,
        "card_type": card.get("type"),
        "card_config": card,
    }
    if hits is not None:
        match["matched"] = hits
    return match


def _find_badge_matches_in_view(
    view: dict[str, Any], view_idx: int, frame: _CardWalkFrame
) -> list[dict[str, Any]]:
    """View-level badges matching ``frame``'s entity_id and/or query.

    Badges are entity references, so they answer entity_id and text searches;
    a heading or a card_type other than ``badge`` excludes them.
    """
    if (
        frame.heading is not None
        or frame.card_type not in (None, "badge")
        or (frame.entity_id is None and frame.query is None)
    ):
        return []
    matches: list[dict[str, Any]] = []
    for badge_idx, badge in enumerate(view.get("badges", [])):
        if frame.entity_id is not None and not _badge_matches(badge, frame.entity_id):
            continue
        hits = _query_hits(badge, "badges", frame.query)
        if hits == []:
            continue
        is_dict_badge = isinstance(badge, dict)
        badge_match: dict[str, Any] = {
            "view_index": view_idx,
            "section_index": None,
            "card_index": None,
            "badge_index": badge_idx,
            "jq_path": f".views[{view_idx}].badges[{badge_idx}]",
            "card_type": "badge",
            "card_config": badge if is_dict_badge else {"entity": badge},
        }
        # A bare-string badge (the common form) is not subscript-assignable, so
        # a python_path spliced into python_transform would raise TypeError.
        # Only advertise python_path for dict badges; string badges must be
        # converted to dict form first.
        if is_dict_badge:
            badge_match["python_path"] = f"['views'][{view_idx}]['badges'][{badge_idx}]"
        if hits is not None:
            badge_match["matched"] = hits
        matches.append(badge_match)
    return matches


def _query_hits(node: Any, key: str, query: str | None) -> list[dict[str, str]] | None:
    """``{field, value}`` for each string ``node`` owns containing ``query``.

    ``None`` when there is no query; strings of cards nested inside ``node``
    belong to those cards and are not counted here.
    """
    if query is None:
        return None
    leaves: list[tuple[str, str]] = []
    _split_card_node(node, ("", ""), key, leaves, [])
    return [{"field": f, "value": v} for f, v in leaves if query in v.lower()]


def _find_header_card_matches(
    view: dict[str, Any], view_idx: int, frame: _CardWalkFrame
) -> list[dict[str, Any]]:
    """Search a sections-view header card (views[n].header.card).

    The header accepts a card (typically Markdown) that can contain entity refs.
    """
    header = view.get("header", {})
    if not isinstance(header, dict):
        return []
    header_card = header.get("card")
    if not isinstance(header_card, dict):
        return []
    return _walk_card(
        header_card,
        jq_prefix=f".views[{view_idx}].header.card",
        python_prefix=f"['views'][{view_idx}]['header']['card']",
        frame=frame,
    )


def _find_view_card_matches(
    view: dict[str, Any], view_idx: int, frame: _CardWalkFrame
) -> list[dict[str, Any]]:
    """Search the top-level cards of a view (sections-based or flat layout)."""
    matches: list[dict[str, Any]] = []
    view_type = view.get("type", "masonry")

    if view_type == "sections":
        sections = view.get("sections", [])
        for section_idx, section in enumerate(sections):
            if not isinstance(section, dict):
                continue
            cards = section.get("cards", [])
            for card_idx, card in enumerate(cards):
                matches.extend(
                    _walk_card(
                        card,
                        jq_prefix=f".views[{view_idx}].sections[{section_idx}].cards[{card_idx}]",
                        python_prefix=f"['views'][{view_idx}]['sections'][{section_idx}]['cards'][{card_idx}]",
                        frame=frame.with_indices(
                            section_index=section_idx, card_index=card_idx
                        ),
                    )
                )
    else:
        cards = view.get("cards", [])
        for card_idx, card in enumerate(cards):
            matches.extend(
                _walk_card(
                    card,
                    jq_prefix=f".views[{view_idx}].cards[{card_idx}]",
                    python_prefix=f"['views'][{view_idx}]['cards'][{card_idx}]",
                    frame=frame.with_indices(section_index=None, card_index=card_idx),
                )
            )
    return matches


def _find_cards_in_config(
    config: dict[str, Any],
    entity_id: str | None = None,
    card_type: str | None = None,
    heading: str | None = None,
    truncation: list[str] | None = None,
    uncovered: list[str] | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """
    Find cards, badges, and header cards in a dashboard config matching the search criteria.

    ``query`` (lower-cased by the caller) additionally requires a string the
    card itself holds to contain it; each such match lists the hits under
    ``matched``. Criteria are AND-ed.

    Returns a list of matches with location info and card/badge/header config.
    Searches cards (in sections and flat views), view-level badges, and
    sections-view header cards (views[n].header.card). Card search recurses into
    nested containers (``cards`` lists in stacks/grids, ``card`` dicts in
    conditional/wrapper cards, ``custom_fields`` sub-cards in button-card, and
    ``states`` sub-cards in custom:state-switch), so a nested card is found like
    a top-level one (issue #1599) — up to a depth bound.

    Each match carries both ``jq_path`` (jq dot-notation) and ``python_path``
    (a Python subscript chain appended after ``config`` for
    ``ha_config_set_dashboard(python_transform)``); these locate nested as well
    as top-level cards. The flat ``*_index`` fields identify the top-level
    container only. If ``truncation`` is provided, the prefixes of any subtrees
    skipped at the depth bound are appended to it. If ``uncovered`` is provided,
    the paths of any walked cards carrying a non-traversed child-bearing key
    (e.g. picture-elements ``elements``) are appended to it.
    """
    matches: list[dict[str, Any]] = []

    if "strategy" in config:
        return []  # Strategy dashboards don't have explicit cards

    views = config.get("views", [])
    for view_idx, view in enumerate(views):
        if not isinstance(view, dict):
            continue

        frame = _CardWalkFrame(
            entity_id,
            card_type,
            heading,
            view_index=view_idx,
            section_index=None,
            card_index=None,
            depth=0,
            truncation=truncation,
            uncovered=uncovered,
            query=query,
        )
        matches.extend(_find_badge_matches_in_view(view, view_idx, frame))
        matches.extend(_find_header_card_matches(view, view_idx, frame))
        matches.extend(_find_view_card_matches(view, view_idx, frame))

    return matches


def _card_matches(
    card: dict[str, Any],
    entity_id: str | None,
    card_type: str | None,
    heading: str | None,
) -> bool:
    """Check if a card matches the search criteria."""
    # Type filter
    if card_type is not None:
        if card.get("type") != card_type:
            return False

    # Entity filter (supports partial matching with *)
    if entity_id is not None:
        card_entity = card.get("entity", "")
        # Also check entities list for cards that have multiple entities
        card_entities = card.get("entities", [])
        if isinstance(card_entities, list):
            all_entities = [card_entity] + [
                e.get("entity", e) if isinstance(e, dict) else e for e in card_entities
            ]
        else:
            all_entities = [card_entity]

        # Support wildcard matching
        if "*" in entity_id:
            pattern = entity_id.replace(".", r"\.").replace("*", ".*")
            if not any(re.match(pattern, e) for e in all_entities if e):
                return False
        elif entity_id not in all_entities:
            return False

    # Heading filter (for heading cards or section titles)
    if heading is not None:
        card_heading = card.get("heading", card.get("title", ""))
        # Case-insensitive partial match
        if heading.lower() not in card_heading.lower():
            return False

    return True


# Substring in WS error message that signals the dashboard identifier was not
# accepted by lovelace/config (e.g., caller passed an internal id where url_path
# is expected). Used to gate the lazy resolver fallback in get/set tools.
#
# Source: homeassistant/components/lovelace/websocket.py, _handle_errors —
# emits f"Unknown config specified: {url_path}" paired with structured
# error.code "config_not_found". The client envelope does surface that code
# top-level (``error_code``), but HA reuses it for the no-stored-config case
# ("No config found." — an auto-generated dashboard), so the code alone cannot
# identify an unresolved identifier; the message substring remains the only
# discriminating signal. If HA reformats this string, the lazy fallback
# regresses silently to never firing — re-verify with major HA upgrades.
_LAZY_RESOLVE_TRIGGER = "Unknown config specified"

# Exact ``lovelace/config`` messages meaning that a storage dashboard exists
# but has not been given a config yet. Mirrors smart_search/_deep.py and stays
# deliberately narrower than ``_LAZY_RESOLVE_TRIGGER``: an unknown path or any
# other pre-read failure must remain fail-closed before a full replacement.
_NO_STORED_CONFIG_MESSAGES = frozenset(
    {"No config found.", "Command failed: No config found."}
)
_DASHBOARD_FETCH_FAILED_PREFIX = "Dashboard fetch failed: "
_HA_CONFIG_NOT_FOUND_CODE = "config_not_found"


def _should_lazy_resolve(error_msg: str) -> bool:
    """Return True if a WS error message indicates the identifier needs resolving."""
    return _LAZY_RESOLVE_TRIGGER in error_msg


def _is_no_stored_dashboard_config_error(exc: ToolError) -> bool:
    """Return whether HA confirms that an existing dashboard has no config."""
    try:
        error_data = json.loads(str(exc))
    except (json.JSONDecodeError, TypeError):
        return False
    if (
        not isinstance(error_data, dict)
        or error_data.get("ha_error_code") != _HA_CONFIG_NOT_FOUND_CODE
    ):
        return False
    message = extract_tool_error_message(exc).removeprefix(
        _DASHBOARD_FETCH_FAILED_PREFIX
    )
    return message in _NO_STORED_CONFIG_MESSAGES


# The ha_mcp_tools/dashboards WS command: list / get / search over the live
# lovelace collection in one in-process frame. Named once so the routing helpers
# and their tests agree on the wire string (Global-Constraint-2 idiom, mirroring
# ``component_devices.WS_DEVICE_GET``).
WS_DASHBOARDS = "ha_mcp_tools/dashboards"

# ``LovelaceConfig.mode`` wire string for a storage dashboard. On both paths the
# ``list`` rows normally carry ``mode``: the ha_mcp_tools component tags every
# runtime row, and the legacy ``lovelace/dashboards/list`` rows are the
# ``LovelaceConfig.config`` dicts, where core's schemas stamp it (storage items
# default ``mode: storage`` in ``STORAGE_DASHBOARD_CREATE_FIELDS``; YAML entries
# REQUIRE ``mode: yaml`` in ``YAML_DASHBOARD_SCHEMA`` — verified against
# home-assistant/core ``lovelace/{dashboard,const,__init__}.py``). A YAML
# dashboard's BODY may carry resolved ``!secret`` plaintext, so the
# cross-dashboard ``search`` walk reads a row's body ONLY when it is EXPLICITLY
# tagged ``storage`` — fail-closed, which also skips the rare UNTAGGED row
# (storage items persisted before core's mode default existed) rather than risk
# reading a body it can't prove is storage. ``list`` still surfaces YAML rows,
# since listing metadata is safe.
_DASHBOARD_STORAGE_MODE = "storage"

# Card search match cap, so one response stays bounded.
_SEARCH_MATCH_CAP = 200


async def _dashboards_via_component(
    client: Any,
    mode: str,
    *,
    url_path: str | None = None,
    query: str | None = None,
) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/dashboards`` read; ``None`` ⇒ use the legacy path.

    Global-Constraint-2 idiom (mirrors
    ``component_devices.fetch_device_via_component``). Returns the component's
    ``result`` dict for ``mode`` (``list`` / ``get`` / ``search``) when the
    component advertises the ``dashboards`` capability AND reports
    ``available: true`` (the lovelace integration is set up). Returns ``None`` —
    the caller runs its unchanged legacy path — on capability miss, downgrade
    (``unknown_command`` → invalidate the cached caps), command error/timeout
    (logged), a connection-establishment failure (logged), a malformed
    envelope, or ``available: false`` (lovelace not set up). A
    ``HomeAssistantConnectionError`` — a pooled-WS drop, or a failed
    (re)connect — is caught here and mapped to ``None``: the legacy dashboards
    path rides the
    ``send_websocket_message`` bridge (and the auto-backup capture consumer
    forbids a blocked write), so a wedged component read must fall back to
    legacy rather than escape into a set/delete write path. If the transport
    itself is dead the legacy bridge raises in turn (#1947) and the write
    fails loud, which is the correct outcome for a write that cannot be
    confirmed.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "dashboards"):
        return None
    kwargs: dict[str, Any] = {"mode": mode}
    if url_path is not None:
        kwargs["url_path"] = url_path
    if query is not None:
        kwargs["query"] = query
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        raw = await ws.send_command(WS_DASHBOARDS, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_DASHBOARDS, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        # HomeAssistantConnectionError: a pooled-WS drop or a failed
        # (re)connect. Fall back to the
        # legacy bridge rather than escape here; if the transport is genuinely
        # dead the bridge raises there instead (#1947).
        logger.warning(
            "%s connection error; falling back to legacy: %r", WS_DASHBOARDS, exc
        )
        return None
    result = raw.get("result")
    if not isinstance(result, dict) or not result.get("available"):
        logger.debug(
            "%s (mode=%s) returned unavailable/malformed result; falling back to legacy",
            WS_DASHBOARDS,
            mode,
        )
        return None
    return result


async def _component_dashboard_rows(client: Any) -> list[dict[str, Any]] | None:
    """All dashboard metadata rows via the component ``list``; ``None`` ⇒ legacy.

    The legacy ``lovelace/dashboards/list`` returns a metadata row for every
    dashboard that has a config — YAML dashboards included — so the component
    ``list`` is passed through with YAML rows KEPT. Dropping them diverged the two
    paths (a YAML dashboard vanished from ``list_only`` output only when the
    component was installed). Listing metadata is safe: only a dashboard's BODY can
    carry resolved ``!secret`` plaintext, and the body-serving paths still exclude
    YAML — ``get`` via the component's ``yaml_excluded`` status and ``search`` by
    reading only ``mode == "storage"`` rows. The additive ``mode`` tag is preserved
    on every row so those exclusions can key off it. ``None`` (component unavailable
    / malformed) routes the caller to the legacy list read.
    """
    result = await _dashboards_via_component(client, "list")
    if result is None:
        return None
    rows = result.get("dashboards")
    if not isinstance(rows, list):
        logger.debug(
            "%s list returned a non-list 'dashboards' (%s); falling back to legacy",
            WS_DASHBOARDS,
            type(rows).__name__,
        )
        return None
    return [row for row in rows if isinstance(row, dict)]


async def _component_dashboard_config(
    client: Any, url_path: str | None
) -> dict[str, Any] | None:
    """One dashboard's config via the component ``get``; ``None`` ⇒ legacy.

    Returns the config body ONLY when the component is available and reports
    ``status == "ok"``. ``yaml_excluded`` (a YAML body may carry resolved
    ``!secret`` plaintext — the legacy read is authoritative for it) and
    ``not_found`` (let the legacy path produce the real error or lazy-resolve an
    internal id) both return ``None`` so the caller runs its unchanged legacy
    read. ``url_path`` ``None`` targets the default dashboard.
    """
    result = await _dashboards_via_component(client, "get", url_path=url_path)
    if result is None or result.get("status") != "ok":
        return None
    config = result.get("config")
    if not isinstance(config, dict):
        logger.debug(
            "%s get returned status=ok but a non-dict config for url_path=%r; "
            "falling back to legacy",
            WS_DASHBOARDS,
            url_path,
        )
        return None
    return config


async def fetch_dashboards_list(
    client: Any,
) -> list[dict[str, Any]] | None:
    """Fetch and normalise the lovelace/dashboards/list WebSocket response.

    Returns the list of dashboard registry entries on success, or ``None``
    when the response shape is unrecognised.  A warning is logged on
    unexpected shapes so that future HA response-format changes surface at
    every fetch site rather than silently degrading.

    When the ``ha_mcp_tools`` component advertises the ``dashboards`` capability
    the rows — YAML rows included, tagged with ``mode`` (see
    ``_component_dashboard_rows``) — are served from one in-process ``list`` frame (the
    ``_resolve_dashboard`` / ``_lookup_existing_dashboards`` / list-mode callers
    all funnel here); otherwise the legacy ``lovelace/dashboards/list`` WS read
    runs unchanged.

    Callers decide how to handle ``None`` (e.g. fall through to ``[]`` or
    propagate the failure).
    """
    component_rows = await _component_dashboard_rows(client)
    if component_rows is not None:
        return component_rows

    result = await client.send_websocket_message({"type": "lovelace/dashboards/list"})
    if isinstance(result, dict) and isinstance(result.get("result"), list):
        return cast(list[dict[str, Any]], result["result"])
    if isinstance(result, list):
        return cast(list[dict[str, Any]], result)
    logger.warning(
        "lovelace/dashboards/list returned an unexpected shape (type=%s); "
        "treating as no-match",
        type(result).__name__,
    )
    return None


def _raise_dashboard_registry_read_error(*, action: str, url_path: str) -> NoReturn:
    """Raise the shared user-facing error for an unreadable dashboard registry."""
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Failed to read the Home Assistant dashboard registry",
            suggestions=[
                "Retry the operation",
                "Check the Home Assistant connection and logs",
                "Use ha_config_get_dashboard(list_only=True) to verify registry access",
            ],
            context={"action": action, "url_path": url_path},
        )
    )


async def _resolve_dashboard(
    client: Any, identifier: str
) -> tuple[dict[str, str] | None, list[dict[str, Any]] | None]:
    """Resolve a dashboard identifier (url_path or internal id) to both forms.

    Calls ``lovelace/dashboards/list`` and returns a 2-tuple
    ``(match, dashboards)``:

    - ``match`` is ``{"url_path": ..., "id": ...}`` when the identifier
      matches either field on a registry entry that has both fields
      populated; otherwise ``None``.
    - ``dashboards`` is the raw list as returned by HA when the
      response shape is recognised (dict-with-``result`` or bare list);
      ``None`` when the shape was unexpected and a warning was logged.

    Returning ``dashboards`` alongside ``match`` lets callers reuse the
    list for follow-on checks (existence, id lookup) instead of paying
    a second ``lovelace/dashboards/list`` round-trip.

    Three call sites:
    - **Lazy fallback** (``_lazy_resolve_and_retry``): only invoked after
      ``lovelace/config`` rejected the identifier with
      ``_LAZY_RESOLVE_TRIGGER`` — the round-trip is gated by the caller.
      Discards ``dashboards``.
    - **Eager pre-resolve** (``_resolve_set_dashboard_url_path``, called
      from ``ha_config_set_dashboard``): invoked before hyphen validation
      so callers may pass either form; gated on a cheap heuristic ("no
      hyphen, not 'lovelace'") rather than an error from HA. Reuses
      ``dashboards`` for the existence-check in ``_lookup_existing_dashboards``
      (threaded through as ``pre_fetched_dashboards``).
    - **Delete** (``ha_config_delete_dashboard``): resolves either form
      to the registry id before issuing the delete. Discards
      ``dashboards``.
    """
    dashboards = await fetch_dashboards_list(client)
    if dashboards is None:
        return None, None

    for d in dashboards:
        if d.get("id") == identifier or d.get("url_path") == identifier:
            url_path = d.get("url_path") or ""
            entry_id = d.get("id") or ""
            if not url_path or not entry_id:
                # Malformed registry entry — neither form is safe to
                # forward. Skip rather than return empty strings that
                # would be silently used by callers (e.g.
                # ``delete_dashboard`` would forward ``resolved_id=""``).
                continue
            return {"url_path": url_path, "id": entry_id}, dashboards
    return None, dashboards


@overload
async def _lazy_resolve_and_retry(
    client: Any,
    url_path: str,
    ws_data: dict[str, Any],
    response: Any,
) -> tuple[str, Any]:
    pass


@overload
async def _lazy_resolve_and_retry(
    client: Any,
    url_path: None,
    ws_data: dict[str, Any],
    response: Any,
) -> tuple[None, Any]:
    pass


async def _lazy_resolve_and_retry(
    client: Any,
    url_path: str | None,
    ws_data: dict[str, Any],
    response: Any,
) -> tuple[str | None, Any]:
    """Trigger-gated lazy resolve + single retry of a lovelace/config call.

    If `response` indicates HA rejected the identifier with the
    _LAZY_RESOLVE_TRIGGER substring, resolves `url_path` via
    lovelace/dashboards/list and retries the WS call with the canonical
    url_path. Returns the (possibly updated) url_path and the
    (possibly retried) response so the caller can chain naturally:

        url_path, response = await _lazy_resolve_and_retry(
            client, url_path, ws_data, response
        )

    No-op when:
    - the response is not a failure (success=True or non-dict),
    - ``url_path`` is empty,
    - the error message does not contain ``_LAZY_RESOLVE_TRIGGER``
      (the substring miss),
    - the resolver finds no match,
    - or the resolver itself raises (logged at WARNING).

    In every no-op case the original ``response`` is returned unchanged
    so the caller's existing error-handling path runs against the real
    HA error rather than a synthetic "resolver failed" one.

    The caller's `ws_data` dict is never mutated: when a retry is needed,
    a shallow copy is made and the canonical `url_path` written into the
    copy before the retry call.
    """
    if not (isinstance(response, dict) and not response.get("success", True)):
        return url_path, response
    if not url_path:
        return url_path, response

    err = response.get("error", {})
    err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    if not _should_lazy_resolve(err_msg):
        return url_path, response

    try:
        resolved, _ = await _resolve_dashboard(client, url_path)
    except Exception as resolver_exc:  # noqa: BLE001
        # Resolver itself raised (timeout, network blip, etc.). Don't let
        # this exception escape and replace the original HA error with
        # one about the resolver — fall through with the original
        # response so the caller surfaces the actual "Unknown config
        # specified" error.
        logger.warning(
            "Lazy resolver failed for url_path=%r: %s; "
            "falling through to original error",
            url_path,
            resolver_exc,
        )
        return url_path, response

    if resolved is None or not resolved["url_path"]:
        return url_path, response

    url_path = resolved["url_path"]
    retry_data = dict(ws_data)
    retry_data["url_path"] = url_path
    response = await client.send_websocket_message(retry_data)
    return url_path, response


def _attach_dashboard_render_paths(
    result: dict[str, Any],
    url_path: str | None,
    config: dict[str, Any] | None,
) -> None:
    """Attach canonical per-view render routes without mutating config."""
    render_paths, warnings = dashboard_render_paths(url_path, config)
    result["render_paths"] = render_paths
    if warnings:
        result.setdefault("warnings", []).extend(warnings)


async def _attach_dashboard_render_paths_after_write(
    client: Any,
    result: dict[str, Any],
    url_path: str,
    fallback_config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Attach authoritative post-write routes without masking a committed write.

    Home Assistant may normalize the submitted dashboard config. Always read it
    back before claiming canonical render paths. The submitted config remains a
    screenshot-targeting fallback only when that readback fails.
    """
    result.update(config_hash=None, write_committed=True, post_write_verified=False)
    try:
        authoritative_config, config_hash = await _get_dashboard_config_internal(
            client, url_path
        )
    except ToolError as exc:
        result.setdefault("warnings", []).append(
            "Canonical render paths unavailable after the dashboard write: "
            f"{extract_tool_error_message(exc)}"
        )
        return fallback_config
    except Exception as exc:
        # The dashboard write already committed. Metadata enrichment must not
        # turn a successful mutation into a reported failure when the follow-up
        # read hits a raw transport or parsing exception.
        logger.warning(
            "Could not fetch canonical render paths after writing %s: %s",
            url_path,
            exc,
            exc_info=True,
        )
        result.setdefault("warnings", []).append(
            f"Canonical render paths unavailable after the dashboard write: {exc}"
        )
        return fallback_config
    result.update(config_hash=config_hash, post_write_verified=True)
    _attach_dashboard_render_paths(result, url_path, authoritative_config)
    return authoritative_config


def _note_screenshot_ignored(
    result: dict[str, Any],
    *,
    include_screenshot: bool,
    full_page: bool = False,
    options: _DashboardScreenshotOptions | None = None,
    mode: str,
) -> None:
    """Warn when get-mode-only options are passed to a mode that ignores them.

    Screenshot options — and ``view_path``, which is a get-mode scoping
    parameter that merely travels inside the options dataclass — are only
    honoured in get mode. In list and search mode they are accepted but
    inapplicable, so surface a ``warnings`` entry rather than dropping the
    request as a silent no-op (matches the warn-don't-fail contract the
    params document)."""
    capture_options = options or _DashboardScreenshotOptions(full_page=full_page)
    if include_screenshot or capture_options != _DashboardScreenshotOptions():
        result.setdefault("warnings", []).append(
            f"include_screenshot, view_path, and screenshot render options are "
            f"ignored in {mode} mode; call "
            "ha_config_get_dashboard with a url_path (and no search criteria) "
            "to scope to a view or get a screenshot."
        )


async def _capture_dashboard_screenshot_result(
    result: dict[str, Any],
    url_path: str | None,
    *,
    client: Any | None,
    config: dict[str, Any] | None,
    options: _DashboardScreenshotOptions,
) -> ToolResult:
    """Render configured captures and attach their ordered MCP metadata."""
    from ..dashboard_screenshot import capture as screenshot_capture

    render_path = dashboard_frontend_path(url_path)
    if options.view_path is not None or config is not None:
        if config is None:
            if client is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        "Dashboard client is unavailable for view_path resolution.",
                    )
                )
            config, _ = await _get_dashboard_config_internal(client, url_path)
        target = resolve_dashboard_view(
            url_path or "default", config, options.view_path
        )
        render_path = target.render_path
        if target.warnings:
            result.setdefault("warnings", []).extend(target.warnings)

    capture_failures: list[dict[str, Any]] = []
    guard_warnings: list[str] = []
    captures = await screenshot_capture.capture_dashboard_images(
        render_path,
        width=options.width,
        height=options.height,
        viewport_presets=options.viewport_presets,
        orientation=options.orientation,
        zoom=options.zoom,
        wait_ms=options.wait_ms,
        full_page=options.full_page,
        theme=options.theme,
        dark_mode=options.dark_mode,
        language=options.language,
        image_format=options.image_format,
        render_timeout_seconds=options.render_timeout_seconds,
        partial_failures=capture_failures,
        client=client,
        capture_warnings=guard_warnings,
    )
    # Build every fallible image/metadata object before publishing screenshot
    # fields. The write path can then degrade serialization failures to a
    # warning without returning content_index entries for images that vanished.
    try:
        image_content = dashboard_image_content(captures)
        screenshot_metadata = dashboard_screenshot_metadata(captures, render_path)
        structured_result = {
            **result,
            "screenshot_render_path": render_path,
            "screenshots": screenshot_metadata,
        }
        if capture_failures:
            structured_result["screenshot_partial"] = True
            structured_result["screenshot_failures"] = capture_failures
        capture_warnings = [
            *dashboard_screenshot_warnings(captures),
            *guard_warnings,
        ]
        if capture_warnings:
            structured_result["warnings"] = [
                *structured_result.get("warnings", []),
                *capture_warnings,
            ]
        return ToolResult(
            content=image_content,
            structured_content=structured_result,
        )
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        error_payload = create_error_response(
            ErrorCode.IMAGE_SERIALIZATION_FAILED,
            "Rendered dashboard images could not be packaged into the MCP response.",
            details=str(exc),
            context={"capture_count": len(captures), "render_path": render_path},
        )
        if guard_warnings:
            # The render already happened, so a theme-guard warning (e.g. a
            # failed restore) must stay visible even when packaging fails.
            error_payload["warnings"] = list(guard_warnings)
        raise_tool_error(error_payload)
    # raise_tool_error is typed -> NoReturn, but CodeQL cannot see that, so it
    # reports py/mixed-returns for the implicit None fall-through past the
    # except block. Keep this terminal statement to suppress the false positive.
    raise AssertionError("unreachable: raise_tool_error always raises")


async def _maybe_attach_screenshot(
    result: dict[str, Any],
    url_path: str | None,
    requested: bool,
    *,
    client: Any | None = None,
    config: dict[str, Any] | None = None,
    options: _DashboardScreenshotOptions | None = None,
    full_page: bool = False,
    raise_on_failure: bool = False,
) -> "dict[str, Any] | ToolResult":
    """Optionally render a dashboard and attach ordered native image blocks.

    Shared by ``ha_config_get_dashboard`` (include_screenshot) and
    ``ha_config_set_dashboard`` (return_screenshot). On success returns a
    FastMCP ``ToolResult`` carrying ``result`` as structured_content plus the
    images as content blocks. Render metadata is added to the existing result
    dict, preserving its structured response contract.

    ``raise_on_failure`` governs what a capture failure does. The set path
    (``return_screenshot``) leaves it False: a screenshot failure must never
    break a write that already committed, so it degrades to a ``warnings``
    entry. The get path (``include_screenshot``) passes True: it does not commit
    a dashboard/config write, and the screenshot is the requested payload, so
    an engine failure propagates as a ToolError (matching the dedicated
    ``ha_get_dashboard_screenshot`` tool) instead of being demoted to a
    warning the caller may never inspect. A disabled feature flag is always a
    warning either way — it is an expected configuration state, not a failure.
    """
    capture_options = options or _DashboardScreenshotOptions(full_page=full_page)
    if options is not None and full_page and not capture_options.full_page:
        capture_options = replace(capture_options, full_page=True)

    if not requested:
        defaults = _DashboardScreenshotOptions()
        if capture_options != defaults:
            result.setdefault("warnings", []).append(
                "Screenshot render options are ignored because no screenshot "
                "was requested "
                "(set include_screenshot / return_screenshot to use it)."
            )
        return result

    from ..config import get_global_settings

    if not get_global_settings().enable_dashboard_screenshot:
        result.setdefault("warnings", []).append(
            "Screenshot requested but dashboard screenshot mode is disabled. "
            "Enable the 'dashboard screenshot' beta feature to use it."
        )
        return result

    try:
        return await _capture_dashboard_screenshot_result(
            result,
            url_path,
            client=client,
            config=config,
            options=capture_options,
        )
    except ToolError as e:
        if raise_on_failure:
            raise
        return _attach_screenshot_tool_error(result, e)
    except Exception as e:
        # On the set path a screenshot failure must never break a write that
        # already committed, so catch everything non-ToolError (lazy import
        # errors, Image construction, timeouts, transport) and degrade to a
        # warning. On the get path (raise_on_failure) there is nothing to
        # protect, so let it surface.
        if raise_on_failure:
            raise
        logger.warning("Dashboard screenshot capture failed: %s", e, exc_info=True)
        result["screenshot_error"] = {
            "code": ErrorCode.INTERNAL_ERROR.value,
            "message": str(e),
        }
        result.setdefault("warnings", []).append(f"Screenshot unavailable: {e}")
        return result


def _attach_screenshot_tool_error(
    result: dict[str, Any], error: ToolError
) -> dict[str, Any]:
    """Preserve a structured capture failure on an already-committed write."""
    try:
        error_payload = json.loads(str(error))
    except (json.JSONDecodeError, TypeError):
        error_payload = {}
    if not isinstance(error_payload, dict):
        error_payload = {}
    structured_error = error_payload.get("error", {})
    if not isinstance(structured_error, dict):
        structured_error = {}
    screenshot_error: dict[str, Any] = {
        "code": structured_error.get("code", ErrorCode.INTERNAL_ERROR.value),
        "message": structured_error.get("message", str(error)),
    }
    screenshot_error.update(
        {
            key: value
            for key, value in error_payload.items()
            if key not in {"success", "error", "warnings"}
        }
    )
    result["screenshot_error"] = screenshot_error
    result.setdefault("warnings", []).append(
        f"Screenshot unavailable: {extract_tool_error_message(error)}"
    )
    # Theme-guard warnings ride on the error payload (a failing batch may
    # already have rendered and clobbered the engine user's theme); keep them
    # visible on the degraded-to-warning path instead of burying them in
    # screenshot_error.
    payload_warnings = error_payload.get("warnings")
    if isinstance(payload_warnings, list):
        result["warnings"].extend(str(warning) for warning in payload_warnings)
    return result


def _search_dashboard_docs(
    docs: list[dict[str, Any]], criteria: dict[str, str | None], *, per_dashboard: bool
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Matches over ``docs`` plus the truncated and uncovered locations.

    ``per_dashboard`` stamps each match with its dashboard's ``url_path`` and
    ``config_hash`` and prefixes each location with ``<url_path>:``.
    """
    query = criteria["query"]
    matches: list[dict[str, Any]] = []
    truncation: list[str] = []
    uncovered: list[str] = []
    for doc in docs:
        doc_truncation: list[str] = []
        doc_uncovered: list[str] = []
        found = _find_cards_in_config(
            doc["config"],
            criteria["entity_id"],
            criteria["card_type"],
            criteria["heading"],
            truncation=doc_truncation,
            uncovered=doc_uncovered,
            query=query.lower() if query is not None else None,
        )
        prefix = ""
        if per_dashboard:
            # The default dashboard has no url_path; "default" is what the get
            # and set tools accept for it.
            url_path = doc["url_path"] or "default"
            prefix = f"{url_path}:"
            config_hash = compute_config_hash(doc["config"]) if found else None
            for match in found:
                match["url_path"] = url_path
                match["config_hash"] = config_hash
        matches.extend(found)
        truncation.extend(prefix + path for path in doc_truncation)
        uncovered.extend(prefix + path for path in doc_uncovered)
    return matches, truncation, uncovered


def _search_warnings(
    *, truncated: bool, truncation: list[str], uncovered: list[str]
) -> list[str]:
    """Warn-don't-truncate (.gemini/styleguide.md Tool Tags and Return Values).

    A capped or depth-truncated search, or a card-criteria search over cards
    whose picture-elements content it does not match, must not look complete.
    Disclosure keys off the *presence* of such a shape, not off a 0-match.
    """
    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"Results capped at {_SEARCH_MATCH_CAP} matches; narrow the search "
            "(url_path, card_type, query) for a complete list."
        )
    if truncation:
        warnings.append(
            f"Search stopped at the nesting depth bound "
            f"(_MAX_CARD_DEPTH={_MAX_CARD_DEPTH}) in "
            f"{len(truncation)} place(s); cards nested deeper were not "
            "searched, so results may be incomplete."
        )
    if uncovered:
        locations = ", ".join(sorted(set(uncovered)))
        warnings.append(
            "entity_id, card_type and heading do not match inside "
            f"picture-elements 'elements', present at: {locations}. Use query= "
            "to search their text, or fetch the full config to inspect them."
        )
    return warnings


class DashboardConfigTools:
    """Home Assistant dashboard configuration tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_get_dashboard",
        tags={"Dashboards"},
        annotations=read_only_hints("Get Dashboard", open_world=True),
    )
    @log_tool_usage
    async def ha_config_get_dashboard(
        self,
        url_path: Annotated[
            str | None,
            Field(
                description="Dashboard URL path (e.g., 'lovelace-home'). Use 'default' for default "
                "dashboard."
            ),
        ] = None,
        list_only: Annotated[
            bool,
            Field(
                description="When True, url_path is ignored.",
            ),
        ] = False,
        force_reload: Annotated[
            bool,
            Field(
                description="Force reload from storage (bypass cache). Not applicable in search mode."
            ),
        ] = False,
        entity_id: Annotated[
            str | None,
            Field(
                description="Find cards by entity ID. Supports wildcards, e.g. "
                "'sensor.temperature_*'. Matches cards with this entity in "
                "'entity' or 'entities' field, view-level badges, and header cards."
            ),
        ] = None,
        card_type: Annotated[
            str | None,
            Field(description="Find cards by type, e.g. 'tile', 'button', 'heading'."),
        ] = None,
        heading: Annotated[
            str | None,
            Field(
                description="Find cards by heading/title text (case-insensitive partial match)."
            ),
        ] = None,
        include_config: Annotated[
            bool,
            Field(
                description="In search mode: include each matched card's own configuration object "
                "in results. A container card's body includes its descendants, which "
                "are also separate matches with their own bodies, so nested stacks "
                "multiply the payload. Bodies are returned only for dashboards provably"
                " in storage mode; for a YAML or unconfirmed dashboard they are "
                "withheld (they may carry resolved !secret values) and the response "
                "says so, with match locations still reported. Ignored outside search "
                "mode."
            ),
        ] = False,
        include_screenshot: Annotated[
            bool,
            Field(
                description="Get mode only: also return rendered image(s) of the dashboard for "
                "visual verification. Requires the 'dashboard screenshot' beta feature "
                "+ engine app (add-on)/sidecar. If the feature is disabled the config "
                "is returned with a warning; if the engine is configured but the render"
                " fails, the call errors. Ignored in list/search mode."
            ),
        ] = False,
        view_path: Annotated[
            str | None,
            Field(
                description="Get mode: return ONLY the view whose Lovelace views[].path matches "
                "(response carries 'view' + 'view_index' instead of the full 'config')."
                " With include_screenshot, also selects the view to render. Ignored in "
                "list/search mode."
            ),
        ] = None,
        mode: Annotated[
            Literal["search"] | None,
            Field(description="Optional; any search parameter already searches."),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="Search: text or entity_id to find in any value a card "
                "holds (case-insensitive substring). Searches every storage "
                "dashboard unless url_path is given."
            ),
        ] = None,
        describe: Annotated[
            bool,
            Field(description="Return card_type's fields from HA's card editor"),
        ] = False,
    ) -> "dict[str, Any] | ToolResult":
        """Get dashboard info - list all dashboards, get config, or search for cards.

        MODE 1 — List: list_only=True
          Lists every dashboard's metadata (url_path, title, icon), storage and
          YAML alike (metadata only — bodies are never included here).

        MODE 2 — Search: any of query / entity_id / card_type / heading
          Finds cards, badges, and header cards in one dashboard (url_path) or,
          without url_path, in every storage-mode dashboard. Cards nested at any
          depth are searched (stacks, grids, conditional cards, button-card
          custom_fields, state-switch states, and custom cards' own keys such as
          groups[].cards[].card). Criteria are AND-ed: query matches any value
          the card holds (listed under `matched`); entity_id matches the
          card's entity/entities (wildcards allowed), badges, and header cards.
          Each match carries a python_path and a jq_path. The python_path is a
          Python subscript chain to be appended after `config` — e.g.
          python_transform=f'config{m["python_path"]}["icon"] = "mdi:x"' (it is
          NOT valid on its own without the `config` prefix). jq_path is the same
          location in jq dot-notation. A one-dashboard search returns its
          config_hash; across dashboards each match carries url_path and its
          dashboard's config_hash. Always fetches fresh config. Capped at 200
          matches (`truncated`). YAML-mode dashboards are searched only by
          url_path, with card bodies withheld (HA resolves `!secret` in them).
          Without the ha_mcp_tools component, the default dashboard is searched
          only by url_path. Strategy dashboards have no explicit cards.

        MODE 3 — Get: Active when list_only=False and no search parameters are provided.
          Returns the full Lovelace dashboard config, defaulting to the
          main dashboard if url_path is omitted.
          With view_path, `config_hash` still covers the FULL config, so a follow-up
          ha_config_set_dashboard(python_transform=...) addressing
          config['views'][view_index] validates unchanged. An unknown
          view_path errors and lists the available view paths.
          When you only need the render and not the config, use the
          dedicated ha_get_dashboard_screenshot tool (registered when the
          dashboard screenshot beta feature is on) instead; it returns images
          without echoing the config.

        MODE 2 (Search) and MODE 3 (Get) return a `config_hash` that stays the same across consecutive reads of an unchanged config; MODE 1 does not return one.

        EXAMPLES:
        - List all dashboards: ha_config_get_dashboard(list_only=True)
        - Get one view only: ha_config_get_dashboard(url_path="lovelace-mobile", view_path="office")
        - Find cards by entity (wildcards allowed): ha_config_get_dashboard(url_path="my-dash", entity_id="sensor.temperature_*")
        - Find heading: ha_config_get_dashboard(url_path="my-dash", heading="Climate", card_type="heading")
        - Which dashboards use an entity: ha_config_get_dashboard(query="light.bedroom")
        - Fields of a card type before writing one (omit card_type to list types):
          ha_config_get_dashboard(card_type="tile", describe=True)
        """
        if describe:
            return await describe_card_response(self._client, card_type)
        screenshot_options = _DashboardScreenshotOptions(view_path=view_path)
        criteria: dict[str, str | None] = {
            "query": query,
            "entity_id": entity_id,
            "card_type": card_type,
            "heading": heading,
        }
        search_mode = mode == "search" or any(
            value is not None for value in criteria.values()
        )
        # Mutable single-element holder so the mode helpers can surface the
        # lazy-resolved/canonicalized url_path back to this scope even when
        # they raise an unexpected (non-ToolError) exception instead of
        # returning normally — the outer except block below needs it for
        # accurate error context.
        resolved_url_path: list[str | None] = [url_path]
        try:
            if list_only and not search_mode:
                return await self._get_dashboard_list_mode(
                    include_screenshot=include_screenshot,
                    screenshot_options=screenshot_options,
                )

            # ``url_path`` is optional in this tool (omitted with
            # ``list_only=True`` lists all dashboards — handled above; omitted
            # without ``list_only`` falls back to the default dashboard via
            # the resolver below). When provided, reject empty/whitespace
            # up-front so the caller gets a structured parameter error
            # instead of a misleading ``RESOURCE_NOT_FOUND``. Extension of
            # the #1312 validate_identifier_not_empty pattern to the
            # dashboards family per #1313.
            if url_path is not None:
                validate_identifier_not_empty(
                    url_path,
                    "url_path",
                    suggestions=[
                        "Pass a dashboard URL path (e.g. 'lovelace-home')",
                        "Omit url_path and pass list_only=True to list dashboards",
                        "Use 'default' to target the default dashboard",
                    ],
                )

            if search_mode:
                return await self._get_dashboard_search_mode(
                    url_path,
                    resolved_url_path=resolved_url_path,
                    criteria=criteria,
                    include_config=include_config,
                    include_screenshot=include_screenshot,
                    screenshot_options=screenshot_options,
                )

            return await self._get_dashboard_get_mode(
                url_path,
                resolved_url_path=resolved_url_path,
                force_reload=force_reload,
                view_path=view_path,
                include_screenshot=include_screenshot,
                screenshot_options=screenshot_options,
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            effective_url_path = resolved_url_path[0]
            context: dict[str, Any]
            if search_mode:
                suggestions = [
                    "Check HA connection",
                    "Verify dashboard with ha_config_get_dashboard(list_only=True)",
                ]
                context = {
                    "action": "search",
                    "url_path": effective_url_path,
                    **criteria,
                }
            else:
                suggestions = [
                    "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                    "Check if you have permission to access this dashboard",
                    "Use url_path='default' for default dashboard",
                ]
                context = {
                    "action": "get" if not list_only else "list",
                    "url_path": effective_url_path,
                }
            exception_to_structured_error(
                e,
                context=context,
                suggestions=suggestions,
            )
            return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    async def _get_dashboard_list_mode(
        self,
        *,
        include_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> dict[str, Any]:
        """``list_only=True`` mode: list every dashboard's metadata row.

        Storage and YAML dashboards alike (metadata only — no bodies — so a
        YAML dashboard's resolved ``!secret`` never surfaces here), matching the
        legacy ``lovelace/dashboards/list`` row set.
        """
        dashboards = await fetch_dashboards_list(self._client) or []
        list_result: dict[str, Any] = {
            "success": True,
            "action": "list",
            "dashboards": dashboards,
            "count": len(dashboards),
        }
        _note_screenshot_ignored(
            list_result,
            include_screenshot=include_screenshot,
            options=screenshot_options,
            mode="list",
        )
        return list_result

    async def _fetch_search_dashboard_config(
        self, url_path: str | None
    ) -> tuple[dict[str, Any], str | None, str | None]:
        """Fetch + resolve the dashboard config for search mode.

        Returns ``(config, url_path, search_resolved_from)`` — ``url_path`` is
        the canonicalized identifier (post lazy-resolve) and
        ``search_resolved_from`` is the original caller-passed identifier when
        it differed from the canonical form, else ``None``.
        """
        get_data: dict[str, Any] = {"type": "lovelace/config", "force": True}
        effective_url_path: str | None = (
            url_path if url_path and url_path != "default" else None
        )
        if effective_url_path is not None:
            get_data["url_path"] = effective_url_path

        response = await self._client.send_websocket_message(get_data)

        # Lazy resolver fallback: same gate as get-mode. If the caller passed
        # an internal id where url_path is expected, HA rejects with the
        # trigger substring; resolve and retry once. (set_dashboard handles
        # this via an eager pre-resolver before the hyphen check, so it has
        # no equivalent fallback here.)
        search_resolved_from: str | None = None
        if effective_url_path is not None:
            new_url_path, response = await _lazy_resolve_and_retry(
                self._client, effective_url_path, get_data, response
            )
            if new_url_path != effective_url_path:
                # Surface the original caller-passed identifier so the
                # caller can see their input was canonicalized.
                search_resolved_from = url_path
                url_path = new_url_path

        if isinstance(response, dict) and not response.get("success", True):
            error_msg = response.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to get dashboard: {error_msg}",
                    suggestions=[
                        "Verify dashboard exists with ha_config_get_dashboard(list_only=True)",
                        "Check HA connection",
                    ],
                    context={"action": "search", "url_path": url_path},
                )
            )

        config = response.get("result") if isinstance(response, dict) else response
        if not isinstance(config, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Dashboard config is empty or invalid",
                    suggestions=["Initialize dashboard with ha_config_set_dashboard"],
                    context={"action": "search", "url_path": url_path},
                )
            )

        if "strategy" in config:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    "Strategy dashboards have no explicit cards to search",
                    suggestions=[
                        "Use 'Take Control' in HA UI to convert to editable",
                        "Or create a non-strategy dashboard",
                    ],
                    context={"action": "search", "url_path": url_path},
                )
            )

        return config, url_path, search_resolved_from

    @staticmethod
    def _build_search_result(
        docs: list[dict[str, Any]],
        *,
        criteria: dict[str, str | None],
        include_config: bool,
        scoped_url_path: str | None = None,
        config_suppressed_note: str | None = None,
    ) -> dict[str, Any]:
        """Run the card search over ``docs`` (``{url_path, config}``) and assemble it.

        One dashboard (``scoped_url_path`` set) reports its ``config_hash`` at the
        top level; a search across dashboards stamps each match with its own
        dashboard's ``url_path`` and ``config_hash``. ``config_suppressed_note``
        (a scoped dashboard not provably storage-mode) withholds card bodies even
        with ``include_config=True`` — a YAML dashboard may carry HA-resolved
        ``!secret`` plaintext. Match LOCATIONS are always reported.
        """
        matches, truncation, uncovered = _search_dashboard_docs(
            docs, criteria, per_dashboard=scoped_url_path is None
        )
        truncated = len(matches) > _SEARCH_MATCH_CAP
        matches = matches[:_SEARCH_MATCH_CAP]
        if not include_config or config_suppressed_note is not None:
            for match in matches:
                del match["card_config"]

        warnings = _search_warnings(
            truncated=truncated,
            truncation=truncation,
            # query reads the text of picture-elements 'elements'; the other
            # criteria match cards only, so only they leave it unsearched.
            uncovered=uncovered
            if any(
                criteria[k] is not None for k in ("entity_id", "card_type", "heading")
            )
            else [],
        )
        if config_suppressed_note is not None and matches and include_config:
            warnings.insert(0, config_suppressed_note)

        result: dict[str, Any] = {"success": True, "action": "search"}
        if scoped_url_path is not None:
            result["url_path"] = scoped_url_path
            result["config_hash"] = compute_config_hash(docs[0]["config"])
        result.update(
            {
                "search_criteria": criteria,
                "matches": matches,
                "match_count": len(matches),
                "truncated": truncated,
                "hint": (
                    "Use python_path with ha_config_set_dashboard(python_transform=...)"
                    " and the dashboard's config_hash for targeted updates."
                    if matches
                    else "No card matches. Try other criteria, or fetch the full "
                    "config (no search params) to inspect the dashboard."
                ),
            }
        )
        if warnings:
            result["warnings"] = warnings
        return result

    async def _get_dashboard_search_mode(
        self,
        url_path: str | None,
        *,
        resolved_url_path: list[str | None],
        criteria: dict[str, str | None],
        include_config: bool,
        include_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> dict[str, Any]:
        """Search cards, badges and header cards in one dashboard or all of them.

        With ``url_path`` the dashboard is read directly (YAML included, with
        bodies withheld). Without it every storage dashboard is searched: the
        component serves all their configs in one frame, and a component-less
        install reads each storage dashboard listed by HA.
        """
        query = criteria["query"]
        if query is not None and not query.strip():
            criteria["query"] = query = None
        if all(value is None for value in criteria.values()):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Search needs query, entity_id, card_type or heading",
                    suggestions=[
                        "Pass query='light.kitchen' to find cards mentioning it",
                        "Use list_only=True to list dashboards, or url_path=... to get one",
                    ],
                    context={"action": "search"},
                )
            )

        if url_path is None:
            docs, failed = await self._search_docs()
            search_result = self._build_search_result(
                docs, criteria=criteria, include_config=include_config
            )
            if failed:
                search_result.setdefault("warnings", []).append(
                    f"{failed} storage dashboard(s) could not be read and were "
                    "not searched."
                )
        else:
            (
                config,
                url_path,
                search_resolved_from,
            ) = await self._fetch_search_dashboard_config(url_path)
            # Surface the canonicalized url_path to the caller's scope now, so
            # an unexpected exception below still reports the resolved
            # identifier (see ha_config_get_dashboard's except block).
            resolved_url_path[0] = url_path
            # Fail-closed storage guard: bodies are surfaced ONLY for a
            # dashboard PROVABLY tagged mode="storage". A YAML or unconfirmed
            # (untagged, default) dashboard may carry resolved !secret
            # plaintext. Only paid when the caller asked for bodies.
            config_suppressed_note: str | None = None
            if include_config and not await self._dashboard_is_storage_mode(url_path):
                config_suppressed_note = (
                    "Matched-card config bodies were withheld: dashboard "
                    f"{url_path!r} is not provably storage-mode (a YAML "
                    "dashboard's config can carry resolved !secret values). Match "
                    "locations are reported; the config was not surfaced."
                )
            search_result = self._build_search_result(
                [{"url_path": url_path, "config": config}],
                criteria=criteria,
                include_config=include_config,
                scoped_url_path=url_path,
                config_suppressed_note=config_suppressed_note,
            )
            if search_resolved_from is not None:
                search_result["resolved_from"] = search_resolved_from
        _note_screenshot_ignored(
            search_result,
            include_screenshot=include_screenshot,
            options=screenshot_options,
            mode="search",
        )
        return search_result

    async def _search_docs(self) -> tuple[list[dict[str, Any]], int]:
        """Every storage dashboard's ``{url_path, config}`` and the unreadable count.

        One component frame when it advertises ``dashboards_docs``; otherwise one
        read per storage dashboard HA lists (the default dashboard has no list
        row there, so it is searched only through the component).
        """
        caps = await get_component_caps(self._client)
        if component_supports(caps, "dashboards_docs"):
            result = await _dashboards_via_component(self._client, "docs")
            if result is not None and isinstance(result.get("docs"), list):
                return result["docs"], int(result.get("load_failed", 0) or 0)
        return await self._collect_legacy_search_docs()

    async def _collect_legacy_search_docs(self) -> tuple[list[dict[str, Any]], int]:
        """Storage-dashboard ``{url_path, config}`` docs and the unreadable count.

        One ``lovelace/config`` read per STORAGE dashboard from
        ``fetch_dashboards_list``. A body is read ONLY when its row is EXPLICITLY
        tagged ``mode == "storage"`` — fail-closed. HA resolves ``!secret`` when it
        loads a YAML Lovelace config, so reading a YAML (or unknown-mode) body could
        leak resolved secrets into a match. Core's own schemas stamp ``mode`` on
        both kinds of row (storage items default it; YAML entries require it), so
        the legacy walk searches storage dashboards normally; the fail-closed check
        additionally skips the rare UNTAGGED row (a storage item persisted before
        core's mode default existed) rather than read a body it can't prove is
        storage. A per-dashboard read failure is skipped (fail-soft, mirroring the
        component's per-dashboard skip) so one broken dashboard doesn't fail the
        whole search.
        """
        rows = await fetch_dashboards_list(self._client) or []
        docs: list[dict[str, Any]] = []
        failed = 0
        for row in rows:
            url_path = row.get("url_path")
            if not url_path:
                continue
            if row.get("mode") != _DASHBOARD_STORAGE_MODE:
                # Fail-closed: only an explicit storage tag is safe to read. A YAML
                # body may carry resolved !secret plaintext, and an untagged row
                # (every row on a component-less install) is not provably storage.
                continue
            config = await self._fetch_dashboard_config_fail_soft(url_path)
            if config is None:
                failed += 1
                continue
            docs.append({"url_path": url_path, "config": config})
        return docs, failed

    async def _dashboard_is_storage_mode(self, url_path: str | None) -> bool:
        """True only when ``url_path`` is a dashboard PROVABLY tagged mode="storage".

        Fail-closed, mirroring the cross-dashboard (MODE 4) walk's storage check:
        the dashboards-list row for ``url_path`` must carry an explicit
        ``mode == "storage"``. A YAML row, an untagged row, or a dashboard absent
        from the list (the default dashboard is never listed) all return ``False``
        — its config body may carry HA-resolved ``!secret`` plaintext and must not
        be surfaced. A malformed/unavailable list also fails closed.
        """
        rows = await fetch_dashboards_list(self._client) or []
        for row in rows:
            if row.get("url_path") == url_path:
                return row.get("mode") == _DASHBOARD_STORAGE_MODE
        return False

    async def _fetch_dashboard_config_fail_soft(
        self, url_path: str
    ) -> dict[str, Any] | None:
        """One dashboard's config, or ``None`` when it is unreadable (fail-soft).

        A ``ToolError`` (dashboard missing / config invalid) is swallowed so the
        cross-dashboard walk skips that dashboard; a transport error propagates to
        the tool's outer handler.
        """
        try:
            config, _config_hash = await _get_dashboard_config_internal(
                self._client, url_path
            )
        except ToolError as exc:
            logger.debug(
                "Cross-dashboard search skipping unreadable dashboard %r: %r",
                url_path,
                exc,
            )
            return None
        return config

    async def _get_dashboard_get_mode(
        self,
        url_path: str | None,
        *,
        resolved_url_path: list[str | None],
        force_reload: bool,
        view_path: str | None,
        include_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> "dict[str, Any] | ToolResult":
        """Get mode: return the full Lovelace config for a single dashboard.

        The component ``get`` serves this from one in-process, freshness-safe read
        (the default dashboard included — the tool's ``"default"``/omitted alias
        maps to the component's ``None`` url_path); ``None`` falls back to the
        unchanged legacy ``lovelace/config`` read, which also covers YAML bodies
        (never served in-process) and internal-id lazy-resolve.

        ``force_reload`` bypasses the component fast path entirely: the component
        ``get`` carries no force semantic, so a forced read must go straight to the
        legacy ``lovelace/config`` request below (which threads ``force=True``) to
        actually bust HA's Lovelace cache.
        """
        component_url_path = (
            None if (not url_path or url_path == "default") else url_path
        )
        component_config = (
            None
            if force_reload
            else await _component_dashboard_config(self._client, component_url_path)
        )
        if component_config is not None:
            # The component matches an exact url_path (or the default), so no
            # lazy-resolve is possible: original == final, resolved_from unset.
            resolved_url_path[0] = url_path
            return await self._finalize_get_result(
                url_path,
                component_config,
                original_url_path=url_path,
                view_path=view_path,
                include_screenshot=include_screenshot,
                screenshot_options=screenshot_options,
            )

        data: dict[str, Any] = {"type": "lovelace/config", "force": force_reload}
        # Handle "default" as special value for default dashboard
        if url_path and url_path != "default":
            data["url_path"] = url_path

        response = await self._client.send_websocket_message(data)

        # Lazy resolver fallback: if HA rejects the identifier as unknown,
        # resolve it via lovelace/dashboards/list and retry once. The
        # round-trip is only paid when the caller passed an internal
        # dashboard id (or another non-url_path form) HA does not accept.
        original_url_path = url_path
        url_path, response = await _lazy_resolve_and_retry(
            self._client, url_path, data, response
        )
        # Surface the canonicalized url_path to the caller's scope now, so
        # an unexpected exception from the config processing below still
        # reports the resolved identifier (see ha_config_get_dashboard's
        # except block).
        resolved_url_path[0] = url_path

        # Check if request failed (after potential retry)
        if isinstance(response, dict) and not response.get("success", True):
            error_msg = response.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(error_msg),
                    suggestions=[
                        "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                        "Check if you have permission to access this dashboard",
                        "Use url_path='default' for default dashboard",
                    ],
                    context={"action": "get", "url_path": url_path},
                )
            )

        # Extract config from WebSocket response
        config = response.get("result") if isinstance(response, dict) else response

        return await self._finalize_get_result(
            url_path,
            config,
            original_url_path=original_url_path,
            view_path=view_path,
            include_screenshot=include_screenshot,
            screenshot_options=screenshot_options,
        )

    async def _finalize_get_result(
        self,
        url_path: str | None,
        config: Any,
        *,
        original_url_path: str | None,
        view_path: str | None,
        include_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> "dict[str, Any] | ToolResult":
        """Shape + return the get-mode response for an already-fetched config.

        Shared by the component fast path and the legacy path so both produce a
        byte-identical envelope: ``config_hash`` (optimistic locking), size,
        render paths, ``resolved_from`` (when the legacy lazy resolver
        canonicalised an internal id), the large-config disclosure hint, and an
        optional screenshot.

        ``view_path`` scopes the payload to one view: the response carries
        ``view`` + ``view_index`` instead of ``config``. The scoped envelope
        deliberately has NO ``config`` key so the view object cannot be
        mistaken for a full config and pushed back through
        ``ha_config_set_dashboard(config=...)``, which would drop every other
        view; ``config_hash``/``config_size_bytes`` still describe the full
        config so ``python_transform`` optimistic locking works unchanged.
        """
        # Compute hash for optimistic locking in subsequent operations
        config_hash = compute_config_hash(config) if isinstance(config, dict) else None

        # Calculate config size for progressive disclosure hint
        config_size = len(json.dumps(config)) if isinstance(config, dict) else 0

        if view_path is not None:
            get_result = self._scoped_view_get_result(
                url_path,
                config,
                view_path=view_path,
                config_hash=config_hash,
                config_size=config_size,
            )
        else:
            get_result = {
                "success": True,
                "action": "get",
                "url_path": url_path,
                "config": config,
                "config_hash": config_hash,
                "config_size_bytes": config_size,
            }
            _attach_dashboard_render_paths(
                get_result,
                url_path,
                config if isinstance(config, dict) else None,
            )
            # Add hint for large configs (progressive disclosure) - 10KB ≈ 2-3k tokens
            if config_size >= 10000:
                get_result["hint"] = (
                    f"Large config ({config_size:,} bytes). Pass view_path=... "
                    "to fetch a single view, or use "
                    "ha_config_get_dashboard(entity_id=...) to find card positions, "
                    "then ha_config_set_dashboard(python_transform=...) "
                    "instead of full config replacement."
                )
        # Surface the original caller-passed identifier when the lazy
        # resolver canonicalised it (parity with delete_dashboard's
        # resolved_id field). Caller can use this to detect that their
        # input was an internal id rather than a url_path.
        if original_url_path is not None and original_url_path != url_path:
            get_result["resolved_from"] = original_url_path

        return await _maybe_attach_screenshot(
            get_result,
            url_path,
            include_screenshot,
            client=self._client,
            config=config if isinstance(config, dict) else None,
            # view_path doubles as the get-mode scoping parameter now, so a
            # scoped read without a screenshot request must not trip the
            # "screenshot render options are ignored" warning.
            options=(
                screenshot_options
                if include_screenshot
                else replace(screenshot_options, view_path=None)
            ),
            raise_on_failure=True,
        )

    def _scoped_view_get_result(
        self,
        url_path: str | None,
        config: Any,
        *,
        view_path: str,
        config_hash: str | None,
        config_size: int,
    ) -> dict[str, Any]:
        """Build the view-scoped get-mode envelope (issue #2010).

        Raises the shared matcher's structured errors for an empty, unknown,
        or ambiguous ``view_path`` and for strategy dashboards (no static
        views). Pure config walking — works whether or not the dashboard
        screenshot beta feature is enabled.
        """
        if not isinstance(config, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "Dashboard config is unavailable, so view_path cannot be resolved.",
                    context={"url_path": url_path, "view_path": view_path},
                    suggestions=["Retry without view_path to inspect the raw response"],
                )
            )
        view_index, view = match_dashboard_view(
            url_path or "default",
            config,
            view_path,
            strategy_suggestions=(
                "Omit view_path — strategy dashboards generate their views at "
                "runtime, so there is no static view config to return",
            ),
        )
        views = config.get("views")
        view_count = len(views) if isinstance(views, list) else 0
        get_result: dict[str, Any] = {
            "success": True,
            "action": "get",
            "url_path": url_path,
            "view_path": view_path,
            "view_index": view_index,
            "view_count": view_count,
            "view": view,
            "config_hash": config_hash,
            "config_size_bytes": config_size,
            "view_size_bytes": len(json.dumps(view)),
            "hint": (
                f"Scoped to view '{view_path}' — config['views'][{view_index}] "
                f"of {view_count} views. config_hash and config_size_bytes "
                "cover the FULL dashboard config, so "
                "ha_config_set_dashboard(python_transform=...) edits addressed "
                f"via config['views'][{view_index}] validate as-is. Do NOT "
                "pass this view object as the config parameter — that would "
                "replace the entire dashboard. Omit view_path for the full "
                "config."
            ),
        }
        _attach_dashboard_render_paths(get_result, url_path, config)
        render_paths = get_result.get("render_paths")
        if isinstance(render_paths, list):
            get_result["render_paths"] = [
                row
                for row in render_paths
                if isinstance(row, dict) and row.get("view_index") == view_index
            ]
        return get_result

    @tool(
        name="ha_config_set_dashboard",
        tags={"Dashboards"},
        annotations=write_hints(
            "Create or Update Dashboard",
            destructive=True,
            idempotent=False,
            open_world=True,
        ),
    )
    @with_auto_backup(domain="dashboard", id_param="url_path")
    @log_tool_usage
    async def ha_config_set_dashboard(
        self,
        url_path: Annotated[
            str,
            Field(
                description="Dashboard URL path (e.g., 'my-dashboard'). "
                "Use 'default' or 'lovelace' for the default dashboard. "
                "New dashboards must use a hyphenated path."
            ),
        ],
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Dashboard configuration with views and cards. "
                "Omit or set to None to create dashboard without initial config. "
                "Mutually exclusive with python_transform and patch."
            ),
        ] = None,
        python_transform: Annotated[
            str | None,
            Field(
                description="Python expression to transform existing dashboard config. "
                "Mutually exclusive with config and patch. "
                "Requires config_hash for validation. "
                "See PYTHON TRANSFORM SECURITY below for allowed operations. "
                "Examples: "
                "Simple: python_transform=\"config['views'][0]['cards'][0]['icon'] = 'mdi:lamp'\" "
                "Pattern: python_transform=\"for card in config['views'][0]['cards']: if 'light' in card.get('entity', ''): card['icon'] = 'mdi:lightbulb'\" "
                "Multi-op: python_transform=\"config['views'][0]['cards'][0]['icon'] = 'mdi:lamp'; del config['views'][0]['cards'][2]\" "
                "\n\n" + get_security_documentation(),
            ),
        ] = None,
        config_hash: Annotated[
            str | None,
            Field(
                description="Config hash from ha_config_get_dashboard for optimistic locking. "
                "REQUIRED for python_transform and patch (validates dashboard unchanged). "
                "Optional for config (validates before full replacement if provided)."
            ),
        ] = None,
        title: Annotated[
            str | None,
            Field(description="Dashboard display name shown in sidebar"),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="MDI icon name (e.g., 'mdi:home', 'mdi:cellphone'). "
                "Defaults to 'mdi:view-dashboard'"
            ),
        ] = None,
        require_admin: Annotated[
            bool | None,
            Field(
                description="Restrict dashboard to admin users only. "
                "For existing dashboards, only updated when explicitly provided."
            ),
        ] = None,
        show_in_sidebar: Annotated[
            bool | None,
            Field(
                description="Show dashboard in sidebar navigation. "
                "For existing dashboards, only updated when explicitly provided."
            ),
        ] = None,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
        # BestPracticeKey (#1779): consumed by StrictBpsMiddleware, never read
        # here — see strict_bps.py for the declaration contract.
        BestPracticeKey: BestPracticeKeyParam = None,
        return_screenshot: Annotated[
            bool,
            Field(
                description="After writing, also return rendered image(s) of the dashboard. "
                "Requires the 'dashboard screenshot' beta feature + engine app "
                "(add-on)/sidecar; if unavailable, the write result is returned with a "
                "warning."
            ),
        ] = False,
        view_path: Annotated[
            str | None,
            Field(
                description="With return_screenshot: stable Lovelace "
                "views[].path to render."
            ),
        ] = None,
        patch: Annotated[
            list[dict[str, Any]] | None,
            JSON_STRING_COERCION,
            Field(
                description="Structured dashboard edits: up to 100 JSON Patch "
                "add, remove, replace or test operations using RFC 6901 paths. "
                "Use /- to append to an array; escape ~ as ~0 and / as ~1 in keys. "
                "Mutually exclusive with config and python_transform. "
                "Strings in value are preserved literally."
            ),
        ] = None,
    ) -> "dict[str, Any] | ToolResult":
        """Create or update a Home Assistant dashboard.

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.
        `dashboard-guide.md` ships under `skill_content` by default. A card's
        fields: ha_config_get_dashboard(card_type=..., describe=True).

        MODES (pick one):
        - patch: edit known paths with literal values using JSON Patch
          add/remove/replace/test and config_hash, e.g.
          patch=[{"op": "replace", "path": "/views/0/title", "value": "Home"}].
          move/copy are unsupported. Full guide:
          https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/dashboard-edits.md
        - python_transform: loops or pattern-based changes across cards and views,
          e.g. 'config["views"][0]["cards"].append({"type": "button", "entity": "light.bedroom"})'.
          After delete/add operations indices shift, so chain multiple ops in ONE
          expression where possible; a later call needs a fresh config_hash from
          ha_config_get_dashboard().
        - config: new dashboards only, or a full restructure. Replaces everything.
          Omit it to create a dashboard without initial config.

        Use ha_config_get_dashboard(entity_id=...) to get the path of any card,
        and ha_search / ha_get_overview to find entity IDs — never guess them.
        For visual re-checks after the write, use ha_get_dashboard_screenshot
        (when available) instead of re-sending config.

        title/icon/require_admin/show_in_sidebar can be updated in a
        metadata-only call or alongside a full config replacement; with
        python_transform or patch, update metadata in a separate call (combining
        it with patch is rejected). Strategy dashboards (config
        {"strategy": {...}}) cannot be converted to custom dashboards here; use
        "Take Control" in the Home Assistant UI.

        EXAMPLE:
        ha_config_set_dashboard(url_path="home-dashboard", title="Home Overview",
            config={"views": [{"title": "Home", "type": "sections", "sections": [{"title": "Climate", "cards": [{"type": "tile", "entity": "climate.living_room"}]}]}]})

        This tool manages storage-mode dashboards only. YAML-mode dashboards (a
        .yaml file registered under ``lovelace: dashboards:`` in
        configuration.yaml) and dashboards inlined under ``lovelace:`` are
        edited in their .yaml file; ``ha_config_set_yaml`` can update the
        ``lovelace:`` registration but not the dashboard body.
        """
        action = (
            "patch"
            if patch is not None
            else ("python_transform" if python_transform is not None else "set")
        )
        screenshot_options = _DashboardScreenshotOptions(view_path=view_path)
        try:
            # Reject an invalid view_path BEFORE committing the write. On the
            # return_screenshot path a screenshot failure is demoted to a
            # warning after the write commits, so without this a blank view_path
            # would be swallowed as a warning on an already-committed write.
            if return_screenshot and view_path is not None and not view_path.strip():
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "view_path cannot be empty.",
                        context={"action": action, "view_path": view_path},
                    )
                )
            (
                url_path,
                pre_resolved_from,
                pre_fetched_dashboards,
            ) = await self._resolve_set_dashboard_url_path(url_path, action=action)

            if (
                sum(value is not None for value in (config, python_transform, patch))
                > 1
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "config, python_transform and patch are mutually exclusive "
                        "and cannot be used simultaneously",
                        suggestions=[
                            "Use only ONE of: config, python_transform or patch",
                            "config: Full replacement",
                            "python_transform: Loops and pattern-based edits",
                        ],
                        context={"action": action, "url_path": url_path},
                    )
                )

            if patch is not None and any(
                value is not None
                for value in (title, icon, require_admin, show_in_sidebar)
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "patch cannot be combined with dashboard metadata "
                        "(title, icon, require_admin, show_in_sidebar)",
                        suggestions=[
                            "Update metadata in a separate ha_config_set_dashboard call "
                            "without patch, config or python_transform",
                        ],
                        context={
                            "action": action,
                            "url_path": url_path,
                            "write_committed": False,
                        },
                    )
                )

            if patch is not None:
                return await self._run_dashboard_patch(
                    url_path,
                    config_hash,
                    patch,
                    pre_resolved_from,
                    MandatoryBPS,
                    return_screenshot=return_screenshot,
                    screenshot_options=screenshot_options,
                )

            if python_transform is not None:
                return await self._run_dashboard_python_transform(
                    url_path,
                    config_hash,
                    python_transform,
                    pre_resolved_from,
                    MandatoryBPS,
                    return_screenshot=return_screenshot,
                    screenshot_options=screenshot_options,
                )

            return await self._run_dashboard_config_update(
                url_path,
                config,
                config_hash,
                title=title,
                icon=icon,
                require_admin=require_admin,
                show_in_sidebar=show_in_sidebar,
                pre_resolved_from=pre_resolved_from,
                pre_fetched_dashboards=pre_fetched_dashboards,
                return_screenshot=return_screenshot,
                screenshot_options=screenshot_options,
                MandatoryBPS=MandatoryBPS,
            )

        except ToolError as te:
            raise augment_tool_error_with_skill_content(te, bp_warnings=None) from None
        except Exception as e:  # noqa: BLE001
            error = exception_to_structured_error(
                e,
                context={"action": action, "url_path": url_path},
                suggestions=[
                    "Ensure url_path is unique (not already in use for different dashboard type)",
                    "New dashboards require a hyphenated url_path",
                    "Check that you have admin permissions",
                    "Verify config format is valid Lovelace JSON",
                ],
                raise_error=False,
            )
            augment_error_dict_with_skill_content(error, bp_warnings=None)
            raise_tool_error(error)
            return None

    async def _resolve_set_dashboard_url_path(
        self, url_path: str, *, action: str = "set"
    ) -> tuple[str, str | None, list[dict[str, Any]] | None]:
        """Validate the set target and canonicalize supported dashboard identifiers.

        Hyphenless input is allowed only when it exactly matches an existing
        dashboard's ``url_path`` (plus the built-in ``lovelace`` alias case).
        New dashboard paths must contain a hyphen. An internal-ID-only match may
        still canonicalize to a different, hyphenated existing ``url_path``.

        Returns ``(url_path, pre_resolved_from, pre_fetched_dashboards)``:
        ``url_path`` is canonicalized (``"default"`` -> ``"lovelace"``, and an
        internal-id form pre-resolved to its url_path when it matches an
        existing dashboard). ``pre_resolved_from`` is the original
        caller-passed identifier when the pre-resolver rewrote it, else
        ``None``. ``pre_fetched_dashboards`` is the ``lovelace/dashboards/list``
        response already fetched by the pre-resolver when it fired, so the
        caller can reuse it instead of paying a second round-trip.
        """
        # ``url_path`` is required (always non-None). Reject empty/
        # whitespace up-front so the caller gets a structured parameter
        # error instead of a misleading downstream failure (the
        # subsequent "default" alias, pre-resolver, and hyphen check
        # all assume a usable string). Extension of the #1312
        # validate_identifier_not_empty pattern to the dashboards
        # family per #1313.
        validate_identifier_not_empty(
            url_path,
            "url_path",
            suggestions=[
                "Pass a dashboard URL path (e.g. 'my-dashboard')",
                "Use 'default' or 'lovelace' for the default dashboard",
            ],
            context={"action": action},
        )
        # Handle "default" as alias for the default dashboard
        # (matches ha_config_get_dashboard behavior)
        if url_path == "default":
            url_path = "lovelace"

        # Pre-resolve a hyphenless dashboard identifier before enforcing the
        # exact-url-path exception below. Internal-ID support is preserved when
        # it canonicalizes to a hyphenated existing path, but an ID-only match
        # cannot exempt a different hyphenless path from validation.
        #
        # This is still a create-or-update API: a caller intending to create a
        # dashboard can target an existing one, either by exact url_path or by a
        # supported internal-ID rewrite. ``action`` reports create vs update;
        # ``resolved_from`` additionally reports only the latter rewrite.
        pre_resolved_from: str | None = None
        exact_url_path_exists = False
        # When the pre-resolver fires and finds a match, ``_resolve_dashboard``
        # has already fetched ``lovelace/dashboards/list``. Capture that list
        # so the existence-check site below can reuse it instead of paying
        # a second round-trip.
        pre_fetched_dashboards: list[dict[str, Any]] | None = None
        if "-" not in url_path and url_path != "lovelace":
            resolved, dashboards = await _resolve_dashboard(self._client, url_path)
            if dashboards is None:
                _raise_dashboard_registry_read_error(action=action, url_path=url_path)
            if resolved is not None and resolved["url_path"]:
                exact_url_path_exists = resolved["url_path"] == url_path
                pre_fetched_dashboards = dashboards
                if not exact_url_path_exists and "-" in resolved["url_path"]:
                    original_url_path = url_path
                    url_path = resolved["url_path"]
                    pre_resolved_from = original_url_path
                    logger.info(
                        "ha_config_set_dashboard pre-resolver mapped %r -> %r",
                        original_url_path,
                        url_path,
                    )

        # Existing exact url_path values may predate HA's creation rule and must
        # remain valid update targets unchanged.
        # The built-in "lovelace" dashboard is exempt since it already exists
        if not exact_url_path_exists and "-" not in url_path and url_path != "lovelace":
            suggestions = [
                "Use format like 'my-dashboard' or 'mobile-view'",
                "Use 'lovelace' or 'default' to edit the default dashboard",
            ]
            hyphenated_url_path = url_path.replace("_", "-")
            if hyphenated_url_path != url_path:
                suggestions.insert(0, f"Try '{hyphenated_url_path}' instead")
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "url_path must contain a hyphen (-)",
                    suggestions=suggestions,
                    context={"action": action, "url_path": url_path},
                )
            )

        return url_path, pre_resolved_from, pre_fetched_dashboards

    async def _fetch_and_verify_dashboard_hash(
        self, url_path: str, config_hash: str, *, action: str = "python_transform"
    ) -> dict[str, Any]:
        """Fetch current dashboard config and verify ``config_hash`` (optimistic locking).

        Re-wraps the shared fetch helper's generic error with
        edit-specific UX suggestions, and raises on a hash
        mismatch (concurrent edit since the caller's last read).
        """
        try:
            current_config, current_hash = await _get_dashboard_config_internal(
                self._client, url_path
            )
        except ToolError as e:
            raise_dashboard_edit_fetch_error(e, url_path, action)

        if current_hash != config_hash:
            raise_dashboard_edit_error(
                url_path,
                "conflict",
                "Dashboard modified since last read (conflict)",
                False,
                action,
            )
        return current_config

    @staticmethod
    def _apply_dashboard_python_transform(
        url_path: str, python_transform: str, current_config: dict[str, Any]
    ) -> dict[str, Any]:
        """Run ``python_transform`` against ``current_config`` in the sandbox."""
        was_strategy_dashboard = "strategy" in current_config
        try:
            transformed_config = safe_execute(python_transform, current_config)
        except PythonSandboxError as e:
            message, suggestions = format_sandbox_error(e, python_transform)
            # A path-shape mismatch (IndexError/KeyError) is almost always
            # a hallucinated path; steer the retry toward search mode so
            # the next transform is built from a verified python_path.
            if isinstance(e, PythonSandboxExecutionError) and isinstance(
                e.__cause__, (IndexError, KeyError)
            ):
                suggestions = [
                    "Call ha_config_get_dashboard with card_type=..., "
                    "entity_id=..., or heading=... to get the verified "
                    "python_path for the target card, then build "
                    "python_transform from that path",
                    *suggestions,
                ]
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    message,
                    suggestions=suggestions,
                    context={"action": "python_transform", "url_path": url_path},
                )
            )
        DashboardConfigTools._validate_strategy_dashboard_replacement(
            url_path,
            was_strategy_dashboard=was_strategy_dashboard,
            replacement_config=transformed_config,
            action="python_transform",
        )
        reject_malformed_dashboard_lists(
            transformed_config, url_path, source="python_transform"
        )
        return transformed_config

    async def _save_dashboard_python_transform(
        self,
        url_path: str,
        transformed_config: dict[str, Any],
        *,
        action: str = "python_transform",
    ) -> tuple[dict[str, Any], str | None, str | None]:
        """Save transformed config and best-effort reload its authoritative form."""
        await self._save_dashboard_config(url_path, transformed_config, action=action)

        # HA may normalize after save, so prefer an authoritative re-fetch. The
        # mutation has already committed at this point: a follow-up read failure
        # must not make the caller believe the write itself failed.
        try:
            post_save_config, new_config_hash = await _get_dashboard_config_internal(
                self._client, url_path
            )
        except ToolError as exc:
            warning = (
                "Dashboard was updated, but its authoritative post-save config "
                f"could not be reloaded: {extract_tool_error_message(exc)}"
            )
            return transformed_config, None, warning
        except Exception as exc:
            logger.warning(
                "Could not reload dashboard %s after %s: %s",
                url_path,
                action,
                exc,
                exc_info=True,
            )
            warning = (
                "Dashboard was updated, but its authoritative post-save config "
                f"could not be reloaded: {exc}"
            )
            return transformed_config, None, warning
        return post_save_config, new_config_hash, None

    async def _run_dashboard_python_transform(
        self,
        url_path: str,
        config_hash: str | None,
        python_transform: str,
        pre_resolved_from: str | None,
        MandatoryBPS: bool,
        *,
        return_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> "dict[str, Any] | ToolResult":
        """Execute python_transform mode and return the tool response."""
        if config_hash is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "config_hash is required for python_transform",
                    suggestions=[
                        "Call ha_config_get_dashboard() first",
                        "Use the config_hash from that response",
                    ],
                    context={"action": "python_transform", "url_path": url_path},
                )
            )

        current_config = await self._fetch_and_verify_dashboard_hash(
            url_path, config_hash
        )
        transformed_config = self._apply_dashboard_python_transform(
            url_path, python_transform, current_config
        )
        native_result = await edit_dashboard_via_component(
            self._client,
            url_path,
            expected_hash=config_hash,
            config=transformed_config,
            action="python_transform",
        )
        if native_result is None:
            native_result = await self._save_dashboard_edit_legacy(
                url_path, transformed_config
            )
        return await self._finish_dashboard_edit(
            url_path,
            native_result,
            "python_transform",
            pre_resolved_from,
            MandatoryBPS,
            python_transform=python_transform,
            return_screenshot=return_screenshot,
            screenshot_options=screenshot_options,
        )

    async def _save_dashboard_edit_legacy(
        self,
        url_path: str,
        config: dict[str, Any],
        *,
        action: str = "python_transform",
    ) -> dict[str, Any]:
        """Adapt the existing save/readback path to the shared result contract."""
        post_config, config_hash, warning = await self._save_dashboard_python_transform(
            url_path, config, action=action
        )
        return {
            "config": post_config,
            "config_hash": config_hash,
            "write_committed": True,
            "post_write_verified": warning is None,
            "warnings": [warning] if warning else [],
        }

    async def _reject_malformed_patched_dashboard(
        self, url_path: str, patch: list[dict[str, Any]]
    ) -> None:
        """Check the patched config when an edit lands inside a card."""
        current, _ = await _get_dashboard_config_internal(self._client, url_path)
        try:
            candidate = apply_dashboard_patch(current, patch)
        except ValueError:
            return  # An unapplicable patch is rejected by the edit itself.
        reject_malformed_dashboard_lists(candidate, url_path, source="patch")

    async def _run_dashboard_patch(
        self,
        url_path: str,
        config_hash: str | None,
        patch: list[dict[str, Any]] | str,
        pre_resolved_from: str | None,
        MandatoryBPS: bool,
        *,
        return_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> "dict[str, Any] | ToolResult":
        """Apply structured edits in Core, or use the existing legacy save path."""
        if config_hash is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "config_hash is required for patch",
                    suggestions=["Read the dashboard first and use its config_hash"],
                    context={"action": "patch", "url_path": url_path},
                )
            )
        try:
            parsed_patch = parse_json_param(patch, "patch")
            if not isinstance(parsed_patch, list):
                raise ValueError("patch must be a list of operations")
        except ValueError as exc:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(exc),
                    context={"action": "patch", "url_path": url_path},
                )
            )
        reject_malformed_dashboard_patch(parsed_patch, url_path)
        if patch_writes_inside_card(parsed_patch):
            await self._reject_malformed_patched_dashboard(url_path, parsed_patch)
        result = await edit_dashboard_via_component(
            self._client,
            url_path,
            expected_hash=config_hash,
            patch=parsed_patch,
            action="patch",
        )
        if result is None:
            current = await self._fetch_and_verify_dashboard_hash(
                url_path, config_hash, action="patch"
            )

            try:
                updated = apply_dashboard_patch(current, parsed_patch)
            except ValueError as exc:
                raise_dashboard_edit_error(
                    url_path, "validation_failed", str(exc), False, "patch"
                )
            self._validate_strategy_dashboard_replacement(
                url_path,
                was_strategy_dashboard="strategy" in current,
                replacement_config=updated,
                action="patch",
            )
            # Compare JSON hashes rather than Python equality: true and 1 are
            # different dashboard values even though Python considers them equal.
            if compute_config_hash(updated) == config_hash:
                result = {
                    "config": current,
                    "config_hash": config_hash,
                    "write_committed": False,
                    "post_write_verified": True,
                    "unchanged": True,
                }
            else:
                result = await self._save_dashboard_edit_legacy(
                    url_path, updated, action="patch"
                )
        return await self._finish_dashboard_edit(
            url_path,
            result,
            "patch",
            pre_resolved_from,
            MandatoryBPS,
            return_screenshot=return_screenshot,
            screenshot_options=screenshot_options,
        )

    async def _finish_dashboard_edit(
        self,
        url_path: str,
        edit: dict[str, Any],
        action: str,
        pre_resolved_from: str | None,
        MandatoryBPS: bool,
        *,
        return_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
        python_transform: str | None = None,
    ) -> "dict[str, Any] | ToolResult":
        """Preserve public edit fields, authoritative paths, BPS and screenshots."""
        result: dict[str, Any] = {
            "success": True,
            "action": action,
            "url_path": url_path,
            "config_hash": edit["config_hash"],
            "write_committed": edit["write_committed"],
            "post_write_verified": edit["post_write_verified"],
            "message": f"Dashboard {url_path} updated via "
            + ("Python transform" if action == "python_transform" else "patch"),
        }
        if python_transform is not None:
            result["python_expression"] = python_transform
        if pre_resolved_from is not None:
            result["resolved_from"] = pre_resolved_from
        if edit.get("warnings"):
            result["warnings"] = list(edit["warnings"])
        if edit.get("unchanged"):
            result["unchanged"] = True
            result["message"] = f"Dashboard {url_path} unchanged"
        if edit["post_write_verified"]:
            _attach_dashboard_render_paths(result, url_path, edit["config"])
        await _attach_dashboard_skill(result, MandatoryBPS, self._client)
        return await _maybe_attach_screenshot(
            result,
            url_path,
            return_screenshot,
            client=self._client,
            config=edit["config"],
            options=screenshot_options,
        )

    async def _lookup_existing_dashboards(
        self, url_path: str, pre_fetched_dashboards: list[dict[str, Any]] | None
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Resolve whether ``url_path`` already exists, reusing a pre-fetched list when available."""
        if pre_fetched_dashboards is not None:
            existing_dashboards = pre_fetched_dashboards
        else:
            fetched_dashboards = await fetch_dashboards_list(self._client)
            if fetched_dashboards is None:
                _raise_dashboard_registry_read_error(action="set", url_path=url_path)
            existing_dashboards = fetched_dashboards
        dashboard_exists = any(
            d.get("url_path") == url_path for d in existing_dashboards
        )
        # The built-in default dashboard ("lovelace") is always present
        # but isn't listed by lovelace/dashboards/list on fresh installs
        if url_path == "lovelace":
            dashboard_exists = True
        return dashboard_exists, existing_dashboards

    async def _create_dashboard(
        self,
        url_path: str,
        *,
        title: str | None,
        icon: str | None,
        require_admin: bool | None,
        show_in_sidebar: bool | None,
    ) -> str | None:
        """Create a new storage-mode dashboard and return its dashboard_id."""
        dashboard_title = title or url_path.replace("-", " ").title()
        create_data: dict[str, Any] = {
            "type": "lovelace/dashboards/create",
            "url_path": url_path,
            "title": dashboard_title,
            "require_admin": require_admin if require_admin is not None else False,
            "show_in_sidebar": show_in_sidebar if show_in_sidebar is not None else True,
        }
        if icon:
            create_data["icon"] = icon
        create_result = await self._client.send_websocket_message(create_data)

        if isinstance(create_result, dict) and not create_result.get("success", True):
            error_msg = create_result.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(error_msg),
                    context={"action": "create", "url_path": url_path},
                )
            )

        if isinstance(create_result, dict) and "result" in create_result:
            dashboard_info = create_result["result"]
            return cast(str | None, dashboard_info.get("id"))
        if isinstance(create_result, dict):
            return cast(str | None, create_result.get("id"))
        return None

    async def _send_dashboard_metadata_update(
        self, dashboard_id: str, metadata_update_fields: dict[str, Any], url_path: str
    ) -> None:
        """Send the ``lovelace/dashboards/update`` WS call for metadata-only changes."""
        meta_update: dict[str, Any] = {
            "type": "lovelace/dashboards/update",
            "dashboard_id": dashboard_id,
            **metadata_update_fields,
        }
        meta_result = await self._client.send_websocket_message(meta_update)
        if isinstance(meta_result, dict) and not meta_result.get("success", True):
            error_msg = meta_result.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.SERVICE_CALL_FAILED,
                    message=f"Failed to update dashboard metadata: {error_msg}",
                    suggestions=[
                        "Check that you have admin permissions",
                        "Verify dashboard is in storage mode (not YAML mode)",
                    ],
                    context={"action": "update", "url_path": url_path},
                )
            )

    async def _update_dashboard_metadata(
        self,
        url_path: str,
        existing_dashboards: list[dict[str, Any]],
        *,
        title: str | None,
        icon: str | None,
        require_admin: bool | None,
        show_in_sidebar: bool | None,
    ) -> tuple[str | None, bool, str | None]:
        """Update metadata for an existing dashboard if any metadata params were provided.

        Returns ``(dashboard_id, metadata_updated, warning)``.
        """
        dashboard_id = None
        for dashboard in existing_dashboards:
            if dashboard.get("url_path") == url_path:
                dashboard_id = dashboard.get("id")
                break

        metadata_update_fields: dict[str, Any] = {
            k: v
            for k, v in {
                "title": title,
                "icon": icon,
                "require_admin": require_admin,
                "show_in_sidebar": show_in_sidebar,
            }.items()
            if v is not None
        }
        if metadata_update_fields and dashboard_id is not None:
            await self._send_dashboard_metadata_update(
                dashboard_id, metadata_update_fields, url_path
            )
            return dashboard_id, True, None
        if metadata_update_fields and dashboard_id is None:
            # Dashboard ID not found in storage list (e.g. default lovelace on
            # fresh installs). Metadata update via lovelace/dashboards/update
            # is not possible without a storage ID. The caller either proceeds
            # with a requested config write or rejects the resulting no-op.
            warning = (
                "Metadata fields were provided but could not be applied: "
                "dashboard has no storage ID (likely the built-in default dashboard)."
            )
            return dashboard_id, False, warning
        return dashboard_id, False, None

    async def _ensure_dashboard_exists(
        self,
        url_path: str,
        *,
        title: str | None,
        icon: str | None,
        require_admin: bool | None,
        show_in_sidebar: bool | None,
        pre_fetched_dashboards: list[dict[str, Any]] | None,
    ) -> tuple[bool, str | None, bool, str | None]:
        """Create the dashboard if missing, else update its metadata if requested.

        Returns ``(dashboard_exists, dashboard_id, metadata_updated, warning)`` —
        ``dashboard_exists`` reflects state *before* this call, so the caller
        can distinguish create vs update for the response's
        ``action``/``dashboard_created`` fields.
        """
        dashboard_exists, existing_dashboards = await self._lookup_existing_dashboards(
            url_path, pre_fetched_dashboards
        )
        if not dashboard_exists:
            dashboard_id = await self._create_dashboard(
                url_path,
                title=title,
                icon=icon,
                require_admin=require_admin,
                show_in_sidebar=show_in_sidebar,
            )
            return dashboard_exists, dashboard_id, False, None

        dashboard_id, metadata_updated, warning = await self._update_dashboard_metadata(
            url_path,
            existing_dashboards,
            title=title,
            icon=icon,
            require_admin=require_admin,
            show_in_sidebar=show_in_sidebar,
        )
        return dashboard_exists, dashboard_id, metadata_updated, warning

    async def _check_dashboard_replace_hash(
        self,
        url_path: str,
        config_hash: str | None,
        replacement_config: dict[str, Any],
    ) -> str | None:
        """Validate replacement safety/hash and warn on large full-config replacement.

        Omitting ``config_hash`` remains the force-replace path, but the current
        config must still be readable so strategy-dashboard protection can be
        evaluated before any replacement write. The one exception is an existing
        storage dashboard with no config yet and no supplied ``config_hash``:
        there is no strategy state to protect, so its first full config may be
        written. A supplied hash still conflicts with the now-absent config.
        """
        try:
            existing_config, existing_hash = await _get_dashboard_config_internal(
                self._client, url_path
            )
        except ToolError as exc:
            if _is_no_stored_dashboard_config_error(exc):
                if config_hash is None:
                    return None
                self._raise_dashboard_hash_conflict(
                    url_path, message="Dashboard has no saved config"
                )
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Cannot verify the current dashboard config before full replacement",
                    details=extract_tool_error_message(exc),
                    suggestions=[
                        "Retry the operation",
                        "Read the dashboard with ha_config_get_dashboard first",
                    ],
                    context={
                        "action": "set",
                        "url_path": url_path,
                        "reason": "load_failed",
                        "write_committed": False,
                        "post_write_verified": False,
                    },
                )
            )

        existing_config_size = len(json.dumps(existing_config))
        if config_hash is not None and existing_hash != config_hash:
            self._raise_dashboard_hash_conflict(url_path)

        self._validate_strategy_dashboard_replacement(
            url_path,
            was_strategy_dashboard="strategy" in existing_config,
            replacement_config=replacement_config,
            action="set",
        )

        return _large_dashboard_replacement_warning(existing_config_size)

    @staticmethod
    def _raise_dashboard_hash_conflict(
        url_path: str, *, message: str = "Dashboard modified since last read (conflict)"
    ) -> NoReturn:
        """Raise the shared optimistic-lock conflict for a full replacement."""
        raise_dashboard_edit_error(
            url_path,
            "conflict",
            message,
            False,
            "set",
            hash_supplied=True,
        )

    @staticmethod
    def _validate_strategy_dashboard_replacement(
        url_path: str,
        *,
        was_strategy_dashboard: bool,
        replacement_config: dict[str, Any],
        action: str,
    ) -> None:
        """Prevent this tool from taking control of a strategy dashboard."""
        if not was_strategy_dashboard or "strategy" in replacement_config:
            return
        raise_dashboard_edit_error(
            url_path,
            "strategy_conversion",
            "Strategy dashboards cannot be converted to custom dashboards via this tool",
            False,
            action,
        )

    async def _save_dashboard_config(
        self,
        url_path: str,
        config_dict: dict[str, Any],
        *,
        action: str = "set",
    ) -> None:
        """Save through the legacy API with a consistent outcome for every edit mode."""
        config_save_data: dict[str, Any] = {
            "type": "lovelace/config/save",
            "config": config_dict,
        }
        if url_path:
            config_save_data["url_path"] = url_path
        try:
            save_result = await self._client.send_websocket_message(config_save_data)
        except ToolError:
            raise
        except asyncio.CancelledError:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Dashboard write outcome unknown: save request was cancelled",
                    suggestions=[
                        "Read the dashboard before retrying to check whether the save applied"
                    ],
                    context={
                        "action": action,
                        "url_path": url_path,
                        "reason": "write_outcome_unknown",
                        "write_committed": None,
                        "post_write_verified": False,
                    },
                )
            )
        except HomeAssistantCommandNotSent as exc:
            raise_dashboard_edit_error(
                url_path, "write_not_sent", str(exc), False, action
            )
        except Exception as exc:  # noqa: BLE001
            exception_to_structured_error(
                exc,
                context={
                    "action": action,
                    "url_path": url_path,
                    "write_committed": None,
                    "reason": "write_outcome_unknown",
                    "post_write_verified": False,
                },
                suggestions=[
                    "Read the dashboard before retrying to check whether the save applied",
                ],
            )

        if isinstance(save_result, dict) and not save_result.get("success", True):
            raise_known_dashboard_save_rejection(save_result, url_path, action)
            error_msg = save_result.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            # Core may update the live config before persistence raises. An
            # unrecognized error response cannot establish an unwritten edit.
            raise_dashboard_edit_error(
                url_path,
                "write_outcome_unknown",
                f"Failed to save {'transformed' if action == 'python_transform' else 'dashboard'} config: {error_msg}",
                None,
                action,
            )

    async def _apply_dashboard_config(
        self,
        url_path: str,
        config: dict[str, Any] | str,
        config_hash: str | None,
        dashboard_exists: bool,
        *,
        metadata_updated: bool = False,
    ) -> tuple[bool, str | None, dict[str, Any], dict[str, Any] | None]:
        """Preserve prior registry writes on any configuration-update failure."""
        try:
            return await self._prepare_and_save_dashboard_config(
                url_path, config, config_hash, dashboard_exists
            )
        except (asyncio.CancelledError, Exception) as exc:
            if dashboard_exists and not metadata_updated:
                raise
            # Native and legacy outcomes describe only the config command. A
            # preceding create/metadata call already succeeded in either case.
            if isinstance(exc, asyncio.CancelledError):
                error = create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Configuration update cancelled before saving",
                    context={"action": "set", "url_path": url_path},
                    suggestions=["Read the dashboard before deciding whether to retry"],
                )
            elif isinstance(exc, ToolError):
                error = json.loads(str(exc))
            else:
                error = exception_to_structured_error(
                    exc,
                    context={"action": "set", "url_path": url_path},
                    raise_error=False,
                )
            # Validation/read failures precede saving. Save helpers attach an
            # explicit None when they cannot determine the configuration outcome.
            error["config_write_committed"] = error.get("write_committed", False)
            error["write_committed"] = True
            error["dashboard_created"] = not dashboard_exists
            error["metadata_updated"] = metadata_updated
            prior_change = (
                "Dashboard metadata was updated"
                if dashboard_exists
                else "Dashboard was created"
            )
            error["error"]["message"] = (
                f"{prior_change}; configuration update: {error['error']['message']}"
            )
            raise_tool_error(error)
        # CodeQL does not infer the shared helper's NoReturn contract.
        raise AssertionError("unreachable: raise_tool_error always raises")

    async def _prepare_and_save_dashboard_config(
        self,
        url_path: str,
        config: dict[str, Any] | str,
        config_hash: str | None,
        dashboard_exists: bool,
    ) -> tuple[bool, str | None, dict[str, Any], dict[str, Any] | None]:
        """Return (updated, warning, saved config, native result) for either backend."""
        parsed_config = parse_json_param(config, "config")
        if parsed_config is None or not isinstance(parsed_config, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Config parameter must be a dict/object",
                    context={
                        "action": "set",
                        "provided_type": type(parsed_config).__name__,
                    },
                )
            )
        config_dict = cast(dict[str, Any], parsed_config)
        # Creation has no previous config to compare. Match the legacy create
        # path, which ignores a supplied hash; existing entries retain the guard.
        native_result = await edit_dashboard_via_component(
            self._client,
            url_path,
            expected_hash=config_hash if dashboard_exists else None,
            config=config_dict,
        )
        if native_result is not None:
            native_warning = _large_dashboard_replacement_warning(
                native_result["previous_config_size"]
            )
            return True, native_warning, native_result["config"], native_result

        warning: str | None = None
        if dashboard_exists:
            warning = await self._check_dashboard_replace_hash(
                url_path, config_hash, config_dict
            )

        await self._save_dashboard_config(url_path, config_dict)
        return True, warning, config_dict, None

    async def _attach_dashboard_write_result(
        self,
        result: dict[str, Any],
        url_path: str,
        render_config: dict[str, Any] | None,
        native_result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Reuse native verification without another dashboard fetch."""
        if native_result is None:
            return await _attach_dashboard_render_paths_after_write(
                self._client, result, url_path, render_config
            )
        for key in ("config_hash", "write_committed", "post_write_verified"):
            result[key] = native_result[key]
        if native_result.get("warnings"):
            result.setdefault("warnings", []).extend(native_result["warnings"])
        if native_result["post_write_verified"]:
            _attach_dashboard_render_paths(result, url_path, native_result["config"])
        return cast(dict[str, Any], native_result["config"])

    async def _run_dashboard_config_update(
        self,
        url_path: str,
        config: dict[str, Any] | str | None,
        config_hash: str | None,
        *,
        title: str | None,
        icon: str | None,
        require_admin: bool | None,
        show_in_sidebar: bool | None,
        pre_resolved_from: str | None,
        pre_fetched_dashboards: list[dict[str, Any]] | None,
        return_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
        MandatoryBPS: bool,
    ) -> "dict[str, Any] | ToolResult":
        """Execute config-replacement mode (create-or-update) and return the tool response."""
        reject_malformed_dashboard_config(config, url_path)
        (
            dashboard_exists,
            dashboard_id,
            metadata_updated,
            metadata_warning,
        ) = await self._ensure_dashboard_exists(
            url_path,
            title=title,
            icon=icon,
            require_admin=require_admin,
            show_in_sidebar=show_in_sidebar,
            pre_fetched_dashboards=pre_fetched_dashboards,
        )

        config_updated = False
        native_result: dict[str, Any] | None = None
        render_config: dict[str, Any] | None = None
        warnings: list[str] = []
        if config is not None:
            (
                config_updated,
                config_warning,
                render_config,
                native_result,
            ) = await self._apply_dashboard_config(
                url_path,
                config,
                config_hash,
                dashboard_exists,
                metadata_updated=metadata_updated,
            )
            if config_warning:
                warnings.append(config_warning)

        if dashboard_exists and not config_updated and not metadata_updated:
            if metadata_warning is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "No dashboard changes were applied",
                        details=metadata_warning,
                        suggestions=[
                            "Use a storage-mode dashboard when changing metadata",
                            "Provide config to replace the dashboard configuration",
                        ],
                        context={"action": "set", "url_path": url_path},
                    )
                )
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "No dashboard changes were requested",
                    suggestions=[
                        "Provide config, patch or python_transform to change dashboard content",
                        "Provide a metadata field such as title or icon",
                        "Use ha_config_get_dashboard to read a dashboard without changing it",
                    ],
                    context={"action": "set", "url_path": url_path},
                )
            )

        if metadata_warning:
            if config_updated:
                metadata_warning = f"{metadata_warning} Dashboard config was saved."
            warnings.insert(0, metadata_warning)

        result_dict: dict[str, Any] = {
            "success": True,
            "action": "create" if not dashboard_exists else "update",
            "url_path": url_path,
            "dashboard_id": dashboard_id,
            "dashboard_created": not dashboard_exists,
            "config_updated": config_updated,
            "metadata_updated": metadata_updated,
            "message": f"Dashboard {url_path} {'created' if not dashboard_exists else 'updated'} successfully",
        }

        if warnings:
            result_dict["warnings"] = warnings
        if pre_resolved_from is not None:
            # This marks identifier canonicalization only. Exact url_path
            # matches deliberately omit it; callers use ``action`` to tell
            # whether the create-or-update operation updated an existing target.
            result_dict["resolved_from"] = pre_resolved_from

        render_config = await self._attach_dashboard_write_result(
            result_dict, url_path, render_config, native_result
        )
        await _attach_dashboard_skill(result_dict, MandatoryBPS, self._client)
        return await _maybe_attach_screenshot(
            result_dict,
            url_path,
            return_screenshot,
            client=self._client,
            config=render_config,
            options=screenshot_options,
        )

    @tool(
        name="ha_config_delete_dashboard",
        tags={"Dashboards"},
        annotations=write_hints(
            "Delete Dashboard", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(domain="dashboard", id_param="url_path")
    @log_tool_usage
    async def ha_config_delete_dashboard(
        self,
        url_path: Annotated[
            str,
            Field(
                description="Dashboard URL path or internal ID to delete "
                "(e.g., 'my-dashboard' or 'my_dashboard'). Both forms are accepted."
            ),
        ],
    ) -> dict[str, Any]:
        """
        Delete a storage-mode dashboard completely.

        WARNING: This permanently deletes the dashboard and all its configuration.
        Cannot be undone. Does not work on YAML-mode dashboards.

        HA internal IDs may differ from url_path (e.g. hyphens → underscores); the tool resolves
        either form to the actual registry ID before deletion.

        EXAMPLES:
        - Delete dashboard: ha_config_delete_dashboard("mobile-dashboard")

        Note: The default dashboard cannot be deleted via this method.
        """
        try:
            # ``url_path`` is required. Reject empty/whitespace up-front so
            # the caller gets a structured parameter error instead of a
            # misleading "no dashboard found" from the resolver below.
            # Extension of the #1312 validate_identifier_not_empty pattern
            # to the dashboards family per #1313.
            validate_identifier_not_empty(
                url_path,
                "url_path",
                suggestions=[
                    "Pass a dashboard URL path or internal ID (e.g. 'my-dashboard')",
                    "Use ha_config_get_dashboard(list_only=True) to list dashboards",
                ],
                context={"action": "delete"},
            )
            resolved, dashboards = await _resolve_dashboard(self._client, url_path)
            if dashboards is None:
                _raise_dashboard_registry_read_error(action="delete", url_path=url_path)
            if resolved is None:
                available_ids = [
                    d.get("url_path") for d in dashboards[:10] if d.get("url_path")
                ]
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Dashboard '{url_path}' not found",
                        details=f"No dashboard found with URL path or internal ID '{url_path}'.",
                        suggestions=[
                            "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                            "YAML-mode and default dashboards are not deletable via this tool",
                        ],
                        context={
                            "action": "delete",
                            "url_path": url_path,
                            "available_dashboard_ids": available_ids,
                        },
                    )
                )
            resolved_id = resolved["id"]

            response = await self._client.send_websocket_message(
                {"type": "lovelace/dashboards/delete", "dashboard_id": resolved_id}
            )

            # Check response for error indication
            if isinstance(response, dict) and not response.get("success", True):
                error_msg = response.get("error", {})
                if isinstance(error_msg, dict):
                    error_str = error_msg.get("message", str(error_msg))
                else:
                    error_str = str(error_msg)

                # If the error is "not found" / "doesn't exist", treat as success (idempotent)
                if (
                    "unable to find" in error_str.lower()
                    or "not found" in error_str.lower()
                ):
                    return {
                        "success": True,
                        "action": "delete",
                        "url_path": url_path,
                        "message": "Dashboard already deleted or does not exist",
                    }

                # For other errors, raise
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to delete dashboard: {error_str}",
                        suggestions=[
                            "Verify dashboard exists and is storage-mode",
                            "Check that you have admin permissions",
                            "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                            "Cannot delete YAML-mode or default dashboard",
                        ],
                        context={"action": "delete", "url_path": url_path},
                    )
                )

            # Delete successful
            result: dict[str, Any] = {
                "success": True,
                "action": "delete",
                "url_path": url_path,
                "message": "Dashboard deleted successfully",
            }
            if resolved_id != url_path:
                result["resolved_id"] = resolved_id
            return result
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"action": "delete", "url_path": url_path},
                suggestions=[
                    "Verify dashboard exists and is storage-mode",
                    "Check that you have admin permissions",
                    "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                    "Cannot delete YAML-mode or default dashboard",
                ],
            )
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


# =========================================================================
# Dashboard Resource Management Tools
# =========================================================================
# Resource tools have been moved to tools_resources.py for better organization.
# Available tools:
# - ha_config_list_dashboard_resources: List all resources
# - ha_config_set_dashboard_resource: Create/update resources (inline code or URL)
# - ha_config_delete_dashboard_resource: Delete resources
# =========================================================================


def register_config_dashboard_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant dashboard configuration tools."""
    register_tool_methods(mcp, DashboardConfigTools(client))
