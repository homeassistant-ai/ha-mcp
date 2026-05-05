"""
Configuration management tools for Home Assistant Lovelace dashboards.

This module provides tools for managing dashboard metadata and content.
"""

import json
import logging
import re
from typing import Annotated, Any, cast, overload

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import ErrorCode, create_error_response, create_resource_not_found_error
from ..utils.config_hash import compute_config_hash
from ..utils.python_sandbox import (
    PythonSandboxError,
    get_security_documentation,
    safe_execute,
)
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)


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
    # Re-fetch current config
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


def _find_cards_in_config(
    config: dict[str, Any],
    entity_id: str | None = None,
    card_type: str | None = None,
    heading: str | None = None,
) -> list[dict[str, Any]]:
    """
    Find cards, badges, and header cards in a dashboard config matching the search criteria.

    Returns a list of matches with location info and card/badge/header config.
    Searches cards (in sections and flat views), view-level badges, and
    sections-view header cards (views[n].header.card).
    """
    matches: list[dict[str, Any]] = []

    if "strategy" in config:
        return []  # Strategy dashboards don't have explicit cards

    views = config.get("views", [])
    for view_idx, view in enumerate(views):
        if not isinstance(view, dict):
            continue

        # Search view-level badges when filtering by entity_id or card_type="badge"
        if (
            entity_id is not None
            and heading is None
            and (card_type is None or card_type == "badge")
        ):
            badges = view.get("badges", [])
            for badge_idx, badge in enumerate(badges):
                if _badge_matches(badge, entity_id):
                    badge_config = (
                        badge if isinstance(badge, dict) else {"entity": badge}
                    )
                    matches.append(
                        {
                            "view_index": view_idx,
                            "section_index": None,
                            "card_index": None,
                            "badge_index": badge_idx,
                            "jq_path": f".views[{view_idx}].badges[{badge_idx}]",
                            "card_type": "badge",
                            "card_config": badge_config,
                        }
                    )

        # Search sections-view header card (views[n].header.card)
        # The header accepts a card (typically Markdown) that can contain entity refs
        header = view.get("header", {})
        if isinstance(header, dict):
            header_card = header.get("card")
            if isinstance(header_card, dict) and _card_matches(
                header_card, entity_id, card_type, heading
            ):
                matches.append(
                    {
                        "view_index": view_idx,
                        "section_index": None,
                        "card_index": None,
                        "jq_path": f".views[{view_idx}].header.card",
                        "card_type": header_card.get("type"),
                        "card_config": header_card,
                    }
                )

        view_type = view.get("type", "masonry")

        if view_type == "sections":
            # Sections-based view
            sections = view.get("sections", [])
            for section_idx, section in enumerate(sections):
                if not isinstance(section, dict):
                    continue
                cards = section.get("cards", [])
                for card_idx, card in enumerate(cards):
                    if not isinstance(card, dict):
                        continue
                    if _card_matches(card, entity_id, card_type, heading):
                        matches.append(
                            {
                                "view_index": view_idx,
                                "section_index": section_idx,
                                "card_index": card_idx,
                                "jq_path": f".views[{view_idx}].sections[{section_idx}].cards[{card_idx}]",
                                "card_type": card.get("type"),
                                "card_config": card,
                            }
                        )
        else:
            # Flat view (masonry, panel, sidebar)
            cards = view.get("cards", [])
            for card_idx, card in enumerate(cards):
                if not isinstance(card, dict):
                    continue
                if _card_matches(card, entity_id, card_type, heading):
                    matches.append(
                        {
                            "view_index": view_idx,
                            "section_index": None,
                            "card_index": card_idx,
                            "jq_path": f".views[{view_idx}].cards[{card_idx}]",
                            "card_type": card.get("type"),
                            "card_config": card,
                        }
                    )

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


