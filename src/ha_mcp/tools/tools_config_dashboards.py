"""
Configuration management tools for Home Assistant Lovelace dashboards.

This module provides tools for managing dashboard metadata and content.
"""

import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal, cast, overload

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from fastmcp.tools.tool import ToolResult
from pydantic import Field

from ..client.rest_client import (
    HomeAssistantCommandError,
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
    resolve_dashboard_view,
)
from ..errors import ErrorCode, create_error_response
from ..strict_bps import BestPracticeKeyParam
from ..utils.config_hash import compute_config_hash
from ..utils.python_sandbox import (
    PythonSandboxError,
    PythonSandboxExecutionError,
    format_sandbox_error,
    get_security_documentation,
    safe_execute,
)
from .auto_backup import with_auto_backup
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    extract_tool_error_message,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .util_helpers import (
    JSON_STRING_COERCION,
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
    parse_json_param,
)

logger = logging.getLogger(__name__)


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


# dashboard-guide.md + dashboard-cards.md cover layout patterns and the
# card-type taxonomy — both relevant on every dashboard write.
_DASHBOARD_SKILL_FILES: tuple[str, ...] = (
    "references/dashboard-guide.md",
    "references/dashboard-cards.md",
)


def _attach_dashboard_skill(response: dict[str, Any], MandatoryBPS: bool) -> None:
    """In-place attach skill_content to a dashboard response when applicable.

    Delegates to the shared :func:`attach_skill_content` so the
    missing-vendor-warning path is consistent across every write tool.
    """
    attach_skill_content(
        response,
        MandatoryBPS=MandatoryBPS,
        canonical_files=_DASHBOARD_SKILL_FILES,
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
    WebSocket call reports failure or the response is not a dict; callers
    can rely on the returned tuple being populated.
    """
    get_data: dict[str, Any] = {"type": "lovelace/config", "force": True}
    if url_path:
        get_data["url_path"] = url_path

    response = await client.send_websocket_message(get_data)

    if isinstance(response, dict) and not response.get("success", True):
        error_msg = response.get("error", {})
        if isinstance(error_msg, dict):
            error_msg = error_msg.get("message", str(error_msg))
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Dashboard fetch failed: {error_msg}",
                context={"url_path": url_path},
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


async def _verify_config_unchanged(
    client: Any,
    url_path: str,
    original_hash: str,
) -> dict[str, Any]:
    """
    Verify dashboard config hasn't changed since original read.

    Returns dict with:
    - success: bool (True if config unchanged)
    - error: str (if config changed)
    - suggestions: list[str] (if config changed)
    """
    # Re-fetch current config. The component ``get`` reads the same in-memory
    # object core serves (freshness-safe, audit-verified — the optimistic-lock
    # re-read must not lag a concurrent save); ``None`` (component unavailable /
    # YAML body / not found) falls back to the unchanged legacy re-fetch.
    current_config: Any = await _component_dashboard_config(client, url_path or None)
    if current_config is None:
        get_data: dict[str, Any] = {"type": "lovelace/config"}
        if url_path:
            get_data["url_path"] = url_path

        result = await client.send_websocket_message(get_data)
        current_config = (
            result.get("result", result) if isinstance(result, dict) else result
        )

    if not isinstance(current_config, dict):
        return {"success": True}  # Can't verify, proceed anyway

    current_hash = compute_config_hash(current_config)

    if current_hash != original_hash:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Dashboard modified since last read (conflict)",
                suggestions=[
                    "Re-read dashboard with ha_config_get_dashboard",
                    "Then retry the operation with fresh data",
                ],
            )
        )

    return {"success": True}


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


# Keys under which a card nests other cards, by descent rule (issue #1599):
#   - ``cards`` (list): vertical/horizontal-stack, grid, and any custom wrapper
#     following the stack convention.
#   - ``card`` (dict): conditional and wrapper cards such as
#     ``custom:auto-entities``.
#   - ``custom_fields`` (dict of field-configs): ``custom:button-card`` embeds
#     sub-cards under ``custom_fields.<name>.card`` (a very common pattern that
#     wraps an entire view in one button-card). Each field-config is descended
#     as a node, so its own ``card`` / ``cards`` are picked up by the recursion.
#   - ``states`` (name->card map): ``custom:state-switch`` swaps a whole card per
#     source state. Each value is itself a card, descended directly as a node.
# Picture-elements ``elements`` is deliberately NOT traversed: it is not one of
# the descent keys above, so a node carrying it is disclosed at the response
# boundary instead of being walked (see ``_UNTRAVERSED_NESTED_KEYS`` and the
# find-card warnings). A blanket "descend every dict with a ``type``" walk is
# intentionally avoided: tile ``features`` and view ``conditions`` also carry
# ``type`` and would false-match as cards.
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
    ) -> None:
        self.entity_id = entity_id
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
        )


def _walk_card_list_key(
    card: dict[str, Any],
    key: str,
    *,
    jq_prefix: str,
    python_prefix: str,
    frame: _CardWalkFrame,
) -> list[dict[str, Any]]:
    """Descend a list-of-cards child (the ``cards`` key: stacks, grids, ...)."""
    matches: list[dict[str, Any]] = []
    child_list = card.get(key)
    if isinstance(child_list, list):
        child_frame = frame.descend()
        for i, child in enumerate(child_list):
            matches.extend(
                _walk_card(
                    child,
                    jq_prefix=f"{jq_prefix}.{key}[{i}]",
                    python_prefix=f"{python_prefix}['{key}'][{i}]",
                    frame=child_frame,
                )
            )
    elif child_list is not None:
        # Key present but not a list — structurally malformed slot.
        logger.debug(
            "Card-search skipping non-list '%s' under %s (%s)",
            key,
            jq_prefix,
            type(child_list).__name__,
        )
    return matches


def _walk_card_dict_key(
    card: dict[str, Any],
    key: str,
    *,
    jq_prefix: str,
    python_prefix: str,
    frame: _CardWalkFrame,
) -> list[dict[str, Any]]:
    """Descend a single-card dict child (the ``card`` key: conditional/wrapper cards)."""
    matches: list[dict[str, Any]] = []
    child = card.get(key)
    if isinstance(child, dict):
        matches.extend(
            _walk_card(
                child,
                jq_prefix=f"{jq_prefix}.{key}",
                python_prefix=f"{python_prefix}['{key}']",
                frame=frame.descend(),
            )
        )
    elif child is not None:
        # Key present but not a dict — structurally malformed slot.
        logger.debug(
            "Card-search skipping non-dict '%s' under %s (%s)",
            key,
            jq_prefix,
            type(child).__name__,
        )
    return matches


def _walk_card_named_children(
    card: dict[str, Any],
    key: str,
    *,
    jq_prefix: str,
    python_prefix: str,
    frame: _CardWalkFrame,
) -> list[dict[str, Any]]:
    """Descend a name-keyed dict of card children (``custom_fields``, ``states``).

    Each value is itself descended as a node (its own ``card``/``cards`` and the
    ``type`` gate are handled by the recursion). Keys are rendered quote/dot-safe
    so a name like ``o'brien`` yields a usable python_path/jq_path (issue #1599).
    """
    matches: list[dict[str, Any]] = []
    children = card.get(key)
    if isinstance(children, dict):
        child_frame = frame.descend()
        for name, child in children.items():
            if not isinstance(name, str):
                _log_non_str_key(key, name, jq_prefix)
                continue
            matches.extend(
                _walk_card(
                    child,
                    jq_prefix=f"{jq_prefix}.{key}{_jq_key(name)}",
                    python_prefix=f"{python_prefix}['{key}']{_py_key(name)}",
                    frame=child_frame,
                )
            )
    elif children is not None:
        logger.debug(
            "Card-search skipping non-dict '%s' under %s (%s)",
            key,
            jq_prefix,
            type(children).__name__,
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

    Descends ``cards`` (list), ``card`` (dict), each ``custom_fields`` value, and
    each ``states`` value (custom:state-switch), for nested as well as top-level
    cards, up to ``_MAX_CARD_DEPTH``.

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
        if _card_matches(card, frame.entity_id, frame.card_type, frame.heading):
            matches.append(
                {
                    "view_index": frame.view_index,
                    "section_index": frame.section_index,
                    "card_index": frame.card_index,
                    "jq_path": jq_prefix,
                    "python_path": python_prefix,
                    "card_type": card.get("type"),
                    "card_config": card,
                }
            )
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
        _walk_card_list_key(
            card,
            _NESTED_CARDS_KEY,
            jq_prefix=jq_prefix,
            python_prefix=python_prefix,
            frame=frame,
        )
    )
    matches.extend(
        _walk_card_dict_key(
            card,
            _NESTED_CARD_KEY,
            jq_prefix=jq_prefix,
            python_prefix=python_prefix,
            frame=frame,
        )
    )
    matches.extend(
        _walk_card_named_children(
            card,
            _NESTED_CUSTOM_FIELDS_KEY,
            jq_prefix=jq_prefix,
            python_prefix=python_prefix,
            frame=frame,
        )
    )
    matches.extend(
        _walk_card_named_children(
            card,
            _NESTED_STATES_KEY,
            jq_prefix=jq_prefix,
            python_prefix=python_prefix,
            frame=frame,
        )
    )
    return matches


def _find_badge_matches_in_view(
    view: dict[str, Any],
    view_idx: int,
    entity_id: str | None,
    card_type: str | None,
    heading: str | None,
) -> list[dict[str, Any]]:
    """Find view-level badges matching the search criteria (entity_id-driven only)."""
    matches: list[dict[str, Any]] = []
    if not (
        entity_id is not None
        and heading is None
        and (card_type is None or card_type == "badge")
    ):
        return matches
    badges = view.get("badges", [])
    for badge_idx, badge in enumerate(badges):
        if not _badge_matches(badge, entity_id):
            continue
        is_dict_badge = isinstance(badge, dict)
        badge_config = badge if is_dict_badge else {"entity": badge}
        badge_match: dict[str, Any] = {
            "view_index": view_idx,
            "section_index": None,
            "card_index": None,
            "badge_index": badge_idx,
            "jq_path": f".views[{view_idx}].badges[{badge_idx}]",
            "card_type": "badge",
            "card_config": badge_config,
        }
        # A bare-string badge (the common form) is not subscript-assignable, so
        # a python_path spliced into python_transform would raise TypeError.
        # Only advertise python_path for dict badges; string badges must be
        # converted to dict form first.
        if is_dict_badge:
            badge_match["python_path"] = f"['views'][{view_idx}]['badges'][{badge_idx}]"
        matches.append(badge_match)
    return matches


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
) -> list[dict[str, Any]]:
    """
    Find cards, badges, and header cards in a dashboard config matching the search criteria.

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

        matches.extend(
            _find_badge_matches_in_view(view, view_idx, entity_id, card_type, heading)
        )

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
        )
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
        else:
            if entity_id not in all_entities:
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
# error.code "config_not_found". The websocket client currently surfaces only
# the message string, so substring matching is the only signal available at
# the tool layer. If HA reformats this string, the lazy fallback regresses
# silently to never firing — re-verify with major HA upgrades.
_LAZY_RESOLVE_TRIGGER = "Unknown config specified"


