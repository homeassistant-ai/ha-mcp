"""Placeholder-aware script generation and execution helpers."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Annotated, Any, cast

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency
    yaml = None

from pydantic import Field

from ..config import get_global_settings
from ..utils.fuzzy_search import create_fuzzy_searcher
from .tools_service import (
    _normalize_quick_action_confidence,
    _normalize_quick_action_terms,
    _score_entities_for_quick_action,
)
from .util_helpers import parse_json_param


_PLACEHOLDER_SELECTION_CACHE: "OrderedDict[str, str]" = OrderedDict()
_PLACEHOLDER_CACHE_LIMIT = 64


def _remember_placeholder_selection(placeholder_id: str, entity_id: str) -> None:
    if not placeholder_id or not entity_id:
        return

    _PLACEHOLDER_SELECTION_CACHE[placeholder_id] = entity_id
    _PLACEHOLDER_SELECTION_CACHE.move_to_end(placeholder_id)

    while len(_PLACEHOLDER_SELECTION_CACHE) > _PLACEHOLDER_CACHE_LIMIT:
        _PLACEHOLDER_SELECTION_CACHE.popitem(last=False)


def _get_cached_placeholder_entity(placeholder_id: str) -> str | None:
    entity_id = _PLACEHOLDER_SELECTION_CACHE.get(placeholder_id)
    if entity_id is not None:
        _PLACEHOLDER_SELECTION_CACHE.move_to_end(placeholder_id)
    return entity_id


def _format_yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text:
        return "''"
    needs_quotes = any(
        ch in text for ch in [":", "-", "#", "{", "}", "[", "]", ",", "\n"]
    ) or text.strip() != text or text.startswith("{") or text.startswith("[")
    if needs_quotes:
        escaped = text.replace("\\", "\\\\").replace("\"", "\\\"")
        return f'"{escaped}"'
    return text


def _dump_yaml(data: Any, indent: int = 0) -> str:
    prefix = "  " * indent
    if isinstance(data, dict):
        lines: list[str] = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_dump_yaml(value, indent + 1))
            else:
                lines.append(f"{prefix}{key}: {_format_yaml_scalar(value)}")
        return "\n".join(lines)
    if isinstance(data, list):
        lines = []
        for item in data:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.append(_dump_yaml(item, indent + 1))
            else:
                lines.append(f"{prefix}- {_format_yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{prefix}{_format_yaml_scalar(data)}"


def _render_script_yaml(script_id: str, definition: dict[str, Any]) -> str:
    document = {"script": {script_id: definition}}
    if yaml is not None:  # pragma: no branch - prefer PyYAML if available
        return cast(str, yaml.safe_dump(document, sort_keys=False))
    return _dump_yaml(document)


def _normalize_placeholder_spec(
    raw_spec: dict[str, Any],
    additional_terms: list[Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    combined_terms: list[Any] = []
    base_terms = raw_spec.get("search_terms")
    if base_terms is not None:
        if isinstance(base_terms, str):
            combined_terms.append(base_terms)
        elif isinstance(base_terms, list):
            combined_terms.extend(base_terms)
        elif isinstance(base_terms, dict):
            combined_terms.append(base_terms)
        else:
            raise ValueError(
                "search_terms must be string, list, or dict; got "
                f"{type(base_terms).__name__}"
            )

    if additional_terms:
        if isinstance(additional_terms, list):
            combined_terms.extend(additional_terms)
        else:
            combined_terms.append(additional_terms)

    if not combined_terms:
        raise ValueError(
            f"Placeholder {raw_spec.get('id', '<unknown>')} requires search_terms"
        )

    normalized_terms = _normalize_quick_action_terms(combined_terms)

    min_confidence = raw_spec.get("min_confidence")
    ratio, percent = _normalize_quick_action_confidence(min_confidence)

    limit = raw_spec.get("limit", 5)
    try:
        limit_value = int(limit)
    except (TypeError, ValueError):
        limit_value = 5

    normalized_spec = {
        "id": str(raw_spec.get("id", "")).strip(),
        "domain": raw_spec.get("domain"),
        "search_terms": normalized_terms,
        "min_confidence": min_confidence
        if min_confidence is not None
        else percent,
        "confidence_threshold_percent": round(percent, 3),
        "confidence_threshold_ratio": round(ratio, 6),
        "fallback_entity_id": raw_spec.get("fallback_entity_id"),
        "area_id": raw_spec.get("area_id"),
        "reuse": bool(raw_spec.get("reuse", True)),
        "resolution_strategy": raw_spec.get(
            "resolution_strategy", "weighted_fuzzy"
        ),
        "limit": max(1, min(limit_value, 10)),
        "metadata": raw_spec.get("metadata", {}),
    }

    return normalized_spec, normalized_terms


def _placeholder_match_priority(match_type: str | None) -> int:
    if not match_type:
        return 4
    lowered = match_type.lower()
    if lowered.startswith("exact"):
        return 0
    if "partial" in lowered:
        return 1
    if "fuzzy" in lowered:
        return 2
    return 3


def _rank_placeholder_matches(
    placeholder_id: str, matches: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    cached_entity = _get_cached_placeholder_entity(placeholder_id)

    def sort_key(match: dict[str, Any]) -> tuple[Any, ...]:
        term_breakdown = match.get("term_breakdown", [])
        dominant_weight = 0.0
        if term_breakdown:
            dominant_weight = max(
                float(item.get("weight", 0.0)) for item in term_breakdown
            )
        cache_priority = 0
        if cached_entity and cached_entity == match.get("entity_id"):
            cache_priority = -1
        return (
            -float(match.get("score", 0.0)),
            _placeholder_match_priority(match.get("match_type")),
            -dominant_weight,
            cache_priority,
            match.get("entity_id", ""),
        )

    ranked = sorted(matches, key=sort_key)
    return ranked


def _collect_manifest_placeholders(
    manifest: dict[str, Any] | list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if isinstance(manifest, dict):
        if "placeholders" in manifest:
            placeholders = manifest.get("placeholders", [])
            if isinstance(placeholders, list):
                return cast(list[dict[str, Any]], placeholders)
            raise ValueError("placeholder_manifest['placeholders'] must be a list")
        return [manifest]
    if isinstance(manifest, list):
        return manifest
    raise ValueError("placeholder_manifest must be list or dict")


def register_script_runner_tools(mcp: Any, client: Any) -> None:
    """Register placeholder-focused script helpers."""

    settings = get_global_settings()

    @mcp.tool
    async def ha_generate_placeholder_script(
        script_id: Annotated[
            str,
            Field(
                description="Script identifier without domain prefix (e.g., 'dynamic_salon_scene')"
            ),
        ],
        alias: Annotated[
            str,
            Field(description="Human-readable script alias"),
        ],
        placeholders: Annotated[
            Any,
            Field(
                description=(
                    "Placeholder descriptors with search terms and constraints."
                )
            ),
        ],
        sequence: Annotated[
            Any,
            Field(
                description=(
                    "Script action sequence using Home Assistant script syntax"
                )
            ),
        ],
        description: Annotated[
            str | None,
            Field(
                default=None,
                description="Optional script description for documentation",
            ),
        ] = None,
        mode: Annotated[
            str,
            Field(
                default="single",
                description="Home Assistant script execution mode",
            ),
        ] = "single",
        additional_fields: Annotated[
            Any,
            Field(
                default=None,
                description="Additional script fields beyond generated placeholders",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Generate YAML/manifest artifacts for dynamic placeholder scripts."""

        script_key = script_id.strip()
        if script_key.startswith("script."):
            script_key = script_key.split(".", 1)[1]

        try:
            parsed_placeholders = parse_json_param(placeholders, "placeholders")
        except ValueError as exc:
            return {
                "success": False,
                "error": f"Invalid placeholders parameter: {exc}",
            }

        try:
            parsed_sequence = parse_json_param(sequence, "sequence")
        except ValueError as exc:
            return {
                "success": False,
                "error": f"Invalid sequence parameter: {exc}",
            }

        if not isinstance(parsed_sequence, list):
            return {
                "success": False,
                "error": "sequence must be a list of script actions",
                "provided_type": type(parsed_sequence).__name__,
            }

        try:
            placeholder_specs = _collect_manifest_placeholders(
                cast(dict[str, Any] | list[dict[str, Any]], parsed_placeholders)
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        manifest_placeholders: list[dict[str, Any]] = []
        fields_block: dict[str, Any] = {}

        for raw_spec in placeholder_specs:
            try:
                normalized_spec, normalized_terms = _normalize_placeholder_spec(raw_spec)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}

            placeholder_id = normalized_spec["id"]
            if not placeholder_id:
                return {
                    "success": False,
                    "error": "Each placeholder requires a non-empty id",
                }

            manifest_placeholders.append(normalized_spec)

            fields_block[placeholder_id] = {
                "name": raw_spec.get(
                    "name",
                    placeholder_id.replace("_", " ").title(),
                ),
                "description": raw_spec.get(
                    "description",
                    "Resolved via weighted fuzzy search",
                ),
                "selector": {
                    "entity": {
                        "domain": raw_spec.get("domain"),
                    }
                },
                "examples": raw_spec.get("examples"),
                "metadata": {
                    "normalized_terms": normalized_terms,
                    "confidence_threshold_percent": normalized_spec[
                        "confidence_threshold_percent"
                    ],
                },
            }

        if additional_fields:
            try:
                parsed_additional = parse_json_param(
                    additional_fields, "additional_fields"
                )
            except ValueError as exc:
                return {
                    "success": False,
                    "error": f"Invalid additional_fields parameter: {exc}",
                }
            if isinstance(parsed_additional, dict):
                fields_block.update(parsed_additional)
            else:
                return {
                    "success": False,
                    "error": "additional_fields must be a mapping of field definitions",
                }

        script_definition = {
            "alias": alias,
            "mode": mode,
            "fields": fields_block,
            "sequence": parsed_sequence,
        }
        if description:
            script_definition["description"] = description

        script_yaml = _render_script_yaml(script_key, script_definition)

        manifest = {
            "placeholders": manifest_placeholders,
            "generated_at": int(time.time()),
            "strategy": "weighted_fuzzy",
        }

        return {
            "success": True,
            "script_id": script_key,
            "entity_id": f"script.{script_key}",
            "script_yaml": script_yaml,
            "manifest": manifest,
            "fields": fields_block,
        }

    @mcp.tool
    async def ha_run_placeholder_script(
        script_id: Annotated[
            str,
            Field(description="Script identifier (with or without 'script.' prefix)"),
        ],
        placeholder_manifest: Annotated[
            Any,
            Field(description="Placeholder manifest describing dynamic lookups"),
        ],
        placeholder_selections: Annotated[
            Any,
            Field(
                default=None,
                description="Manual placeholder selections {placeholder_id: entity_id}",
            ),
        ] = None,
        placeholder_search_terms: Annotated[
            Any,
            Field(
                default=None,
                description="Additional weighted search terms per placeholder",
            ),
        ] = None,
        resolved_entities: Annotated[
            Any,
            Field(
                default=None,
                description="Previously resolved placeholders to reuse across calls",
            ),
        ] = None,
        fields: Annotated[
            Any,
            Field(
                default=None,
                description="Additional script field values to pass on execution",
            ),
        ] = None,
        elicitation_round: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of elicitation rounds already performed",
            ),
        ] = 0,
        max_elicitation_rounds: Annotated[
            int,
            Field(
                default=2,
                ge=1,
                le=4,
                description="Maximum total elicitation rounds before failing",
            ),
        ] = 2,
    ) -> dict[str, Any]:
        """Resolve placeholders, optionally elicit clarification, then execute script."""

        script_key = script_id.strip()
        if script_key.startswith("script."):
            script_key = script_key.split(".", 1)[1]

        try:
            parsed_manifest = parse_json_param(
                placeholder_manifest, "placeholder_manifest"
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        try:
            parsed_selections = parse_json_param(
                placeholder_selections, "placeholder_selections"
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        try:
            parsed_search_terms = parse_json_param(
                placeholder_search_terms, "placeholder_search_terms"
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        try:
            parsed_resolved = parse_json_param(resolved_entities, "resolved_entities")
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        try:
            parsed_fields = parse_json_param(fields, "fields")
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        try:
            placeholder_specs = _collect_manifest_placeholders(
                cast(dict[str, Any] | list[dict[str, Any]], parsed_manifest)
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        manual_selections: dict[str, str] = {}
        if isinstance(parsed_selections, dict):
            manual_selections = {
                str(key): str(value)
                for key, value in parsed_selections.items()
                if value is not None
            }

        additional_terms_map: dict[str, list[Any]] = {}
        if isinstance(parsed_search_terms, dict):
            for key, value in parsed_search_terms.items():
                if isinstance(value, list):
                    additional_terms_map[str(key)] = list(value)
                elif value is not None:
                    additional_terms_map[str(key)] = [value]

        existing_resolutions: dict[str, str] = {}
        if isinstance(parsed_resolved, dict):
            existing_resolutions = {
                str(key): str(value)
                for key, value in parsed_resolved.items()
                if value is not None
            }

        variables: dict[str, Any] = {}
        if isinstance(parsed_fields, dict):
            variables.update(parsed_fields)

        states = await client.get_states()
        searcher = create_fuzzy_searcher(threshold=settings.fuzzy_threshold)

        placeholder_results: dict[str, dict[str, Any]] = {}

        for placeholder in placeholder_specs:
            placeholder_id = str(placeholder.get("id", "")).strip()
            if not placeholder_id:
                return {
                    "success": False,
                    "error": "Placeholder without id detected in manifest",
                }

            if placeholder_id in existing_resolutions:
                variables[placeholder_id] = existing_resolutions[placeholder_id]
                placeholder_results[placeholder_id] = {
                    "entity_id": existing_resolutions[placeholder_id],
                    "source": "cached",
                    "score": None,
                    "threshold_percent": placeholder.get(
                        "confidence_threshold_percent"
                    ),
                }
                continue

            if placeholder_id in manual_selections:
                selected_entity = manual_selections[placeholder_id]
                matching_state = next(
                    (
                        entity
                        for entity in states
                        if entity.get("entity_id") == selected_entity
                    ),
                    None,
                )
                if not matching_state:
                    return {
                        "success": False,
                        "error": (
                            f"Selected entity {selected_entity} for {placeholder_id} "
                            "was not found in Home Assistant states"
                        ),
                    }

                variables[placeholder_id] = selected_entity
                placeholder_results[placeholder_id] = {
                    "entity_id": selected_entity,
                    "source": "manual",
                    "score": None,
                    "threshold_percent": placeholder.get(
                        "confidence_threshold_percent"
                    ),
                }
                _remember_placeholder_selection(placeholder_id, selected_entity)
                continue

            extra_terms = additional_terms_map.get(placeholder_id, [])

            try:
                normalized_spec, normalized_terms = _normalize_placeholder_spec(
                    placeholder, extra_terms
                )
            except ValueError as exc:
                return {"success": False, "error": str(exc)}

            domain_filter = normalized_spec.get("domain")
            matches = _score_entities_for_quick_action(
                states, normalized_terms, searcher, domain_filter=domain_filter
            )
            ranked_matches = _rank_placeholder_matches(
                placeholder_id, matches
            )[: normalized_spec["limit"]]

            if not ranked_matches:
                if elicitation_round >= max_elicitation_rounds:
                    return {
                        "success": False,
                        "error": (
                            "No entities matched the provided search terms for "
                            f"{placeholder_id}. Threshold {normalized_spec['confidence_threshold_percent']}%"
                        ),
                        "placeholder_id": placeholder_id,
                        "candidates": [],
                        "elicitation_round": elicitation_round,
                        "max_elicitation_rounds": max_elicitation_rounds,
                    }

                return {
                    "success": False,
                    "needs_elicitation": True,
                    "placeholder_id": placeholder_id,
                    "reason": "no_matches",
                    "message": (
                        f"No matches found for {placeholder_id}. Provide more specific "
                        "search terms or choose a fallback entity."
                    ),
                    "options": [],
                    "elicitation_round": elicitation_round,
                    "max_elicitation_rounds": max_elicitation_rounds,
                    "next_call": {
                        "tool": "ha_run_placeholder_script",
                        "parameters": {
                            "script_id": script_key,
                            "placeholder_manifest": parsed_manifest,
                            "placeholder_search_terms": additional_terms_map,
                            "resolved_entities": existing_resolutions,
                            "fields": variables,
                            "elicitation_round": elicitation_round + 1,
                            "max_elicitation_rounds": max_elicitation_rounds,
                        },
                    },
                }

            best_match = ranked_matches[0]
            threshold_percent = normalized_spec["confidence_threshold_percent"]

            if best_match["score"] >= threshold_percent:
                variables[placeholder_id] = best_match["entity_id"]
                placeholder_results[placeholder_id] = {
                    "entity_id": best_match["entity_id"],
                    "source": "auto",
                    "score": best_match["score"],
                    "threshold_percent": threshold_percent,
                    "term_breakdown": best_match.get("term_breakdown", []),
                    "match_type": best_match.get("match_type"),
                }
                _remember_placeholder_selection(
                    placeholder_id, best_match["entity_id"]
                )
                continue

            if elicitation_round >= max_elicitation_rounds:
                return {
                    "success": False,
                    "error": (
                        f"Unable to resolve {placeholder_id} above threshold "
                        f"{threshold_percent}% after {elicitation_round} rounds."
                    ),
                    "placeholder_id": placeholder_id,
                    "candidates": ranked_matches,
                    "elicitation_round": elicitation_round,
                    "max_elicitation_rounds": max_elicitation_rounds,
                }

            options = [
                {
                    "entity_id": match.get("entity_id"),
                    "friendly_name": match.get("friendly_name"),
                    "score": match.get("score"),
                    "match_type": match.get("match_type"),
                    "term_breakdown": match.get("term_breakdown", []),
                }
                for match in ranked_matches
            ]

            message = (
                f"Top match for {placeholder_id} scored {best_match['score']:.1f}% "
                f"below the {threshold_percent:.1f}% threshold."
            )

            additional_terms_map.setdefault(placeholder_id, [])

            return {
                "success": False,
                "needs_elicitation": True,
                "placeholder_id": placeholder_id,
                "reason": "low_confidence",
                "message": message,
                "options": options,
                "elicitation_round": elicitation_round,
                "max_elicitation_rounds": max_elicitation_rounds,
                "best_score": best_match["score"],
                "threshold_percent": threshold_percent,
                "next_call": {
                    "tool": "ha_run_placeholder_script",
                    "parameters": {
                        "script_id": script_key,
                        "placeholder_manifest": parsed_manifest,
                        "placeholder_search_terms": additional_terms_map,
                        "resolved_entities": existing_resolutions,
                        "fields": variables,
                        "elicitation_round": elicitation_round + 1,
                        "max_elicitation_rounds": max_elicitation_rounds,
                    },
                },
            }

        service_data = {
            "entity_id": f"script.{script_key}",
            "variables": variables,
        }

        await client.call_service("script", "turn_on", service_data)

        for placeholder_id, details in placeholder_results.items():
            entity_id = details.get("entity_id")
            if entity_id:
                _remember_placeholder_selection(placeholder_id, entity_id)

        return {
            "success": True,
            "script_id": script_key,
            "entity_id": f"script.{script_key}",
            "resolved_placeholders": placeholder_results,
            "variables": variables,
            "service_call": {
                "domain": "script",
                "service": "turn_on",
                "data": service_data,
            },
        }


__all__ = ["register_script_runner_tools"]