async def _resolve_dashboard(client: Any, identifier: str) -> dict[str, str] | None:
    """Resolve a dashboard identifier (url_path or internal id) to both forms.

    Calls ``lovelace/dashboards/list`` and returns
    ``{"url_path": ..., "id": ...}`` when the identifier matches either field
    on a registry entry that has both fields populated; otherwise returns
    ``None``. Always pays the round-trip when called.

    Two call sites:
    - **Lazy fallback** (``_lazy_resolve_and_retry``): only invoked after
      ``lovelace/config`` rejected the identifier with
      ``_LAZY_RESOLVE_TRIGGER`` — the round-trip is gated by the caller.
    - **Eager pre-resolve** (``ha_config_set_dashboard``): invoked before
      hyphen validation so callers may pass either form; gated on a
      cheap heuristic ("no hyphen, not 'lovelace'") rather than an error
      from HA.
    """
    result = await client.send_websocket_message({"type": "lovelace/dashboards/list"})
    if isinstance(result, dict) and "result" in result:
        dashboards = result["result"]
    elif isinstance(result, list):
        dashboards = result
    else:
        # Neither dict-with-result nor list — either HA returned an error
        # envelope (unknown shape) or the response format changed.
        # Surface a warning so the next response-shape change isn't a
        # silent "always no match" regression.
        logger.warning(
            "lovelace/dashboards/list returned an unexpected shape (type=%s); "
            "treating as no-match",
            type(result).__name__,
        )
        return None

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
            return {"url_path": url_path, "id": entry_id}
    return None


@overload
async def _lazy_resolve_and_retry(
    client: Any,
    url_path: str,
    ws_data: dict[str, Any],
    response: Any,
) -> tuple[str, Any]: ...


@overload
async def _lazy_resolve_and_retry(
    client: Any,
    url_path: None,
    ws_data: dict[str, Any],
    response: Any,
) -> tuple[None, Any]: ...


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
        resolved = await _resolve_dashboard(client, url_path)
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