def _should_lazy_resolve(error_msg: str) -> bool:
    """Return True if a WS error message indicates the identifier needs resolving."""
    return _LAZY_RESOLVE_TRIGGER in error_msg


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

# Cross-dashboard ``search`` match cap — mirrors the component's
# ``_DASHBOARD_MATCH_CAP`` so the component-less legacy walk truncates identically
# (parity pinned by test_component_dashboards_contract.py).
_SEARCH_ALL_MATCH_CAP = 200

# Structural keys walked as their own card containers, never scored as leaf
# strings — mirrors the component's ``_DASHBOARD_STRUCTURAL_KEYS``.
_SEARCH_ALL_STRUCTURAL_KEYS = frozenset({"cards", "sections"})


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
    (logged), a malformed envelope, or ``available: false`` (lovelace not set
    up). A ``HomeAssistantConnectionError`` (WS down) is not caught here; it
    propagates and the legacy path, sharing the same socket, would fail
    identically.
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
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_DASHBOARDS, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_DASHBOARDS, exc)
        return None
    result = raw.get("result")
    if not isinstance(result, dict) or not result.get("available"):
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
    return config if isinstance(config, dict) else None


# --- Cross-dashboard search walk (component-less legacy path) -----------------
# Byte-for-byte port of the component's ``_search_dashboard_docs`` walk
# (custom_components/ha_mcp_tools/websocket_api.py) so a component-less install
# gets the SAME cross-dashboard ``search`` matches, just at N+1 WS cost. The two
# implementations are pinned equal by test_component_dashboards_contract.py.


