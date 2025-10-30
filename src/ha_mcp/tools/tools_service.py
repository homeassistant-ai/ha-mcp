"""
Service call and device operation tools for Home Assistant MCP server.

This module provides service execution and WebSocket-enabled operation monitoring tools.
"""

from collections import OrderedDict
import json
import time
from typing import Annotated, Any, cast

from pydantic import Field

from ..config import get_global_settings
from ..utils.fuzzy_search import create_fuzzy_searcher
from .util_helpers import parse_json_param


_QUICK_ACTION_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_QUICK_ACTION_CACHE_LIMIT = 32
_DEFAULT_MIN_CONFIDENCE_PERCENT = 85.0


def _normalize_quick_action_confidence(
    raw_value: Any,
) -> tuple[float, float]:
    """Normalize min_confidence inputs into ratio and percentage values."""

    value_to_report = raw_value

    if raw_value is None:
        numeric_value = _DEFAULT_MIN_CONFIDENCE_PERCENT
    elif isinstance(raw_value, bool):
        numeric_value = float(raw_value)
    elif isinstance(raw_value, (int, float)):
        numeric_value = float(raw_value)
    elif isinstance(raw_value, str):
        stripped = raw_value.strip()
        try:
            numeric_value = float(stripped)
        except ValueError as exc:  # noqa: TRY003 - we want consistent error messaging
            raise ValueError(
                "min_confidence must be integer [0-100] or float [0-1]; "
                f"got {value_to_report!r}"
            ) from exc
    else:
        raise ValueError(
            "min_confidence must be integer [0-100] or float [0-1]; "
            f"got {value_to_report!r}"
        )

    if numeric_value < 0 or numeric_value > 100:
        raise ValueError(
            "min_confidence must be integer [0-100] or float [0-1]; "
            f"got {value_to_report!r}"
        )

    if numeric_value <= 1:
        ratio_value = numeric_value
    else:
        ratio_value = numeric_value / 100.0

    ratio_value = max(0.0, min(1.0, ratio_value))
    percent_value = ratio_value * 100.0

    return ratio_value, percent_value


def _normalize_quick_action_terms(raw_terms: Any) -> list[dict[str, Any]]:
    """Normalize search term inputs into weighted descriptors."""

    if raw_terms is None:
        raise ValueError("search_terms parameter is required")

    terms: list[Any] = []

    # Allow JSON strings that describe lists/dicts
    if isinstance(raw_terms, str):
        stripped = raw_terms.strip()
        if not stripped:
            raise ValueError("search_terms cannot be empty")

        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed_json = json.loads(stripped)
            except json.JSONDecodeError:
                terms = [stripped]
            else:
                return _normalize_quick_action_terms(parsed_json)
        else:
            cleaned = stripped.strip()
            if (cleaned.startswith("\"") and cleaned.endswith("\"")) or (
                cleaned.startswith("'") and cleaned.endswith("'")
            ):
                cleaned = cleaned[1:-1]
            terms = [cleaned.strip()]
    elif isinstance(raw_terms, dict):
        # Either {"value": "kitchen", "weight": 0.6} or {"kitchen": 0.6, "light": 0.4}
        if any(key in raw_terms for key in ("value", "term", "query")):
            terms = [raw_terms]
        else:
            terms = [
                {"value": str(term), "weight": weight}
                for term, weight in raw_terms.items()
            ]
    elif isinstance(raw_terms, list):
        terms = raw_terms
    else:
        raise ValueError(
            "search_terms must be a string, list, or dictionary describing search inputs"
        )

    normalized: list[dict[str, Any]] = []
    total_weight = 0.0

    for term in terms:
        if isinstance(term, str):
            value = term.strip()
            weight = 1.0
        elif isinstance(term, dict):
            value = (
                str(
                    term.get("value")
                    or term.get("term")
                    or term.get("query")
                    or ""
                ).strip()
            )
            weight = (
                term.get("weight")
                or term.get("importance")
                or term.get("score")
                or 1.0
            )
        else:
            # Unsupported type within list; skip
            continue

        if not value:
            continue

        try:
            weight_value = float(weight)
        except (TypeError, ValueError):
            weight_value = 0.0

        if weight_value <= 0:
            continue

        normalized.append(
            {
                "value": value,
                "original_weight": weight_value,
            }
        )
        total_weight += weight_value

    if not normalized or total_weight <= 0:
        raise ValueError(
            "At least one valid search term with a positive weight is required"
        )

    for item in normalized:
        item["weight"] = item["original_weight"] / total_weight

    return normalized