def register_config_dashboard_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant dashboard configuration tools."""

    @mcp.tool(
        tags={"Dashboards"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Dashboard",
        },
    )
    @log_tool_usage
    async def ha_config_get_dashboard(
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
                "object in results (increases output size). Does not affect whether the full "
                "dashboard config is returned — search mode always returns matches only, "
                "not the full dashboard. Ignored outside search mode."
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        Get dashboard info - list all dashboards, get config, or search for cards.

        MODE 1 — List: list_only=True
          Lists all storage-mode dashboards with metadata (url_path, title, icon).

        MODE 2 — Search: any of entity_id / card_type / heading provided
          Finds cards, badges, and header cards matching the criteria.
          Returns matches with jq_path for use with ha_config_set_dashboard(python_transform=...).
          Multiple criteria are AND-ed. Always fetches fresh config (force=True).
          Strategy dashboards are not searchable (no explicit cards).

        MODE 3 — Get: Active when list_only=False and no search parameters are provided.
          Returns the full Lovelace dashboard config, defaulting to the
          main dashboard if url_path is omitted.

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
               python_transform=f'config{find["matches"][0]["jq_path"]}["icon"] = "mdi:lamp"'
           )

        Note: YAML-mode dashboards (defined in configuration.yaml) are not included in list.
        """
        search_mode = (
            entity_id is not None or card_type is not None or heading is not None
        )
        try:
            # List mode
            if list_only:
                result = await client.send_websocket_message(
                    {"type": "lovelace/dashboards/list"}
                )
                if isinstance(result, dict) and "result" in result:
                    dashboards = result["result"]
                elif isinstance(result, list):
                    dashboards = result
                else:
                    dashboards = []

                return {
                    "success": True,
                    "action": "list",
                    "dashboards": dashboards,
                    "count": len(dashboards),
                }

            # Search mode — find cards, badges, or header cards
            if search_mode:
                get_data: dict[str, Any] = {"type": "lovelace/config", "force": True}
                effective_url_path: str | None = (
                    url_path if url_path and url_path != "default" else None
                )
                if effective_url_path is not None:
                    get_data["url_path"] = effective_url_path

                response = await client.send_websocket_message(get_data)

                # Lazy resolver fallback: same gate as get-mode. If the
                # caller passed an internal id where url_path is expected,
                # HA rejects with the trigger substring; resolve and retry
                # once. (set_dashboard handles this via an eager pre-resolver
                # before the hyphen check, so it has no equivalent fallback
                # here.)
                search_resolved_from: str | None = None
                if effective_url_path is not None:
                    new_url_path, response = await _lazy_resolve_and_retry(
                        client, effective_url_path, get_data, response
                    )
                    if new_url_path != effective_url_path:
                        # Surface the original caller-passed identifier so
                        # the caller can see their input was canonicalized.
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

                config = (
                    response.get("result") if isinstance(response, dict) else response
                )
                if not isinstance(config, dict):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            "Dashboard config is empty or invalid",
                            suggestions=[
                                "Initialize dashboard with ha_config_set_dashboard"
                            ],
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

                matches = _find_cards_in_config(config, entity_id, card_type, heading)

                if not include_config:
                    for match in matches:
                        del match["card_config"]

                config_hash: str | None = compute_config_hash(config)

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
                    "hint": (
                        "Use jq_path with ha_config_set_dashboard(python_transform=...) "
                        "for targeted updates"
                        if matches
                        else "No matches found. Try broader search criteria."
                    ),
                }
                if search_resolved_from is not None:
                    search_result["resolved_from"] = search_resolved_from
                return search_result

            # Get mode - build WebSocket message
            data: dict[str, Any] = {"type": "lovelace/config", "force": force_reload}
            # Handle "default" as special value for default dashboard
            if url_path and url_path != "default":
                data["url_path"] = url_path

            response = await client.send_websocket_message(data)

            # Lazy resolver fallback: if HA rejects the identifier as unknown,
            # resolve it via lovelace/dashboards/list and retry once. The
            # round-trip is only paid when the caller passed an internal
            # dashboard id (or another non-url_path form) HA does not accept.
            original_url_path = url_path
            url_path, response = await _lazy_resolve_and_retry(
                client, url_path, data, response
            )

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

            # Compute hash for optimistic locking in subsequent operations
            config_hash = (
                compute_config_hash(config) if isinstance(config, dict) else None
            )

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

            return get_result
        except ToolError:
            raise
        except Exception as e:
            if search_mode:
                logger.error(
                    f"Error finding card in dashboard: url_path={url_path}, "
                    f"entity_id={entity_id}, card_type={card_type}, heading={heading}, "
                    f"error={e}",
                    exc_info=True,
                )
                suggestions = [
                    "Check HA connection",
                    "Verify dashboard with ha_config_get_dashboard(list_only=True)",
                ]
                context: dict[str, Any] = {
                    "action": "find_card",
                    "url_path": url_path,
                    "entity_id": entity_id,
                    "card_type": card_type,
                    "heading": heading,
                }
            else:
                logger.error(f"Error getting dashboard: {e}", exc_info=True)
                suggestions = [
                    "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                    "Check if you have permission to access this dashboard",
                    "Use url_path='default' for default dashboard",
                ]
                context = {
                    "action": "get" if not list_only else "list",
                    "url_path": url_path,
                }
            exception_to_structured_error(
                e,
                context=context,
                suggestions=suggestions,
            )

    @mcp.tool(
        tags={"Dashboards"},
        annotations={"destructiveHint": True, "title": "Create or Update Dashboard"},
    )
    @log_tool_usage
    async def ha_config_set_dashboard(
        url_path: Annotated[
            str,
            Field(
                description="Dashboard URL path (e.g., 'my-dashboard'). "
                "Use 'default' or 'lovelace' for the default dashboard. "
                "New dashboards must use a hyphenated path."
            ),
        ],
        config: Annotated[
            str | dict[str, Any] | None,
            Field(
                description="Dashboard configuration with views and cards. "
                "Can be dict or JSON string. "
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
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant dashboard.

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

        MODERN DASHBOARD BEST PRACTICES (2024+):
        - Use "sections" view type (default) with grid-based layouts
        - Use "tile" cards as primary card type (replaces legacy entity/light/climate cards)
        - Use "grid" cards for multi-column layouts within sections
        - Create multiple views with navigation paths (avoid single-view endless scrolling)
        - Use "area" cards with navigation for hierarchical organization

        DISCOVERING ENTITY IDs FOR DASHBOARDS:
        Do NOT guess entity IDs - use these tools to find exact entity IDs:
        1. ha_get_overview(include_entity_id=True) - Get all entities organized by domain/area
        2. ha_search_entities(query, domain_filter, area_filter) - Find specific entities
        3. ha_deep_search(query) - Comprehensive search across entities, areas, automations

        If unsure about entity IDs, ALWAYS use one of these tools first.

        DASHBOARD DOCUMENTATION (via MCP skills):
        - skill://home-assistant-best-practices/references/dashboard-guide.md — comprehensive guide
        - skill://home-assistant-best-practices/references/dashboard-cards.md — card types list
        - ha_get_skill_home_assistant_best_practices — guidance on card types and configuration

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
        """
        try:
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
            if "-" not in url_path and url_path != "lovelace":
                resolved = await _resolve_dashboard(client, url_path)
                if resolved is not None and resolved["url_path"]:
                    original_url_path = url_path
                    url_path = resolved["url_path"]
                    pre_resolved_from = original_url_path
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

            # Handle python_transform mode
            if python_transform is not None:
                # config_hash is REQUIRED
                if config_hash is None:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "config_hash is required for python_transform",
                            suggestions=[
                                "Call ha_config_get_dashboard() first",
                                "Use the config_hash from that response",
                            ],
                            context={
                                "action": "python_transform",
                                "url_path": url_path,
                            },
                        )
                    )

                # Fetch current dashboard config
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
                            f"Dashboard not found or inaccessible: {error_msg}",
                            suggestions=[
                                "python_transform requires an existing dashboard",
                                "Use 'config' parameter to create a new dashboard",
                                "Verify dashboard exists with ha_config_get_dashboard(list_only=True)",
                            ],
                            context={
                                "action": "python_transform",
                                "url_path": url_path,
                            },
                        )
                    )

                current_config = (
                    response.get("result") if isinstance(response, dict) else response
                )
                if not isinstance(current_config, dict):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            "Current dashboard config is invalid",
                            suggestions=[
                                "Initialize dashboard with 'config' parameter first"
                            ],
                            context={
                                "action": "python_transform",
                                "url_path": url_path,
                            },
                        )
                    )

                # Validate config_hash for optimistic locking
                current_hash = compute_config_hash(current_config)
                if current_hash != config_hash:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            "Dashboard modified since last read (conflict)",
                            suggestions=[
                                "Call ha_config_get_dashboard() again",
                                "Use the fresh config_hash from that response",
                            ],
                            context={
                                "action": "python_transform",
                                "url_path": url_path,
                            },
                        )
                    )

                # Apply Python transformation with validation
                try:
                    transformed_config = safe_execute(python_transform, current_config)
                except PythonSandboxError as e:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            str(e),
                            suggestions=[
                                "Check expression syntax",
                                "Ensure only allowed operations are used",
                                "See tool description for allowed operations",
                                f"Expression: {python_transform[:100]}...",
                            ],
                            context={
                                "action": "python_transform",
                                "url_path": url_path,
                            },
                        )
                    )

                # Save transformed config
                save_data: dict[str, Any] = {
                    "type": "lovelace/config/save",
                    "config": transformed_config,
                }
                if url_path:
                    save_data["url_path"] = url_path

                save_result = await client.send_websocket_message(save_data)

                if isinstance(save_result, dict) and not save_result.get(
                    "success", True
                ):
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
                            context={
                                "action": "python_transform",
                                "url_path": url_path,
                            },
                        )
                    )

                # Compute new hash for potential chaining
                new_config_hash = compute_config_hash(transformed_config)

                transform_result: dict[str, Any] = {
                    "success": True,
                    "action": "python_transform",
                    "url_path": url_path,
                    "config_hash": new_config_hash,
                    "python_expression": python_transform,
                    "message": f"Dashboard {url_path} updated via Python transform",
                }
                if pre_resolved_from is not None:
                    transform_result["resolved_from"] = pre_resolved_from
                return transform_result

            # Check if dashboard exists
            result = await client.send_websocket_message(
                {"type": "lovelace/dashboards/list"}
            )
            if isinstance(result, dict) and "result" in result:
                existing_dashboards = result["result"]
            elif isinstance(result, list):
                existing_dashboards = result
            else:
                existing_dashboards = []
            dashboard_exists = any(
                d.get("url_path") == url_path for d in existing_dashboards
            )

            # The built-in default dashboard ("lovelace") is always present
            # but isn't listed by lovelace/dashboards/list on fresh installs
            if url_path == "lovelace":
                dashboard_exists = True

            # If dashboard doesn't exist, create it
            dashboard_id = None
            metadata_updated = False
            hint = None
            if not dashboard_exists:
                # Use provided title or generate from url_path
                dashboard_title = title or url_path.replace("-", " ").title()

                # Build create message
                create_data: dict[str, Any] = {
                    "type": "lovelace/dashboards/create",
                    "url_path": url_path,
                    "title": dashboard_title,
                    "require_admin": require_admin
                    if require_admin is not None
                    else False,
                    "show_in_sidebar": show_in_sidebar
                    if show_in_sidebar is not None
                    else True,
                }
                if icon:
                    create_data["icon"] = icon
                create_result = await client.send_websocket_message(create_data)

                # Check if dashboard creation was successful
                if isinstance(create_result, dict) and not create_result.get(
                    "success", True
                ):
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

                # Extract dashboard ID from create response
                if isinstance(create_result, dict) and "result" in create_result:
                    dashboard_info = create_result["result"]
                    dashboard_id = dashboard_info.get("id")
                elif isinstance(create_result, dict):
                    dashboard_id = create_result.get("id")
            else:
                # If dashboard already exists, get its ID from the list
                for dashboard in existing_dashboards:
                    if dashboard.get("url_path") == url_path:
                        dashboard_id = dashboard.get("id")
                        break

                # Update metadata for existing dashboard if any metadata params provided
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
                    meta_update: dict[str, Any] = {
                        "type": "lovelace/dashboards/update",
                        "dashboard_id": dashboard_id,
                        **metadata_update_fields,
                    }
                    meta_result = await client.send_websocket_message(meta_update)
                    if isinstance(meta_result, dict) and not meta_result.get(
                        "success", True
                    ):
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
                    metadata_updated = True
                elif metadata_update_fields and dashboard_id is None:
                    # Dashboard ID not found in storage list (e.g. default lovelace on
                    # fresh installs). Metadata update via lovelace/dashboards/update
                    # is not possible without a storage ID — config update still proceeds.
                    metadata_updated = False
                    hint = (
                        "Metadata fields were provided but could not be applied: "
                        "dashboard has no storage ID (likely the built-in default dashboard). "
                        "Config changes were still saved."
                    )

            # Set config if provided
            config_updated = False
            existing_config_size = 0

            if config is not None:
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

                # For existing dashboards, optionally validate config_hash and warn on large replacement
                if dashboard_exists:
                    # Fetch current config for validation/comparison
                    get_data = {
                        "type": "lovelace/config",
                        "force": True,
                    }
                    if url_path:
                        get_data["url_path"] = url_path
                    current_response = await client.send_websocket_message(get_data)
                    current_config = (
                        current_response.get("result")
                        if isinstance(current_response, dict)
                        else current_response
                    )

                    if isinstance(current_config, dict):
                        existing_config_size = len(json.dumps(current_config))

                        # Optional config_hash validation for full replacement
                        if config_hash is not None:
                            current_hash = compute_config_hash(current_config)
                            if current_hash != config_hash:
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

                        # Soft warning for large config full replacement (10KB ≈ 2-3k tokens)
                        if existing_config_size >= 10000:
                            hint = (
                                f"Replaced large config ({existing_config_size:,} bytes). "
                                "Consider python_transform for targeted edits."
                            )

                # Build save config message
                config_save_data: dict[str, Any] = {
                    "type": "lovelace/config/save",
                    "config": config_dict,
                }
                if url_path:
                    config_save_data["url_path"] = url_path
                save_result = await client.send_websocket_message(config_save_data)

                # Check if save failed
                if isinstance(save_result, dict) and not save_result.get(
                    "success", True
                ):
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

                config_updated = True

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

            return result_dict

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error setting dashboard: {e}")
            exception_to_structured_error(
                e,
                context={"action": "set", "url_path": url_path},
                suggestions=[
                    "Ensure url_path is unique (not already in use for different dashboard type)",
                    "New dashboards require a hyphenated url_path",
                    "Check that you have admin permissions",
                    "Verify config format is valid Lovelace JSON",
                ],
            )

    @mcp.tool(
        tags={"Dashboards"},
        annotations={"destructiveHint": True, "title": "Delete Dashboard"},
    )
    @log_tool_usage
    async def ha_config_delete_dashboard(
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
            resolved = await _resolve_dashboard(client, url_path)
            if resolved is None:
                raise_tool_error(
                    create_resource_not_found_error(
                        "Dashboard",
                        url_path,
                        details=(
                            f"No dashboard found with URL path or internal ID '{url_path}'. "
                            "Use ha_config_get_dashboard(list_only=True) to see available dashboards."
                        ),
                    )
                )
            resolved_id = resolved["id"]

            response = await client.send_websocket_message(
                {"type": "lovelace/dashboards/delete", "dashboard_id": resolved_id}
            )

            # Check response for error indication
            if isinstance(response, dict) and not response.get("success", True):
                error_msg = response.get("error", {})
                if isinstance(error_msg, dict):
                    error_str = error_msg.get("message", str(error_msg))
                else:
                    error_str = str(error_msg)

                logger.error(f"Error deleting dashboard: {error_str}")

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
            logger.error(f"Error deleting dashboard: {e}")
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

    # =========================================================================
    # Dashboard Resource Management Tools
    # =========================================================================
    # Resource tools have been moved to tools_resources.py for better organization.
    # Available tools:
    # - ha_config_list_dashboard_resources: List all resources
    # - ha_config_set_dashboard_resource: Create/update resources (inline code or URL)
    # - ha_config_delete_dashboard_resource: Delete resources
    # =========================================================================