def _walk_all_dashboard_docs(
    docs: list[dict[str, Any]], query_lower: str
) -> tuple[list[dict[str, Any]], bool]:
    """Walk each dashboard config for ``query_lower``; return ``(matches, truncated)``.

    An empty query matches nothing (a bare substring would match every string).
    Matches are capped at :data:`_SEARCH_ALL_MATCH_CAP` with a ``truncated`` flag.
    """
    if not query_lower:
        return [], False
    matches: list[dict[str, Any]] = []
    for doc in docs:
        _collect_all_dashboard_doc_matches(doc, query_lower, matches)
    truncated = len(matches) > _SEARCH_ALL_MATCH_CAP
    return matches[:_SEARCH_ALL_MATCH_CAP], truncated


def _collect_all_dashboard_doc_matches(
    doc: dict[str, Any], query_lower: str, matches: list[dict[str, Any]]
) -> None:
    """Append every ``query_lower`` hit in one dashboard config to ``matches``."""
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
        for cards, base_path in _all_dashboard_view_card_containers(view, view_index):
            _collect_all_dashboard_card_matches(
                cards,
                base_path,
                url_path,
                dash_title,
                view_index,
                view_title,
                query_lower,
                matches,
            )


def _all_dashboard_view_card_containers(
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


def _collect_all_dashboard_card_matches(
    cards: Any,
    base_path: str,
    url_path: Any,
    dash_title: Any,
    view_index: int,
    view_title: Any,
    query_lower: str,
    matches: list[dict[str, Any]],
) -> None:
    """Recurse a card list, recording one match per string leaf containing the query."""
    if not isinstance(cards, list):
        return
    for card_index, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        card_path = f"{base_path}[{card_index}]"
        card_type = card.get("type")
        for field, value in _all_dashboard_card_string_leaves(card):
            if query_lower in value.lower():
                matches.append(
                    {
                        "url_path": url_path,
                        "title": dash_title,
                        "view_index": view_index,
                        "view_title": view_title,
                        "card_path": card_path,
                        "card_type": card_type,
                        "matched_field": field,
                        "matched_value": value,
                    }
                )
        nested = card.get("cards")
        if isinstance(nested, list):
            _collect_all_dashboard_card_matches(
                nested,
                f"{card_path}.cards",
                url_path,
                dash_title,
                view_index,
                view_title,
                query_lower,
                matches,
            )


def _all_dashboard_card_string_leaves(card: dict[str, Any]) -> list[tuple[str, str]]:
    """``(immediate_key, string)`` for every string leaf of a card.

    Descends nested dicts/lists but NOT the structural ``cards``/``sections`` keys
    (those are walked as their own cards). The key attributed to a leaf is the
    nearest dict key, matching the component's field taxonomy.
    """
    out: list[tuple[str, str]] = []
    _walk_all_dashboard_card_leaves(card, "", out)
    return out


def _walk_all_dashboard_card_leaves(
    value: Any, key: str, out: list[tuple[str, str]]
) -> None:
    """Recursive worker for :func:`_all_dashboard_card_string_leaves`."""
    if isinstance(value, str):
        if value:
            out.append((key, value))
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in _SEARCH_ALL_STRUCTURAL_KEYS:
                _walk_all_dashboard_card_leaves(v, str(k), out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_all_dashboard_card_leaves(item, key, out)


async def fetch_dashboards_list(
    client: Any,
) -> list[dict[str, Any]] | None:
    """Fetch and normalise the lovelace/dashboards/list WebSocket response.

    Returns the list of dashboard registry entries on success, or ``None``
    when the response shape is unrecognised.  A warning is logged on
    unexpected shapes so that future HA response-format changes surface at
    every fetch site rather than silently degrading.

    When the ``ha_mcp_tools`` component advertises the ``dashboards`` capability
    the storage-only rows are served from one in-process ``list`` frame (the
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
    except Exception as resolver_exc:
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
    try:
        authoritative_config, _ = await _get_dashboard_config_internal(client, url_path)
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
    """Warn when a screenshot was requested in a mode that can't render one.

    Screenshot options are only honoured in get mode. In list and search mode
    they are accepted but inapplicable, so surface a
    ``warnings`` entry rather than dropping the request as a silent no-op
    (matches the warn-don't-fail contract the params document)."""
    capture_options = options or _DashboardScreenshotOptions(full_page=full_page)
    if include_screenshot or capture_options != _DashboardScreenshotOptions():
        result.setdefault("warnings", []).append(
            f"include_screenshot and screenshot render options are ignored in {mode} "
            "mode; call "
            "ha_config_get_dashboard with a url_path (and no search criteria) "
            "to get a screenshot."
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
        capture_warnings = dashboard_screenshot_warnings(captures)
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
    except Exception as exc:
        raise_tool_error(
            create_error_response(
                ErrorCode.IMAGE_SERIALIZATION_FAILED,
                "Rendered dashboard images could not be packaged into the MCP response.",
                details=str(exc),
                context={"capture_count": len(captures), "render_path": render_path},
            )
        )
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
            if key not in {"success", "error"}
        }
    )
    result["screenshot_error"] = screenshot_error
    result.setdefault("warnings", []).append(
        f"Screenshot unavailable: {extract_tool_error_message(error)}"
    )
    return result


class DashboardConfigTools:
    """Home Assistant dashboard configuration tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_get_dashboard",
        tags={"Dashboards"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "title": "Get Dashboard",
        },
    )
    @log_tool_usage
    async def ha_config_get_dashboard(
        self,
        url_path: Annotated[
            str | None,
            Field(
                description="Dashboard URL path (e.g., 'lovelace-home'). "
                "Use 'default' for default dashboard. "
                "If omitted with list_only=True, lists all dashboards."
            ),
        ] = None,
        list_only: Annotated[
            bool,
            Field(
                description="If True, list all dashboards instead of getting config. "
                "When True, url_path is ignored.",
            ),
        ] = False,
        force_reload: Annotated[
            bool,
            Field(
                description="Force reload from storage (bypass cache). Not applicable in search mode (search always uses force=True for fresh results)."
            ),
        ] = False,
        entity_id: Annotated[
            str | None,
            Field(
                description="Find cards by entity ID. Supports wildcards, e.g. "
                "'sensor.temperature_*'. Matches cards with this entity in "
                "'entity' or 'entities' field, view-level badges, and header cards. "
                "When provided, activates search mode (returns matches, not full config)."
            ),
        ] = None,
        card_type: Annotated[
            str | None,
            Field(
                description="Find cards by type, e.g. 'tile', 'button', 'heading'. "
                "When provided, activates search mode."
            ),
        ] = None,
        heading: Annotated[
            str | None,
            Field(
                description="Find cards by heading/title text (case-insensitive partial match). "
                "When provided, activates search mode."
            ),
        ] = None,
        include_config: Annotated[
            bool,
            Field(
                description="In search mode: include each matched card's own configuration "
                "object in results (increases output size). Note that a matched container "
                "card's config contains its descendants, which are themselves separate "
                "matches with their own config, so deeply-nested stacks multiply the "
                "payload — keep the default (False) unless you need the bodies. Does not "
                "affect whether the full dashboard config is returned — search mode always "
                "returns matches only, not the full dashboard. Ignored outside search mode."
            ),
        ] = False,
        include_screenshot: Annotated[
            bool,
            Field(
                description="Get mode only: also return rendered image(s) of the "
                "dashboard for visual verification. Requires the 'dashboard "
                "screenshot' beta feature + engine add-on/sidecar. If the "
                "feature is disabled the config is returned with a warning; if "
                "the engine is configured but the render fails, the call errors "
                "(the screenshot is the requested payload). Ignored in "
                "list/search mode."
            ),
        ] = False,
        view_path: Annotated[
            str | None,
            Field(
                description="With include_screenshot: stable Lovelace "
                "views[].path to render. Omit to render the dashboard base route."
            ),
        ] = None,
        mode: Annotated[
            Literal["search"] | None,
            Field(
                description="Set to 'search' for a CROSS-dashboard search: which "
                "dashboards contain a given entity_id or text (requires query). "
                "Leave unset for the default list/get/single-dashboard-search "
                "behavior selected by list_only / entity_id / card_type / heading."
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="With mode='search': the entity_id or substring to "
                "find across all storage-mode dashboards. Ignored otherwise."
            ),
        ] = None,
    ) -> "dict[str, Any] | ToolResult":
        """
        Get dashboard info - list all dashboards, get config, or search for cards.

        MODE 1 — List: list_only=True
          Lists every dashboard's metadata (url_path, title, icon), storage and
          YAML alike (metadata only — bodies are never included here).

        MODE 2 — Search: any of entity_id / card_type / heading provided
          Finds cards, badges, and header cards matching the criteria, including
          cards nested inside stacks, grids, conditional cards, button-card
          custom_fields, and state-switch states. Each match carries a
          python_path and a jq_path that locate the card for nested as well as
          top-level cards. The python_path is a Python subscript chain to be
          appended after `config` — e.g.
          python_transform=f'config{m["python_path"]}["icon"] = "mdi:x"' (it is
          NOT valid on its own without the `config` prefix). jq_path is the same
          location in jq dot-notation.
          Multiple criteria are AND-ed. Always fetches fresh config (force=True).
          Search covers cards/card/custom_fields/states containers up to a depth
          bound; if the dashboard carries a non-traversed child-bearing shape
          (e.g. picture-elements `elements`), the result carries a `warnings`
          entry naming where, so its hidden content is not mistaken for absent.
          Strategy dashboards are not searchable (no explicit cards).

        MODE 3 — Get: Active when list_only=False and no search parameters are provided.
          Returns the full Lovelace dashboard config, defaulting to the
          main dashboard if url_path is omitted.

        MODE 4 — Search all: mode="search" with query=<entity_id or text>
          Answers "which dashboards contain this entity/card" by walking every
          storage-mode dashboard's views/cards/sections for the query substring.
          Each match names the url_path, view, card_path, card_type, and the
          matched field/value. Takes precedence over the other modes (list_only /
          entity_id / card_type / heading are ignored when mode="search").
          YAML-mode dashboards are never searched on either path — the component
          walk skips them in-process and the component-less legacy walk skips any
          row tagged mode="yaml" — because HA resolves `!secret` when loading a
          YAML Lovelace config, so searching one could surface resolved secrets.
          On installs without the ha_mcp_tools component, the default (unnamed)
          dashboard is also not searched — only dashboards with a url_path are.

        Return a stable `config_hash` (Get and Search modes only; not present in list_only mode) across consecutive reads of an unchanged config — `compute_config_hash` documents the underlying contract.

        EXAMPLES:
        - List all dashboards: ha_config_get_dashboard(list_only=True)
        - Get default dashboard: ha_config_get_dashboard(url_path="default")
        - Get custom dashboard: ha_config_get_dashboard(url_path="lovelace-mobile")
        - Force reload: ha_config_get_dashboard(url_path="lovelace-home", force_reload=True)
        - Find cards by entity: ha_config_get_dashboard(url_path="my-dash", entity_id="light.living_room")
        - Find by wildcard: ha_config_get_dashboard(url_path="my-dash", entity_id="sensor.temperature_*")
        - Find by type: ha_config_get_dashboard(url_path="my-dash", card_type="tile")
        - Find heading: ha_config_get_dashboard(url_path="my-dash", heading="Climate", card_type="heading")

        SEARCH WORKFLOW EXAMPLE:
        1. find = ha_config_get_dashboard(url_path="my-dash", entity_id="light.bedroom")
        2. ha_config_set_dashboard(
               url_path="my-dash",
               config_hash=find["config_hash"],
               python_transform=f'config{find["matches"][0]["python_path"]}["icon"] = "mdi:lamp"'
           )

        Note: YAML-mode dashboards (defined in configuration.yaml) are not included in list.
        """
        screenshot_options = _DashboardScreenshotOptions(view_path=view_path)
        search_mode = (
            entity_id is not None or card_type is not None or heading is not None
        )
        # Mutable single-element holder so the mode helpers can surface the
        # lazy-resolved/canonicalized url_path back to this scope even when
        # they raise an unexpected (non-ToolError) exception instead of
        # returning normally — the outer except block below needs it for
        # accurate error context.
        resolved_url_path: list[str | None] = [url_path]
        try:
            if mode == "search":
                # Cross-dashboard search takes precedence over every other mode.
                return await self._get_dashboard_search_all_mode(query)

            if list_only:
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
                    entity_id=entity_id,
                    card_type=card_type,
                    heading=heading,
                    include_config=include_config,
                    include_screenshot=include_screenshot,
                    screenshot_options=screenshot_options,
                )

            return await self._get_dashboard_get_mode(
                url_path,
                resolved_url_path=resolved_url_path,
                force_reload=force_reload,
                include_screenshot=include_screenshot,
                screenshot_options=screenshot_options,
            )
        except ToolError:
            raise
        except Exception as e:
            effective_url_path = resolved_url_path[0]
            if mode == "search":
                suggestions = [
                    "Check HA connection",
                    "Verify dashboards with ha_config_get_dashboard(list_only=True)",
                ]
                context: dict[str, Any] = {
                    "action": "search_all",
                    "query": query,
                }
            elif search_mode:
                suggestions = [
                    "Check HA connection",
                    "Verify dashboard with ha_config_get_dashboard(list_only=True)",
                ]
                context = {
                    "action": "find_card",
                    "url_path": effective_url_path,
                    "entity_id": entity_id,
                    "card_type": card_type,
                    "heading": heading,
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
        self,
        url_path: str | None,
        *,
        entity_id: str | None,
        card_type: str | None,
        heading: str | None,
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
                    context={"action": "find_card", "url_path": url_path},
                )
            )

        config = response.get("result") if isinstance(response, dict) else response
        if not isinstance(config, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Dashboard config is empty or invalid",
                    suggestions=["Initialize dashboard with ha_config_set_dashboard"],
                    context={"action": "find_card", "url_path": url_path},
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
                    context={"action": "find_card", "url_path": url_path},
                )
            )

        return config, url_path, search_resolved_from

    @staticmethod
    def _build_search_result(
        config: dict[str, Any],
        url_path: str | None,
        *,
        entity_id: str | None,
        card_type: str | None,
        heading: str | None,
        include_config: bool,
        search_resolved_from: str | None,
    ) -> dict[str, Any]:
        """Run the card search over ``config`` and assemble the response dict."""
        truncation: list[str] = []
        uncovered: list[str] = []
        matches = _find_cards_in_config(
            config,
            entity_id,
            card_type,
            heading,
            truncation=truncation,
            uncovered=uncovered,
        )

        if not include_config:
            for match in matches:
                del match["card_config"]

        config_hash: str | None = compute_config_hash(config)

        # Warn-don't-truncate (AGENTS.md Return Values): the walker covers
        # cards / card / custom_fields / states containers and stops at
        # the depth bound, so neither a depth-truncated search nor a
        # search over a dashboard carrying a non-traversed child-bearing
        # shape may read as an authoritative complete result. Disclosure
        # keys off the *presence* of such a shape (collected during the
        # walk), not off a 0-match — a matching un-walkable container no
        # longer suppresses the warning, and a true negative over a
        # fully-coverable dashboard no longer cries wolf.
        warnings: list[str] = []
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
                "Cards nesting content under keys this search does not "
                "traverse (e.g. picture-elements 'elements') are present at: "
                f"{locations}. That nested content is not searched; fetch the "
                "full config (ha_config_get_dashboard without search params) "
                "to inspect those."
            )

        if matches:
            hint = (
                "Use python_path with "
                "ha_config_set_dashboard(python_transform=...) for targeted "
                "updates"
            )
        else:
            hint = (
                "No matches in searched containers. Try other criteria, or "
                "fetch the full config (no search params) to inspect nesting "
                "shapes this search does not cover."
            )

        search_result: dict[str, Any] = {
            "success": True,
            "action": "find_card",
            "url_path": url_path,
            "config_hash": config_hash,
            "search_criteria": {
                "entity_id": entity_id,
                "card_type": card_type,
                "heading": heading,
            },
            "matches": matches,
            "match_count": len(matches),
            "hint": hint,
        }
        if warnings:
            search_result["warnings"] = warnings
        if search_resolved_from is not None:
            search_result["resolved_from"] = search_resolved_from
        return search_result

    async def _get_dashboard_search_mode(
        self,
        url_path: str | None,
        *,
        resolved_url_path: list[str | None],
        entity_id: str | None,
        card_type: str | None,
        heading: str | None,
        include_config: bool,
        include_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> dict[str, Any]:
        """Search mode: find cards, badges, or header cards matching criteria."""
        (
            config,
            url_path,
            search_resolved_from,
        ) = await self._fetch_search_dashboard_config(
            url_path, entity_id=entity_id, card_type=card_type, heading=heading
        )
        # Surface the canonicalized url_path to the caller's scope now, so
        # an unexpected exception from the search/hashing below still
        # reports the resolved identifier (see ha_config_get_dashboard's
        # except block).
        resolved_url_path[0] = url_path
        search_result = self._build_search_result(
            config,
            url_path,
            entity_id=entity_id,
            card_type=card_type,
            heading=heading,
            include_config=include_config,
            search_resolved_from=search_resolved_from,
        )
        _note_screenshot_ignored(
            search_result,
            include_screenshot=include_screenshot,
            options=screenshot_options,
            mode="search",
        )
        return search_result

    async def _get_dashboard_search_all_mode(self, query: str | None) -> dict[str, Any]:
        """mode='search': find which storage dashboards contain ``query``.

        The component ``search`` walks every storage dashboard in one in-process
        frame; a component-less install runs the SAME walk server-side over each
        dashboard's config (one WS get per storage dashboard). Both paths return
        byte-identical match records.
        """
        if not query or not query.strip():
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "query is required for mode='search'",
                    suggestions=[
                        "Pass query='light.kitchen' to find dashboards using that entity",
                        "Use list_only=True to list dashboards, or url_path=... to get one",
                    ],
                    context={"action": "search_all"},
                )
            )

        matches, truncated = await self._search_all_dashboards(query)
        result: dict[str, Any] = {
            "success": True,
            "action": "search_all",
            "query": query,
            "matches": matches,
            "match_count": len(matches),
            "truncated": truncated,
            "hint": (
                "Each match names the dashboard (url_path), view, and card path. "
                "Use ha_config_get_dashboard(url_path=..., entity_id=...) for the "
                "python_path to edit a specific card."
                if matches
                else "No storage-mode dashboard card contains that query."
            ),
        }
        if truncated:
            result["warnings"] = [
                f"Results capped at {_SEARCH_ALL_MATCH_CAP} matches; "
                "refine the query for a complete list."
            ]
        return result

    async def _search_all_dashboards(
        self, query: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Cross-dashboard matches for ``query`` — component frame or legacy walk.

        Returns ``(matches, truncated)``. When the component serves ``search`` the
        matches come straight from its one in-process frame; otherwise the storage
        dashboards are listed, each config fetched, and the SAME walk run
        server-side (parity pinned by test_component_dashboards_contract.py).
        """
        result = await _dashboards_via_component(self._client, "search", query=query)
        if result is not None:
            matches = result.get("matches")
            return (
                matches if isinstance(matches, list) else [],
                bool(result.get("truncated")),
            )

        docs = await self._collect_legacy_search_docs()
        # Mirror the component's query normalization exactly (parity).
        query_lower = (query or "").strip().lower()
        return _walk_all_dashboard_docs(docs, query_lower)

    async def _collect_legacy_search_docs(self) -> list[dict[str, Any]]:
        """Storage-dashboard ``{url_path, title, config}`` docs for the legacy walk.

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
                continue
            title = config.get("title")
            docs.append(
                {
                    "url_path": url_path,
                    "title": str(title) if title is not None else None,
                    "config": config,
                }
            )
        return docs

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
        except ToolError:
            return None
        return config

    async def _get_dashboard_get_mode(
        self,
        url_path: str | None,
        *,
        resolved_url_path: list[str | None],
        force_reload: bool,
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
        actually bust HA's Lovelace cache. (The optimistic-lock re-read in
        ``_verify_config_unchanged`` deliberately keeps the no-force component read
        — it wants the same in-memory object core serves.)
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
            include_screenshot=include_screenshot,
            screenshot_options=screenshot_options,
        )

    async def _finalize_get_result(
        self,
        url_path: str | None,
        config: Any,
        *,
        original_url_path: str | None,
        include_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> "dict[str, Any] | ToolResult":
        """Shape + return the get-mode response for an already-fetched config.

        Shared by the component fast path and the legacy path so both produce a
        byte-identical envelope: ``config_hash`` (optimistic locking), size,
        render paths, ``resolved_from`` (when the legacy lazy resolver
        canonicalised an internal id), the large-config disclosure hint, and an
        optional screenshot.
        """
        # Compute hash for optimistic locking in subsequent operations
        config_hash = compute_config_hash(config) if isinstance(config, dict) else None

        # Calculate config size for progressive disclosure hint
        config_size = len(json.dumps(config)) if isinstance(config, dict) else 0

        get_result: dict[str, Any] = {
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
        # Surface the original caller-passed identifier when the lazy
        # resolver canonicalised it (parity with delete_dashboard's
        # resolved_id field). Caller can use this to detect that their
        # input was an internal id rather than a url_path.
        if original_url_path is not None and original_url_path != url_path:
            get_result["resolved_from"] = original_url_path

        # Add hint for large configs (progressive disclosure) - 10KB ≈ 2-3k tokens
        if config_size >= 10000:
            get_result["hint"] = (
                f"Large config ({config_size:,} bytes). For edits, use "
                "ha_config_get_dashboard(entity_id=...) to find card positions, "
                "then ha_config_set_dashboard(python_transform=...) "
                "instead of full config replacement."
            )

        return await _maybe_attach_screenshot(
            get_result,
            url_path,
            include_screenshot,
            client=self._client,
            config=config if isinstance(config, dict) else None,
            options=screenshot_options,
            raise_on_failure=True,
        )

    @tool(
        name="ha_config_set_dashboard",
        tags={"Dashboards"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Create or Update Dashboard",
        },
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
                "Mutually exclusive with python_transform."
            ),
        ] = None,
        python_transform: Annotated[
            str | None,
            Field(
                description="Python expression to transform existing dashboard config. "
                "Mutually exclusive with config. "
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
                "REQUIRED for python_transform (validates dashboard unchanged). "
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
                description="After writing, also return rendered image(s) of the "
                "dashboard so you can see what it looks like in a single call "
                "(the dashboard creation/iteration loop). Requires the "
                "'dashboard screenshot' beta feature + engine add-on/sidecar; "
                "if unavailable, the write result is returned with a warning."
            ),
        ] = False,
        view_path: Annotated[
            str | None,
            Field(
                description="With return_screenshot: stable Lovelace "
                "views[].path to render."
            ),
        ] = None,
    ) -> "dict[str, Any] | ToolResult":
        """
        Create or update a Home Assistant dashboard.

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.

        Creates a new dashboard or updates an existing one with the provided configuration.
        Supports two modes: full config replacement OR Python transformation.

        Use 'default' or 'lovelace' to target the built-in default dashboard.
        New dashboards require a hyphenated url_path (e.g., 'my-dashboard').

        WHEN TO USE WHICH MODE:
        - python_transform: RECOMMENDED for edits. Surgical/pattern-based updates, works on all platforms.
        - config: New dashboards only, or full restructure. Replaces everything.

        IMPORTANT: After delete/add operations, indices shift! Subsequent python_transform calls
        must use fresh config_hash from ha_config_get_dashboard()
        to get updated structure. Chain multiple ops in ONE expression when possible.

        TIP: Use ha_config_get_dashboard(entity_id=...) to get the path for any card.

        PYTHON TRANSFORM EXAMPLES (RECOMMENDED):
        - Update card icon: 'config["views"][0]["cards"][0]["icon"] = "mdi:thermometer"'
        - Add card: 'config["views"][0]["cards"].append({"type": "button", "entity": "light.bedroom"})'
        - Delete card: 'del config["views"][0]["cards"][2]'
        - Pattern-based update: 'for card in config["views"][0]["cards"]: if "light" in card.get("entity", ""): card["icon"] = "mdi:lightbulb"'
        - Multi-operation: 'config["views"][0]["cards"][0]["icon"] = "mdi:a"; config["views"][0]["cards"][1]["icon"] = "mdi:b"'

        MODERN DASHBOARD BEST PRACTICES:
        - Use "sections" view type (default) with grid-based layouts
        - Use "tile" cards as primary card type (replaces legacy entity/light/climate cards)
        - Use "grid" cards for multi-column layouts within sections
        - Create multiple views with navigation paths (avoid single-view endless scrolling)
        - Use "area" cards with navigation for hierarchical organization

        DISCOVERING ENTITY IDs FOR DASHBOARDS:
        Do NOT guess entity IDs - use these tools to find exact entity IDs:
        1. ha_get_overview(include_entity_id=True) - Get all entities organized by domain/area
        2. ha_search(query, domain_filter, area_filter, search_types) - Find entities and config-body references in one call

        If unsure about entity IDs, ALWAYS use one of these tools first.

        DASHBOARD DOCUMENTATION:
        - dashboard-guide.md and dashboard-cards.md ship in this response
          under ``skill_content`` by default — layout patterns,
          card-type taxonomy, and worked examples.
        - ha_get_skill_guide — deeper card-type and configuration guidance.

        EXAMPLES:

        Create empty dashboard:
        ha_config_set_dashboard(
            url_path="mobile-dashboard",
            title="Mobile View",
            icon="mdi:cellphone"
        )

        Create dashboard with modern sections view:
        ha_config_set_dashboard(
            url_path="home-dashboard",
            title="Home Overview",
            config={
                "views": [{
                    "title": "Home",
                    "type": "sections",
                    "sections": [{
                        "title": "Climate",
                        "cards": [{
                            "type": "tile",
                            "entity": "climate.living_room",
                            "features": [{"type": "target-temperature"}]
                        }]
                    }]
                }]
            }
        )

        Create strategy-based dashboard (auto-generated):
        ha_config_set_dashboard(
            url_path="my-home",
            title="My Home",
            config={
                "strategy": {
                    "type": "home",
                    "favorite_entities": ["light.bedroom"]
                }
            }
        )

        Note: Strategy dashboards cannot be converted to custom dashboards via this tool.
        Use the "Take Control" feature in the Home Assistant interface to convert them.

        Update existing dashboard config:
        ha_config_set_dashboard(
            url_path="existing-dashboard",
            config={
                "views": [{
                    "title": "Updated View",
                    "type": "sections",
                    "sections": [{
                        "cards": [{"type": "markdown", "content": "Updated!"}]
                    }]
                }]
            }
        )

        Note: When updating an existing dashboard, title/icon/require_admin/show_in_sidebar
        are also updated if explicitly provided alongside (or instead of) a config change.

        STORAGE-MODE vs YAML-MODE DASHBOARDS:
        This tool only manages storage-mode dashboards (created via UI/API and stored in
        Home Assistant's storage backend). It does NOT touch YAML-defined dashboards.
        Two distinct YAML cases exist and this tool covers neither:
        - "YAML-mode" dashboards: written in their own .yaml file referenced from
          configuration.yaml under ``lovelace: dashboards:``. The dashboard itself lives
          in a separate YAML file but its registration is in configuration.yaml.
        - Dashboards inlined directly in ``configuration.yaml`` under the ``lovelace:``
          key (legacy single-dashboard mode).
        For either YAML case, edit the dashboard's .yaml file directly.
        ``ha_config_set_yaml`` can update the ``lovelace:`` registration
        entry in configuration.yaml but does NOT touch the dashboard
        body in the referenced .yaml file.
        """
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
                        context={"view_path": view_path},
                    )
                )
            (
                url_path,
                pre_resolved_from,
                pre_fetched_dashboards,
            ) = await self._resolve_set_dashboard_url_path(url_path)

            # Validate mutual exclusivity of config and python_transform
            if config is not None and python_transform is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Cannot use both config and python_transform simultaneously",
                        suggestions=[
                            "Use only ONE of: config or python_transform",
                            "config: Full replacement",
                            "python_transform: Python-based edits (recommended)",
                        ],
                        context={"action": "set", "url_path": url_path},
                    )
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
        except Exception as e:
            error = exception_to_structured_error(
                e,
                context={"action": "set", "url_path": url_path},
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
        self, url_path: str
    ) -> tuple[str, str | None, list[dict[str, Any]] | None]:
        """Validate + canonicalize ``url_path`` for ``ha_config_set_dashboard``.

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
            context={"action": "set"},
        )
        # Handle "default" as alias for the default dashboard
        # (matches ha_config_get_dashboard behavior)
        if url_path == "default":
            url_path = "lovelace"

        # Pre-resolve internal dashboard ID to url_path form before the
        # hyphen check below, so callers may pass either form. Only fires
        # when the identifier looks like an internal id (no hyphen, not
        # the built-in "lovelace") and matches a known dashboard.
        #
        # Caveat: if a caller passes a hyphenless identifier intending
        # to *create* a new dashboard, but it happens to match an
        # existing dashboard's id, the rewrite silently re-targets the
        # operation onto that existing dashboard. Pre-PR they'd have
        # hit the hyphen-validation error and known their input was
        # invalid; now the create-vs-update distinction depends on
        # whether the registry happens to contain a matching id.
        # We log the rewrite and surface the original identifier as
        # ``resolved_from`` on the success response so callers can
        # detect this redirect.
        pre_resolved_from: str | None = None
        # When the pre-resolver fires and finds a match, ``_resolve_dashboard``
        # has already fetched ``lovelace/dashboards/list``. Capture that list
        # so the existence-check site below can reuse it instead of paying
        # a second round-trip.
        pre_fetched_dashboards: list[dict[str, Any]] | None = None
        if "-" not in url_path and url_path != "lovelace":
            resolved, dashboards = await _resolve_dashboard(self._client, url_path)
            if resolved is not None and resolved["url_path"]:
                original_url_path = url_path
                url_path = resolved["url_path"]
                pre_resolved_from = original_url_path
                pre_fetched_dashboards = dashboards
                logger.info(
                    "ha_config_set_dashboard pre-resolver mapped %r -> %r",
                    original_url_path,
                    url_path,
                )

        # Validate url_path contains hyphen for new dashboards
        # The built-in "lovelace" dashboard is exempt since it already exists
        if "-" not in url_path and url_path != "lovelace":
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "url_path must contain a hyphen (-)",
                    suggestions=[
                        f"Try '{url_path.replace('_', '-')}' instead",
                        "Use format like 'my-dashboard' or 'mobile-view'",
                        "Use 'lovelace' or 'default' to edit the default dashboard",
                    ],
                    context={"action": "set", "url_path": url_path},
                )
            )

        return url_path, pre_resolved_from, pre_fetched_dashboards

    async def _fetch_and_verify_dashboard_hash(
        self, url_path: str, config_hash: str
    ) -> dict[str, Any]:
        """Fetch current dashboard config and verify ``config_hash`` (optimistic locking).

        Re-wraps the shared fetch helper's generic error with
        python_transform-specific UX suggestions, and raises on a hash
        mismatch (concurrent edit since the caller's last read).
        """
        try:
            current_config, current_hash = await _get_dashboard_config_internal(
                self._client, url_path
            )
        except ToolError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Dashboard not found or inaccessible: {extract_tool_error_message(e)}",
                    suggestions=[
                        "python_transform requires an existing dashboard",
                        "Use 'config' parameter to create a new dashboard",
                        "Verify dashboard exists with ha_config_get_dashboard(list_only=True)",
                    ],
                    context={"action": "python_transform", "url_path": url_path},
                )
            )

        if current_hash != config_hash:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Dashboard modified since last read (conflict)",
                    suggestions=[
                        "Call ha_config_get_dashboard() again",
                        "Use the fresh config_hash from that response",
                    ],
                    context={"action": "python_transform", "url_path": url_path},
                )
            )
        return current_config

    @staticmethod
    def _apply_dashboard_python_transform(
        url_path: str, python_transform: str, current_config: dict[str, Any]
    ) -> dict[str, Any]:
        """Run ``python_transform`` against ``current_config`` in the sandbox."""
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
        return transformed_config

    async def _save_dashboard_python_transform(
        self, url_path: str, transformed_config: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None, str | None]:
        """Save transformed config and best-effort reload its authoritative form."""
        save_data: dict[str, Any] = {
            "type": "lovelace/config/save",
            "config": transformed_config,
        }
        if url_path:
            save_data["url_path"] = url_path

        save_result = await self._client.send_websocket_message(save_data)

        if isinstance(save_result, dict) and not save_result.get("success", True):
            error_msg = save_result.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to save transformed config: {error_msg}",
                    suggestions=[
                        "Expression may have produced invalid dashboard structure",
                        "Verify config format is valid Lovelace JSON",
                    ],
                    context={"action": "python_transform", "url_path": url_path},
                )
            )

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
                "Could not reload dashboard %s after Python transform: %s",
                url_path,
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
        (
            post_save_config,
            new_config_hash,
            post_save_warning,
        ) = await self._save_dashboard_python_transform(url_path, transformed_config)

        transform_result: dict[str, Any] = {
            "success": True,
            "action": "python_transform",
            "url_path": url_path,
            "config_hash": new_config_hash,
            "write_committed": True,
            "post_write_verified": post_save_warning is None,
            "python_expression": python_transform,
            "message": f"Dashboard {url_path} updated via Python transform",
        }
        if pre_resolved_from is not None:
            transform_result["resolved_from"] = pre_resolved_from
        if post_save_warning is not None:
            transform_result["warnings"] = [post_save_warning]
        if post_save_warning is None:
            _attach_dashboard_render_paths(transform_result, url_path, post_save_config)
        _attach_dashboard_skill(transform_result, MandatoryBPS)
        return await _maybe_attach_screenshot(
            transform_result,
            url_path,
            return_screenshot,
            client=self._client,
            config=post_save_config,
            options=screenshot_options,
        )

    async def _lookup_existing_dashboards(
        self, url_path: str, pre_fetched_dashboards: list[dict[str, Any]] | None
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Resolve whether ``url_path`` already exists, reusing a pre-fetched list when available."""
        if pre_fetched_dashboards is not None:
            existing_dashboards = pre_fetched_dashboards
        else:
            existing_dashboards = await fetch_dashboards_list(self._client) or []
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

        Returns ``(dashboard_id, metadata_updated, hint)``.
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
            # is not possible without a storage ID — config update still proceeds.
            hint = (
                "Metadata fields were provided but could not be applied: "
                "dashboard has no storage ID (likely the built-in default dashboard). "
                "Config changes were still saved."
            )
            return dashboard_id, False, hint
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

        Returns ``(dashboard_exists, dashboard_id, metadata_updated, hint)`` —
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

        dashboard_id, metadata_updated, hint = await self._update_dashboard_metadata(
            url_path,
            existing_dashboards,
            title=title,
            icon=icon,
            require_admin=require_admin,
            show_in_sidebar=show_in_sidebar,
        )
        return dashboard_exists, dashboard_id, metadata_updated, hint

    async def _check_dashboard_replace_hash(
        self, url_path: str, config_hash: str | None
    ) -> str | None:
        """Optionally validate config_hash and warn on large full-config replacement.

        Tolerates fetch failures — full replacement still proceeds even if the
        pre-read can't load the current state (force-replace path).
        """
        try:
            existing_config, existing_hash = await _get_dashboard_config_internal(
                self._client, url_path
            )
        except ToolError:
            # Pre-read failure is non-fatal on the force-replace path: skip
            # the optimistic-lock check and large-config warning and proceed
            # with the replacement.
            return None

        if not isinstance(existing_config, dict):
            return None

        existing_config_size = len(json.dumps(existing_config))
        if config_hash is not None and existing_hash != config_hash:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Dashboard modified since last read (conflict)",
                    suggestions=[
                        "Call ha_config_get_dashboard() again",
                        "Use the fresh config_hash, or omit config_hash to force replace",
                    ],
                    context={"action": "set", "url_path": url_path},
                )
            )

        if existing_config_size >= 10000:
            return (
                f"Replaced large config ({existing_config_size:,} bytes). "
                "Consider python_transform for targeted edits."
            )
        return None

    async def _save_dashboard_config(
        self, url_path: str, config_dict: dict[str, Any]
    ) -> None:
        """Save ``config_dict`` as the full dashboard config replacement."""
        config_save_data: dict[str, Any] = {
            "type": "lovelace/config/save",
            "config": config_dict,
        }
        if url_path:
            config_save_data["url_path"] = url_path
        save_result = await self._client.send_websocket_message(config_save_data)

        if isinstance(save_result, dict) and not save_result.get("success", True):
            error_msg = save_result.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to save dashboard config: {error_msg}",
                    suggestions=[
                        "Verify config format is valid Lovelace JSON",
                        "Check that you have admin permissions",
                        "Ensure all entity IDs in config exist",
                    ],
                    context={"action": "set", "url_path": url_path},
                )
            )

    async def _apply_dashboard_config(
        self,
        url_path: str,
        config: dict[str, Any] | str,
        config_hash: str | None,
        dashboard_exists: bool,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        """Parse + validate ``config`` and save it as a full replacement.

        Returns ``(config_updated, hint, saved_config)``.
        """
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

        hint: str | None = None
        if dashboard_exists:
            hint = await self._check_dashboard_replace_hash(url_path, config_hash)

        await self._save_dashboard_config(url_path, config_dict)
        return True, hint, config_dict

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
        (
            dashboard_exists,
            dashboard_id,
            metadata_updated,
            hint,
        ) = await self._ensure_dashboard_exists(
            url_path,
            title=title,
            icon=icon,
            require_admin=require_admin,
            show_in_sidebar=show_in_sidebar,
            pre_fetched_dashboards=pre_fetched_dashboards,
        )

        config_updated = False
        render_config: dict[str, Any] | None = None
        if config is not None:
            (
                config_updated,
                config_hint,
                render_config,
            ) = await self._apply_dashboard_config(
                url_path, config, config_hash, dashboard_exists
            )
            if config_hint:
                hint = config_hint

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

        if hint:
            result_dict["hint"] = hint
        if pre_resolved_from is not None:
            # Caller passed an internal id; pre-resolver mapped it to
            # the canonical url_path. Surface the original so a caller
            # who *intended* to create a new dashboard can detect that
            # an existing dashboard was updated instead.
            result_dict["resolved_from"] = pre_resolved_from

        render_config = await _attach_dashboard_render_paths_after_write(
            self._client, result_dict, url_path, render_config
        )
        _attach_dashboard_skill(result_dict, MandatoryBPS)
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
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Delete Dashboard",
        },
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

        Accepts either the URL path or the internal dashboard ID. HA internal IDs
        may differ from url_path (e.g. hyphens → underscores); the tool resolves
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
            if resolved is None:
                available_ids = [
                    d.get("url_path")
                    for d in (dashboards or [])[:10]
                    if d.get("url_path")
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
        except Exception as e:
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