def _score_entities_for_quick_action(
    entities: list[dict[str, Any]],
    terms: list[dict[str, Any]],
    searcher: Any,
    domain_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Compute weighted fuzzy scores for entities based on provided terms."""

    if not terms or not entities:
        return []

    primary_term = max(terms, key=lambda item: item["weight"])  # Highest weight term
    primary_query = primary_term["value"].lower()

    results: list[dict[str, Any]] = []

    for entity in entities:
        entity_id = entity.get("entity_id", "")
        if not entity_id:
            continue

        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain_filter and domain != domain_filter:
            continue

        attributes = entity.get("attributes", {})
        friendly_name = attributes.get("friendly_name", entity_id)

        total_score = 0.0
        term_breakdown: list[dict[str, Any]] = []

        for term in terms:
            query_lower = term["value"].lower()
            raw_score = searcher._calculate_entity_score(
                entity_id,
                friendly_name,
                domain,
                query_lower,
            )
            normalized_score = max(0.0, min(100.0, float(raw_score)))
            contribution = normalized_score * term["weight"]
            term_breakdown.append(
                {
                    "term": term["value"],
                    "weight": round(term["weight"], 4),
                    "raw_score": round(float(raw_score), 2),
                    "normalized_score": round(normalized_score, 2),
                    "contribution": round(contribution, 2),
                }
            )
            total_score += contribution

        if total_score <= 0:
            continue

        match_type = searcher._get_match_type(
            entity_id,
            friendly_name,
            domain,
            primary_query,
        )

        results.append(
            {
                "entity_id": entity_id,
                "friendly_name": friendly_name,
                "domain": domain,
                "state": entity.get("state", "unknown"),
                "score": round(min(total_score, 100.0), 2),
                "raw_score": round(total_score, 2),
                "term_breakdown": term_breakdown,
                "match_type": match_type,
            }
        )

    results.sort(key=lambda item: item["score"], reverse=True)
    return results


def _make_quick_action_cache_key(
    domain: str,
    service: str,
    terms: list[dict[str, Any]],
    entity_domain: str | None,
    override_key: str | None = None,
) -> str | None:
    """Build a stable cache key for quick action lookups."""

    if override_key:
        return override_key.lower()

    if not terms:
        return None

    term_signature = "|".join(
        f"{term['value'].lower()}@{round(term['weight'], 4)}" for term in terms
    )
    parts = [domain.lower(), service.lower(), term_signature]
    if entity_domain:
        parts.append(entity_domain.lower())
    return "::".join(parts)


def _get_cached_quick_action(cache_key: str | None) -> dict[str, Any] | None:
    """Retrieve a cached quick action entry, refreshing its order."""

    if not cache_key:
        return None

    entry = _QUICK_ACTION_CACHE.get(cache_key)
    if entry is not None:
        _QUICK_ACTION_CACHE.move_to_end(cache_key)
    return entry


def _store_cached_quick_action(cache_key: str | None, entry: dict[str, Any]) -> None:
    """Store quick action results with simple LRU eviction."""

    if not cache_key:
        return

    _QUICK_ACTION_CACHE[cache_key] = entry
    _QUICK_ACTION_CACHE.move_to_end(cache_key)

    while len(_QUICK_ACTION_CACHE) > _QUICK_ACTION_CACHE_LIMIT:
        _QUICK_ACTION_CACHE.popitem(last=False)


def register_service_tools(mcp, client, device_tools, **kwargs):
    """Register service call and operation monitoring tools with the MCP server."""

    @mcp.tool
    async def ha_call_service(
        domain: str,
        service: str,
        entity_id: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Execute Home Assistant services with comprehensive validation and examples.

        This is the universal tool for controlling all Home Assistant entities and executing automations.

        **Common Usage Examples:**

        **Light Control:**
        ```python
        # Turn on light
        ha_call_service("light", "turn_on", entity_id="light.living_room")

        # Turn on with brightness and color
        ha_call_service("light", "turn_on", entity_id="light.bedroom",
                      data={"brightness_pct": 75, "color_temp_kelvin": 2700})

        # Turn off all lights
        ha_call_service("light", "turn_off")
        ```

        **Climate Control:**
        ```python
        # Set temperature
        ha_call_service("climate", "set_temperature",
                      entity_id="climate.thermostat", data={"temperature": 22})

        # Change mode
        ha_call_service("climate", "set_hvac_mode",
                      entity_id="climate.living_room", data={"hvac_mode": "heat"})
        ```

        **Automation Control:**
        ```python
        # Trigger automation (replaces ha_trigger_automation)
        ha_call_service("automation", "trigger", entity_id="automation.morning_routine")

        # Turn automation on/off
        ha_call_service("automation", "turn_off", entity_id="automation.night_mode")
        ha_call_service("automation", "turn_on", entity_id="automation.security_check")
        ```

        **Scene Activation:**
        ```python
        # Activate scene
        ha_call_service("scene", "turn_on", entity_id="scene.movie_night")
        ha_call_service("scene", "turn_on", entity_id="scene.bedtime")
        ```

        **Input Helpers:**
        ```python
        # Set input number
        ha_call_service("input_number", "set_value",
                      entity_id="input_number.temp_offset", data={"value": 2.5})

        # Toggle input boolean
        ha_call_service("input_boolean", "toggle", entity_id="input_boolean.guest_mode")

        # Set input text
        ha_call_service("input_text", "set_value",
                      entity_id="input_text.status", data={"value": "Away"})
        ```

        **Universal Controls (works with any entity):**
        ```python
        # Universal toggle
        ha_call_service("homeassistant", "toggle", entity_id="switch.porch_light")

        # Universal turn on/off
        ha_call_service("homeassistant", "turn_on", entity_id="media_player.spotify")
        ha_call_service("homeassistant", "turn_off", entity_id="fan.ceiling_fan")
        ```

        **Script Execution:**
        ```python
        # Run script
        ha_call_service("script", "turn_on", entity_id="script.bedtime_routine")
        ha_call_service("script", "good_night_sequence")
        ```

        **Media Player Control:**
        ```python
        # Volume control
        ha_call_service("media_player", "volume_set",
                      entity_id="media_player.living_room", data={"volume_level": 0.5})

        # Play media
        ha_call_service("media_player", "play_media",
                      entity_id="media_player.spotify",
                      data={"media_content_type": "music", "media_content_id": "spotify:playlist:123"})
        ```

        **Cover Control:**
        ```python
        # Open/close covers
        ha_call_service("cover", "open_cover", entity_id="cover.garage_door")
        ha_call_service("cover", "close_cover", entity_id="cover.living_room_blinds")

        # Set position
        ha_call_service("cover", "set_cover_position",
                      entity_id="cover.bedroom_curtains", data={"position": 50})
        ```

        **Parameter Guidelines:**
        - **entity_id**: Optional for services that affect all entities of a domain
        - **data**: Service-specific parameters (brightness, temperature, volume, etc.)
        - Use ha_get_state() first to check current values and supported features
        - Use ha_get_domain_docs() for detailed service documentation
        """
        try:
            # Parse JSON data if provided as string
            try:
                parsed_data = parse_json_param(data, "data")
            except ValueError as e:
                return {
                    "success": False,
                    "error": f"Invalid data parameter: {e}",
                    "provided_data_type": type(data).__name__,
                }

            # Ensure service_data is a dict
            service_data: dict[str, Any] = {}
            if parsed_data is not None:
                if isinstance(parsed_data, dict):
                    service_data = parsed_data
                else:
                    return {
                        "success": False,
                        "error": "Data parameter must be a JSON object",
                        "provided_type": type(parsed_data).__name__,
                    }

            if entity_id:
                service_data["entity_id"] = entity_id
            result = await client.call_service(domain, service, service_data)

            return {
                "success": True,
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
                "parameters": data,
                "result": result,
                "message": f"Successfully executed {domain}.{service}",
            }
        except Exception as error:
            return {
                "success": False,
                "error": str(error),
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
                "suggestions": [
                    f"Verify {entity_id} exists using ha_get_state()",
                    f"Check available services for {domain} domain using ha_get_domain_docs()",
                    f"For automation: ha_call_service('automation', 'trigger', entity_id='{entity_id}')",
                    f"For universal control: ha_call_service('homeassistant', 'toggle', entity_id='{entity_id}')",
                    "Use ha_search_entities() to find correct entity IDs",
                ],
                "examples": {
                    "automation_trigger": f"ha_call_service('automation', 'trigger', entity_id='{entity_id}')",
                    "universal_toggle": f"ha_call_service('homeassistant', 'toggle', entity_id='{entity_id}')",
                    "light_control": "ha_call_service('light', 'turn_on', entity_id='light.bedroom', data={'brightness_pct': 75})",
                },
            }

    @mcp.tool
    async def ha_quick_service_action(
        domain: str,
        service: str,
        search_terms: Annotated[
            Any,
            Field(
                description=(
                    "Search descriptors for the target entity. Accepts a string, a list of "
                    "strings, or a list/dictionary with weighted terms."
                )
            ),
        ],
        data: str | dict[str, Any] | None = None,
        min_confidence: Annotated[
            float,
            Field(
                default=85,
                description=(
                    "Minimum confidence required to auto-execute the service. "
                    "Accepts 0-100 for percentages or 0-1 for ratios."
                ),
            ),
        ] = 85,
        limit: Annotated[
            int,
            Field(
                default=5,
                ge=1,
                le=10,
                description="Maximum number of candidate matches to include in responses",
            ),
        ] = 5,
        selected_entity_id: str | None = None,
        confirm: bool | None = None,
        retry_count: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of elicitation retries already performed",
            ),
        ] = 0,
        max_retries: Annotated[
            int,
            Field(
                default=2,
                ge=1,
                le=5,
                description="Maximum number of elicitation attempts before failing",
            ),
        ] = 2,
        entity_domain: str | None = Field(
            default=None,
            description="Optional entity domain filter when service domain is generic",
        ),
        use_cache: bool = Field(
            default=True,
            description="Whether to reuse cached entity matches for faster execution",
        ),
        cache_key: str | None = Field(
            default=None,
            description="Optional custom cache namespace shared across related requests",
        ),
    ) -> dict[str, Any]:
        """Execute a service call using weighted fuzzy search with elicitation fallback."""

        # Parse service data similar to ha_call_service
        try:
            parsed_data = parse_json_param(data, "data") if data is not None else None
        except ValueError as exc:
            return {
                "success": False,
                "error": f"Invalid data parameter: {exc}",
                "provided_data_type": type(data).__name__,
            }

        if parsed_data is not None and not isinstance(parsed_data, dict):
            return {
                "success": False,
                "error": "Data parameter must be a JSON object",
                "provided_type": type(parsed_data).__name__,
            }

        service_payload: dict[str, Any] = parsed_data.copy() if isinstance(parsed_data, dict) else {}

        try:
            normalized_terms = _normalize_quick_action_terms(search_terms)
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "search_terms": search_terms,
            }

        try:
            min_confidence_ratio, min_confidence_percent = _normalize_quick_action_confidence(
                min_confidence
            )
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "min_confidence": min_confidence,
            }

        confidence_percent_display = round(min_confidence_percent, 2)
        confidence_ratio_display = round(min_confidence_ratio, 4)

        applied_terms = [
            {
                "value": term["value"],
                "weight": round(term["weight"], 4),
                "original_weight": round(term["original_weight"], 4),
            }
            for term in normalized_terms
        ]

        if selected_entity_id and confirm is False:
            return {
                "success": False,
                "cancelled": True,
                "reason": "user_declined_selection",
                "entity_id": selected_entity_id,
                "message": "Selection cancelled. Provide a different entity_id or refine the search terms.",
                "search_terms": applied_terms,
            }

        settings = get_global_settings()
        searcher = create_fuzzy_searcher(threshold=settings.fuzzy_threshold)

        # Determine default entity domain filter when not explicitly provided
        domain_filter = entity_domain
        generic_domains = {"homeassistant"}
        if domain_filter is None and domain not in generic_domains:
            domain_filter = domain

        resolved_cache_key = _make_quick_action_cache_key(
            domain, service, normalized_terms, domain_filter, cache_key
        )

        async def _perform_service(
            target_entity_id: str | None,
            context: dict[str, Any],
            matches: list[dict[str, Any]] | None,
            source: str,
            cache_hit: bool,
        ) -> dict[str, Any]:
            call_payload = service_payload.copy()
            if target_entity_id:
                call_payload["entity_id"] = target_entity_id

            try:
                result = await client.call_service(domain, service, call_payload)
            except Exception as exc:  # noqa: BLE001 - surface HA errors to caller
                return {
                    "success": False,
                    "error": str(exc),
                    "domain": domain,
                    "service": service,
                    "entity_id": target_entity_id,
                    "search_terms": applied_terms,
                    "source": source,
                    "cache_hit": cache_hit,
                }

            response = {
                "success": True,
                "domain": domain,
                "service": service,
                "entity_id": target_entity_id,
                "data": call_payload,
                "result": result,
                "confidence": context.get("score"),
                "match_type": context.get("match_type"),
                "search_terms": applied_terms,
                "cache_hit": cache_hit,
                "search_context": {
                    "strategy": "weighted_fuzzy",
                    "source": source,
                    "limit": limit,
                    "matches_considered": matches or [],
                },
                "confidence_threshold_percent": confidence_percent_display,
                "confidence_threshold_ratio": confidence_ratio_display,
                "message": (
                    f"Successfully executed {domain}.{service}"
                    if target_entity_id
                    else f"Executed {domain}.{service} without a specific entity"
                ),
            }

            if use_cache:
                _store_cached_quick_action(
                    resolved_cache_key,
                    {
                        "entity_id": target_entity_id,
                        "score": context.get("score"),
                        "friendly_name": context.get("friendly_name"),
                        "match_type": context.get("match_type"),
                        "matches": matches or [],
                        "terms": applied_terms,
                        "domain": domain,
                        "service": service,
                        "entity_domain": domain_filter,
                        "cached_at": time.time(),
                    },
                )

            return response

        # Attempt cache shortcut before performing expensive searches
        cache_entry = _get_cached_quick_action(resolved_cache_key) if use_cache else None
        if cache_entry and not selected_entity_id:
            cached_entity_id = cache_entry.get("entity_id")
            if cached_entity_id:
                try:
                    cached_state = await client.get_entity_state(cached_entity_id)
                except Exception:
                    cache_entry = None
                    _QUICK_ACTION_CACHE.pop(resolved_cache_key, None)
                else:
                    cached_context = {
                        "entity_id": cached_entity_id,
                        "friendly_name": cache_entry.get("friendly_name")
                        or cached_state.get("attributes", {}).get(
                            "friendly_name", cached_entity_id
                        ),
                        "score": cache_entry.get("score"),
                        "match_type": cache_entry.get("match_type", "cache"),
                    }
                    return await _perform_service(
                        cached_entity_id,
                        cached_context,
                        cache_entry.get("matches") or [],
                        source="cache",
                        cache_hit=True,
                    )

        # Fetch all states once when needed
        entities = await client.get_states()
        all_matches = _score_entities_for_quick_action(
            entities, normalized_terms, searcher, domain_filter
        )
        top_matches = all_matches[:limit]

        if selected_entity_id:
            selected_match = next(
                (match for match in all_matches if match["entity_id"] == selected_entity_id),
                None,
            )

            if not selected_match:
                try:
                    selected_state = await client.get_entity_state(selected_entity_id)
                except Exception as exc:  # noqa: BLE001 - propagate lookup errors
                    return {
                        "success": False,
                        "error": f"Unable to resolve selected entity: {exc}",
                        "entity_id": selected_entity_id,
                        "search_terms": applied_terms,
                        "top_matches": top_matches,
                    }

                selected_match = {
                    "entity_id": selected_entity_id,
                    "friendly_name": selected_state.get("attributes", {}).get(
                        "friendly_name", selected_entity_id
                    ),
                    "score": None,
                    "match_type": "manual_selection",
                    "term_breakdown": [],
                }

            return await _perform_service(
                selected_entity_id,
                selected_match,
                top_matches,
                source="user_selection",
                cache_hit=False,
            )

        if not top_matches:
            suggestions = searcher.get_smart_suggestions(
                entities, " ".join(term["value"] for term in normalized_terms)
            )

            if retry_count >= max_retries:
                return {
                    "success": False,
                    "error": "No matching entities found after maximum retries",
                    "search_terms": applied_terms,
                    "suggestions": suggestions,
                    "retry_count": retry_count,
                    "max_retries": max_retries,
                }

            return {
                "success": False,
                "needs_elicitation": True,
                "reason": "no_matches",
                "search_terms": applied_terms,
                "suggestions": suggestions,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "elicitation": {
                    "type": "refine_search_terms",
                    "message": "No entities matched the provided search terms. Provide more specific terms or different keywords.",
                    "next_call": {
                        "tool": "ha_quick_service_action",
                        "parameters": {
                            "domain": domain,
                            "service": service,
                            "search_terms": applied_terms,
                            "data": service_payload,
                            "min_confidence": min_confidence,
                            "limit": limit,
                            "retry_count": retry_count + 1,
                            "max_retries": max_retries,
                            "entity_domain": domain_filter,
                            "use_cache": use_cache,
                            "cache_key": cache_key,
                        },
                    },
                },
            }

        best_match = top_matches[0]

        if best_match["score"] >= min_confidence_percent:
            return await _perform_service(
                best_match["entity_id"],
                best_match,
                top_matches,
                source="auto",
                cache_hit=False,
            )

        suggestions = searcher.get_smart_suggestions(
            entities, " ".join(term["value"] for term in normalized_terms)
        )

        if retry_count >= max_retries:
            return {
                "success": False,
                "error": (
                    "Unable to identify a matching entity with sufficient confidence. "
                    "Manual confirmation required."
                ),
                "search_terms": applied_terms,
                "top_matches": top_matches,
                "best_score": best_match["score"],
                "min_confidence": confidence_percent_display,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "suggestions": suggestions,
            }

        options = [
            {
                "entity_id": match["entity_id"],
                "friendly_name": match["friendly_name"],
                "score": match["score"],
                "match_type": match.get("match_type"),
            }
            for match in top_matches
        ]

        return {
            "success": False,
            "needs_elicitation": True,
            "reason": "low_confidence",
            "search_terms": applied_terms,
            "top_matches": top_matches,
            "min_confidence": confidence_percent_display,
            "best_score": best_match["score"],
            "retry_count": retry_count,
            "max_retries": max_retries,
            "suggestions": suggestions,
            "elicitation": {
                "type": "confirm_entity",
                "message": (
                    "Confirm the correct entity or provide additional search terms. "
                    f"Best match scored {best_match['score']:.1f}% (threshold {confidence_percent_display}%)."
                ),
                "options": options,
                "instructions": "Call this tool again with selected_entity_id set to the correct entity and confirm=true, or provide refined search terms.",
                "next_call": {
                    "tool": "ha_quick_service_action",
                    "parameters": {
                        "domain": domain,
                        "service": service,
                        "search_terms": applied_terms,
                        "data": service_payload,
                        "min_confidence": min_confidence,
                        "limit": limit,
                        "retry_count": retry_count + 1,
                        "max_retries": max_retries,
                        "entity_domain": domain_filter,
                        "use_cache": use_cache,
                        "cache_key": cache_key,
                    },
                },
            },
        }

    @mcp.tool
    async def ha_get_operation_status(
        operation_id: str, timeout_seconds: int = 10
    ) -> dict[str, Any]:
        """Check status of device operation with real-time WebSocket verification."""
        result = await device_tools.get_device_operation_status(
            operation_id=operation_id, timeout_seconds=timeout_seconds
        )
        return cast(dict[str, Any], result)

    @mcp.tool
    async def ha_bulk_control(
        operations: str | list[dict[str, Any]], parallel: bool = True
    ) -> dict[str, Any]:
        """Control multiple devices with bulk operation support and WebSocket tracking."""
        # Parse JSON operations if provided as string
        try:
            parsed_operations = parse_json_param(operations, "operations")
        except ValueError as e:
            return {
                "success": False,
                "error": f"Invalid operations parameter: {e}",
                "provided_operations_type": type(operations).__name__,
            }

        # Ensure operations is a list of dicts
        if parsed_operations is None or not isinstance(parsed_operations, list):
            return {
                "success": False,
                "error": "Operations parameter must be a list",
                "provided_type": type(parsed_operations).__name__,
            }

        operations_list = cast(list[dict[str, Any]], parsed_operations)
        result = await device_tools.bulk_device_control(
            operations=operations_list, parallel=parallel
        )
        return cast(dict[str, Any], result)

    @mcp.tool
    async def ha_get_bulk_status(operation_ids: list[str]) -> dict[str, Any]:
        """Check status of multiple WebSocket-monitored operations."""
        result = await device_tools.get_bulk_operation_status(
            operation_ids=operation_ids
        )
        return cast(dict[str, Any], result)
