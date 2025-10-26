"""Quick placeholder script execution tool for Home Assistant MCP server.

This module implements the "quick call" workflow that resolves placeholder
entity references, performs weighted fuzzy scoring, optionally elicits more
information from the caller, and ultimately executes a Home Assistant script
once all placeholders have been resolved.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Any, Iterable

from pydantic import BaseModel, Field, ValidationError

from ..utils.fuzzy_search import (
    calculate_partial_ratio,
    calculate_ratio,
    calculate_token_sort_ratio,
)

MAX_ELICITATION_ROUNDS = 2
DEFAULT_LIMIT = 8
DEFAULT_CONFIDENCE_RATIO = 0.75
DEFAULT_GAP_POINTS = 10.0


class WeightedTermModel(BaseModel):
    """Input model representing a weighted search term."""

    value: str
    weight: float | int | None = Field(
        default=None, description="Optional weight for the term (0-1 or percentage)"
    )


class PlaceholderOverrideModel(BaseModel):
    """Input model describing placeholder overrides passed to the tool."""

    id: str = Field(..., description="Placeholder identifier")
    search_terms: list[str | WeightedTermModel] | None = Field(
        default=None,
        description="Override search terms for this placeholder",
    )
    min_confidence: float | int | None = Field(
        default=None,
        description="Override minimum confidence (0-1 float or 0-100 integer)",
    )
    fallback_entity_id: str | None = Field(
        default=None, description="Optional fallback entity ID when no match"
    )
    limit: int | None = Field(
        default=None,
        description="Maximum number of candidates to evaluate for this placeholder",
    )
    elicitation_round: int | None = Field(
        default=None,
        description="Caller-provided tracking for elicitation rounds",
    )


@dataclass(slots=True)
class NormalizedTerm:
    """Normalized representation of a search term with resolved weight."""

    value: str
    weight: float
    raw_weight: float | None


@dataclass(slots=True)
class PlaceholderDefinition:
    """Merged manifest definition for a single placeholder."""

    id: str
    search_terms: list[str | dict[str, Any]]
    min_confidence: float | int | None = None
    fallback_entity_id: str | None = None
    limit: int | None = None


@dataclass(slots=True)
class PlaceholderContext:
    """Runtime context used during placeholder resolution."""

    id: str
    normalized_terms: list[NormalizedTerm]
    raw_terms: list[Any]
    threshold_ratio: float
    threshold_percent: float
    threshold_source: str
    limit: int
    fallback_entity_id: str | None
    source: str


def normalize_search_terms(raw_terms: Iterable[str | dict[str, Any]]) -> tuple[list[NormalizedTerm], list[Any]]:
    """Normalize search terms into weighted structures.

    Args:
        raw_terms: Iterable of strings or mapping objects containing a ``value``
            key and optional ``weight`` field.

    Returns:
        Tuple of normalized ``NormalizedTerm`` entries and a JSON-serialisable
        list capturing the raw, caller-supplied data for diagnostics.

    Raises:
        ValueError: If terms are empty or contain invalid structures.
    """

    terms: list[NormalizedTerm] = []
    raw_snapshot: list[Any] = []

    term_list = list(raw_terms or [])
    if not term_list:
        raise ValueError("At least one search term is required")

    total_weight = 0.0
    implicit_weight_count = 0

    for item in term_list:
        if isinstance(item, str):
            value = item.strip()
            if not value:
                raise ValueError("Search term strings cannot be empty")
            raw_snapshot.append(item)
            terms.append(NormalizedTerm(value=value, weight=0.0, raw_weight=None))
            implicit_weight_count += 1
            continue

        if isinstance(item, dict):
            if "value" not in item:
                raise ValueError("Search term objects must include a 'value' field")
            value = str(item["value"]).strip()
            if not value:
                raise ValueError("Search term value cannot be empty")

            raw_snapshot.append(dict(item))
            weight = item.get("weight")
            if weight is None:
                terms.append(NormalizedTerm(value=value, weight=0.0, raw_weight=None))
                implicit_weight_count += 1
                continue

            try:
                weight_float = float(weight)
            except (TypeError, ValueError) as exc:
                raise ValueError("Search term weight must be numeric") from exc

            if weight_float < 0:
                raise ValueError("Search term weight cannot be negative")

            terms.append(
                NormalizedTerm(value=value, weight=weight_float, raw_weight=weight_float)
            )
            total_weight += weight_float
            continue

        raise ValueError(
            "Search terms must be strings or objects with 'value'/'weight' fields"
        )

    # Assign implicit weights if none provided
    if total_weight == 0 and implicit_weight_count > 0:
        uniform_weight = 1.0 / len(terms)
        terms = [NormalizedTerm(value=t.value, weight=uniform_weight, raw_weight=t.raw_weight) for t in terms]
        return terms, raw_snapshot

    # Mix implicit weights with explicit ones by distributing remaining weight evenly
    if implicit_weight_count > 0:
        remaining_weight = max(0.0, 1.0 - total_weight)
        if math.isclose(total_weight, 0.0):
            share = 1.0 / implicit_weight_count
        else:
            share = remaining_weight / implicit_weight_count if implicit_weight_count else 0.0
        for idx, term in enumerate(terms):
            if term.raw_weight is None:
                terms[idx] = NormalizedTerm(value=term.value, weight=share, raw_weight=None)
            else:
                normalized_weight = term.weight
                terms[idx] = NormalizedTerm(value=term.value, weight=normalized_weight, raw_weight=term.raw_weight)
    else:
        # Purely explicit weights: normalise to 1.0 if possible
        if total_weight <= 0:
            raise ValueError("At least one positive weight is required")
        terms = [
            NormalizedTerm(
                value=term.value,
                weight=term.weight / total_weight,
                raw_weight=term.raw_weight,
            )
            for term in terms
        ]

    # Final sanity check to ensure weights sum to 1.0
    weight_sum = sum(term.weight for term in terms)
    if not math.isclose(weight_sum, 1.0, rel_tol=1e-6):
        correction = 1.0 / weight_sum
        terms = [
            NormalizedTerm(
                value=term.value,
                weight=term.weight * correction,
                raw_weight=term.raw_weight,
            )
            for term in terms
        ]

    return terms, raw_snapshot


def normalize_confidence(
    value: float | int | None, default_ratio: float = DEFAULT_CONFIDENCE_RATIO
) -> tuple[float, float, str]:
    """Normalise ``min_confidence`` values into ratio/percentage pairs."""

    if value is None:
        ratio = default_ratio
        return ratio, ratio * 100.0, "default"

    if isinstance(value, float):
        if not 0 <= value <= 1:
            raise ValueError("Float confidence values must be between 0 and 1")
        return value, value * 100.0, "float"

    if isinstance(value, int):
        if not 0 <= value <= 100:
            raise ValueError("Integer confidence values must be between 0 and 100")
        ratio = value / 100.0
        return ratio, float(value), "int"

    raise ValueError("min_confidence must be float (0-1), int (0-100), or None")


def is_obvious_match(
    candidates: list[dict[str, Any]],
    threshold_ratio: float,
    cached_entity_id: str | None = None,
    gap_points: float = DEFAULT_GAP_POINTS,
) -> tuple[dict[str, Any] | None, str | None]:
    """Determine if a single candidate clearly satisfies the match threshold."""

    if not candidates:
        return None, None

    threshold = threshold_ratio * 100.0
    top = candidates[0]
    top_score = top.get("score", 0.0)
    top_raw = top.get("raw_score", top_score)
    if top_score < threshold:
        return None, None

    if len(candidates) == 1:
        return top, "single_candidate"

    # Cache preference when scores are tied and cached entity is present
    if cached_entity_id:
        cached_candidate = next(
            (candidate for candidate in candidates if candidate["entity_id"] == cached_entity_id),
            None,
        )
        cached_score = cached_candidate.get("score", 0.0) if cached_candidate else 0.0
        if cached_candidate and cached_score >= threshold:
            if abs(cached_score - top_score) < 1e-6:
                return cached_candidate, "cache_preference"

    second = candidates[1]
    second_raw = second.get("raw_score", second.get("score", 0.0))
    if top_raw - second_raw >= gap_points:
        return top, "gap_satisfied"

    return None, None


class QuickPlaceholderScriptExecutor:
    """Resolver and execution engine for quick placeholder scripts."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._manifest_cache: dict[str, dict[str, Any]] = {}
        self._selection_cache: dict[str, str] = {}

    async def execute(
        self,
        script_id: str,
        placeholders: list[PlaceholderOverrideModel] | None,
        script_args: dict[str, Any] | None,
        elicitation_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Resolve placeholders and execute the Home Assistant script."""

        state = dict(elicitation_state or {})
        rounds_used = int(state.get("rounds_used", 0))

        try:
            manifest = await self._load_manifest(script_id)
        except Exception as exc:  # pragma: no cover - safety net
            return {
                "status": "failed",
                "error": str(exc),
                "resolution_details": {},
            }

        try:
            merged_placeholders, placeholder_order = self._merge_placeholders(manifest, placeholders)
        except ValueError as exc:
            return {
                "status": "failed",
                "error": str(exc),
                "resolution_details": {},
            }

        try:
            contexts = self._build_placeholder_contexts(manifest, merged_placeholders, placeholder_order)
        except ValueError as exc:
            return {
                "status": "failed",
                "error": str(exc),
                "resolution_details": {},
            }

        resolved_entities: dict[str, str] = {}
        resolution_details: dict[str, Any] = {}
        placeholder_candidates_cache: dict[str, list[dict[str, Any]]] = {}

        try:
            entities = await self.client.get_states()
        except Exception as exc:
            return {
                "status": "failed",
                "error": f"Failed to load Home Assistant entities: {exc}",
                "resolution_details": {},
            }

        entity_lookup = {entity.get("entity_id"): entity for entity in entities}

        for context in contexts:
            if context.id in resolved_entities:
                details = resolution_details[context.id]
                details["cache_reused"] = True
                continue

            candidates = self._score_candidates(context, entity_lookup)
            placeholder_candidates_cache[context.id] = candidates

            cached_entity_id = self._selection_cache.get(context.id)
            match, decision = is_obvious_match(
                candidates,
                threshold_ratio=context.threshold_ratio,
                cached_entity_id=cached_entity_id,
            )

            detail_payload = self._build_detail_payload(
                context=context,
                candidates=candidates,
                decision=decision,
                cached_entity_id=cached_entity_id,
            )

            if match:
                resolved_entities[context.id] = match["entity_id"]
                resolution_details[context.id] = detail_payload | {
                    "status": "resolved",
                    "selected_entity": match,
                }
                self._selection_cache[context.id] = match["entity_id"]
                continue

            # Elicitation path
            resolution_details[context.id] = detail_payload | {"status": "needs_elicitation"}

            if rounds_used >= MAX_ELICITATION_ROUNDS:
                return {
                    "status": "failed",
                    "resolved_entities": resolved_entities,
                    "resolution_details": resolution_details,
                    "error": (
                        "Exceeded maximum elicitation rounds while resolving placeholder "
                        f"{context.id}"
                    ),
                }

            rounds_used += 1
            next_state = {"rounds_used": rounds_used}

            elicitation_payload = self._build_elicitation_payload(
                context=context,
                candidates=candidates,
                rounds_used=rounds_used,
            )
            return {
                "status": "elicitation",
                "resolved_entities": resolved_entities,
                "resolution_details": resolution_details,
                "elicitation": elicitation_payload,
                "elicitation_state": next_state,
            }

        # All placeholders resolved - execute script
        service_data: dict[str, Any] = {"entity_id": script_id}
        variables: dict[str, Any] = dict(resolved_entities)
        if script_args:
            variables.update(script_args)
        if variables:
            service_data["variables"] = variables

        try:
            service_result = await self.client.call_service("script", "turn_on", service_data)
            return {
                "status": "resolved",
                "resolved_entities": resolved_entities,
                "resolution_details": resolution_details,
                "script_execution": {
                    "success": True,
                    "service_data": service_data,
                    "result": service_result,
                },
            }
        except Exception as exc:
            return {
                "status": "failed",
                "resolved_entities": resolved_entities,
                "resolution_details": resolution_details,
                "script_execution": {
                    "success": False,
                    "service_data": service_data,
                    "error": str(exc),
                },
            }

    async def _load_manifest(self, script_id: str) -> dict[str, Any]:
        if script_id in self._manifest_cache:
            return self._manifest_cache[script_id]

        config = await self.client.get_script_config(script_id)
        manifest_data = self._extract_manifest(config)
        if manifest_data is None:
            # Allow scripts without manifests by returning empty defaults
            manifest = {
                "placeholders": {},
                "order": [],
                "defaults": {
                    "min_confidence": DEFAULT_CONFIDENCE_RATIO,
                    "limit": DEFAULT_LIMIT,
                },
            }
            self._manifest_cache[script_id] = manifest
            return manifest

        placeholders = manifest_data.get("placeholders", [])
        order: list[str] = []
        placeholder_map: dict[str, PlaceholderDefinition] = {}
        for placeholder in placeholders:
            if not isinstance(placeholder, dict) or "id" not in placeholder:
                continue
            placeholder_id = str(placeholder["id"])
            order.append(placeholder_id)
            placeholder_map[placeholder_id] = PlaceholderDefinition(
                id=placeholder_id,
                search_terms=list(placeholder.get("search_terms", [])),
                min_confidence=placeholder.get("min_confidence"),
                fallback_entity_id=placeholder.get("fallback_entity_id"),
                limit=placeholder.get("limit"),
            )

        manifest = {
            "placeholders": placeholder_map,
            "order": order,
            "defaults": {
                "min_confidence": manifest_data.get(
                    "min_confidence", DEFAULT_CONFIDENCE_RATIO
                ),
                "limit": manifest_data.get("limit", DEFAULT_LIMIT),
            },
        }
        self._manifest_cache[script_id] = manifest
        return manifest

    def _extract_manifest(self, config: dict[str, Any]) -> dict[str, Any] | None:
        manifest_keys = (
            "placeholder_manifest",
            "quick_call_manifest",
            "mcp_quick_call",
        )
        for key in manifest_keys:
            if key in config and isinstance(config[key], dict):
                return config[key]

        fields = config.get("fields")
        if isinstance(fields, dict):
            for key in manifest_keys:
                if key in fields and isinstance(fields[key], dict):
                    return fields[key]
        return None

    def _merge_placeholders(
        self,
        manifest: dict[str, Any],
        overrides: list[PlaceholderOverrideModel] | None,
    ) -> tuple[dict[str, PlaceholderDefinition], list[str]]:
        manifest_placeholders: dict[str, PlaceholderDefinition] = dict(
            manifest.get("placeholders", {})
        )
        order: list[str] = list(manifest.get("order", []))

        override_list = overrides or []
        for override in override_list:
            placeholder_id = override.id
            if placeholder_id in manifest_placeholders:
                placeholder = manifest_placeholders[placeholder_id]
            else:
                placeholder = PlaceholderDefinition(id=placeholder_id, search_terms=[])
                manifest_placeholders[placeholder_id] = placeholder
                order.append(placeholder_id)

            if override.search_terms is not None:
                placeholder.search_terms = [
                    term.model_dump() if isinstance(term, BaseModel) else term
                    for term in override.search_terms
                ]
            if override.min_confidence is not None:
                placeholder.min_confidence = override.min_confidence
            if override.fallback_entity_id is not None:
                placeholder.fallback_entity_id = override.fallback_entity_id
            if override.limit is not None:
                placeholder.limit = override.limit

        return manifest_placeholders, order

    def _build_placeholder_contexts(
        self,
        manifest: dict[str, Any],
        placeholders: dict[str, PlaceholderDefinition],
        order: list[str],
    ) -> list[PlaceholderContext]:
        contexts: list[PlaceholderContext] = []
        defaults = manifest.get("defaults", {})
        default_confidence = defaults.get("min_confidence", DEFAULT_CONFIDENCE_RATIO)
        default_limit = int(defaults.get("limit", DEFAULT_LIMIT))

        for placeholder_id in order:
            definition = placeholders.get(placeholder_id)
            if definition is None:
                raise ValueError(f"Missing definition for placeholder {placeholder_id}")
            if not definition.search_terms:
                raise ValueError(
                    f"Placeholder {placeholder_id} does not define any search terms"
                )

            normalized_terms, raw_snapshot = normalize_search_terms(
                definition.search_terms
            )
            threshold_ratio, threshold_percent, threshold_source = normalize_confidence(
                definition.min_confidence, default_confidence
            )

            limit = definition.limit if definition.limit is not None else default_limit
            if limit <= 0:
                raise ValueError(
                    f"Placeholder {placeholder_id} limit must be positive (got {limit})"
                )

            contexts.append(
                PlaceholderContext(
                    id=placeholder_id,
                    normalized_terms=normalized_terms,
                    raw_terms=raw_snapshot,
                    threshold_ratio=threshold_ratio,
                    threshold_percent=threshold_percent,
                    threshold_source=threshold_source,
                    limit=limit,
                    fallback_entity_id=definition.fallback_entity_id,
                    source="manifest",
                )
            )

        return contexts

    def _score_candidates(
        self,
        context: PlaceholderContext,
        entity_lookup: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidate_ids: set[str] = set()

        for term in context.normalized_terms:
            top_candidates = self._score_term_candidates(term.value, entity_lookup.values(), context.limit)
            candidate_ids.update(candidate["entity_id"] for candidate in top_candidates)

        if context.fallback_entity_id:
            candidate_ids.add(context.fallback_entity_id)

        candidates: list[dict[str, Any]] = []
        for entity_id in candidate_ids:
            entity_state = entity_lookup.get(entity_id)
            if not entity_state:
                continue
            friendly_name = entity_state.get("attributes", {}).get(
                "friendly_name", entity_id
            )
            domain = entity_id.split(".")[0] if "." in entity_id else "unknown"
            area_id = entity_state.get("attributes", {}).get("area_id")

            term_breakdown = []
            total_score = 0.0
            for term in context.normalized_terms:
                raw_score = self._score_single_term(entity_id, friendly_name, domain, term.value)
                contribution = raw_score * term.weight
                total_score += contribution
                term_breakdown.append(
                    {
                        "term": term.value,
                        "weight": term.weight,
                        "raw_score": raw_score,
                        "contribution": contribution,
                    }
                )

            candidate = {
                "entity_id": entity_id,
                "friendly_name": friendly_name,
                "domain": domain,
                "area_id": area_id,
                "raw_score": total_score,
                "score": min(100.0, total_score),
                "term_breakdown": term_breakdown,
            }
            candidates.append(candidate)

        candidates.sort(
            key=lambda item: (item.get("score", 0.0), item.get("raw_score", 0.0)),
            reverse=True,
        )
        return candidates[: context.limit]

    def _score_term_candidates(
        self,
        term_value: str,
        entities: Iterable[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        scored: list[dict[str, Any]] = []
        for entity in entities:
            entity_id = entity.get("entity_id")
            if not entity_id:
                continue
            friendly_name = entity.get("attributes", {}).get(
                "friendly_name", entity_id
            )
            domain = entity_id.split(".")[0] if "." in entity_id else "unknown"
            score = self._score_single_term(entity_id, friendly_name, domain, term_value)
            if score <= 0:
                continue
            scored.append(
                {
                    "entity_id": entity_id,
                    "friendly_name": friendly_name,
                    "score": score,
                }
            )

        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]

    def _score_single_term(
        self, entity_id: str, friendly_name: str, domain: str, term_value: str
    ) -> float:
        query = term_value.lower().strip()
        entity_lower = entity_id.lower()
        name_lower = friendly_name.lower()
        domain_lower = domain.lower()

        score = 0.0

        if query == entity_lower:
            score += 100
        elif query == name_lower:
            score += 95
        elif query == domain_lower:
            score += 90

        if query in entity_lower:
            score += 85
        if query in name_lower:
            score += 80

        entity_ratio = calculate_ratio(query, entity_lower)
        friendly_ratio = calculate_ratio(query, name_lower)
        domain_ratio = calculate_ratio(query, domain_lower)

        entity_partial = calculate_partial_ratio(query, entity_lower)
        friendly_partial = calculate_partial_ratio(query, name_lower)

        entity_token = calculate_token_sort_ratio(query, entity_lower)
        friendly_token = calculate_token_sort_ratio(query, name_lower)

        score += max(entity_ratio, entity_partial, entity_token) * 0.7
        score += max(friendly_ratio, friendly_partial, friendly_token) * 0.8
        score += domain_ratio * 0.6

        return float(score)

    def _build_detail_payload(
        self,
        context: PlaceholderContext,
        candidates: list[dict[str, Any]],
        decision: str | None,
        cached_entity_id: str | None,
    ) -> dict[str, Any]:
        return {
            "placeholder_id": context.id,
            "input_terms": [
                {
                    "term": term.value,
                    "weight": term.weight,
                    "raw_weight": term.raw_weight,
                }
                for term in context.normalized_terms
            ],
            "raw_terms": context.raw_terms,
            "min_confidence": {
                "ratio": context.threshold_ratio,
                "percent": context.threshold_percent,
                "source": context.threshold_source,
            },
            "limit": context.limit,
            "fallback_entity_id": context.fallback_entity_id,
            "candidates": candidates,
            "decision": decision or "elicitation_required",
            "cache_preference": cached_entity_id,
        }

    def _build_elicitation_payload(
        self,
        context: PlaceholderContext,
        candidates: list[dict[str, Any]],
        rounds_used: int,
    ) -> dict[str, Any]:
        candidate_summary = [
            {
                "entity_id": candidate["entity_id"],
                "friendly_name": candidate["friendly_name"],
                "score": candidate["score"],
                "raw_score": candidate.get("raw_score", candidate["score"]),
                "term_breakdown": candidate["term_breakdown"],
                "area_id": candidate.get("area_id"),
            }
            for candidate in candidates
        ]

        prompt = (
            f"Placeholder {context.id} did not reach the {context.threshold_ratio:.2f} "
            "confidence threshold."
        )
        if context.fallback_entity_id:
            prompt += f" Optional fallback: {context.fallback_entity_id}."

        return {
            "status": "needs_selection",
            "prompt": prompt,
            "placeholder_id": context.id,
            "threshold": {
                "ratio": context.threshold_ratio,
                "percent": context.threshold_percent,
            },
            "candidates": candidate_summary,
            "instructions": "Select a candidate, provide more weighted search terms, or cancel.",
            "allowed_responses": {
                "select": [candidate["entity_id"] for candidate in candidate_summary],
                "add_terms": [{"value": "string", "weight": "number"}],
                "cancel": True,
            },
            "rounds_used": rounds_used,
            "rounds_remaining": max(0, MAX_ELICITATION_ROUNDS - rounds_used),
        }


def register_quick_placeholder_script_tool(mcp: Any, client: Any, **_: Any) -> None:
    """Register the quick placeholder script execution tool with FastMCP."""

    executor = QuickPlaceholderScriptExecutor(client)

    @mcp.tool
    async def ha_quick_placeholder_script(
        script_id: Annotated[str, Field(description="Home Assistant script identifier")],
        placeholders: Annotated[
            list[dict[str, Any]] | None,
            Field(
                default=None,
                description=(
                    "Optional placeholder overrides. Each item may define 'id',\n"
                    "'search_terms', 'min_confidence', 'fallback_entity_id', and 'limit'."
                ),
            ),
        ] = None,
        script_args: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description="Additional script variables merged with resolved placeholders",
            ),
        ] = None,
        elicitation_state: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description="Opaque state returned from previous elicitation responses",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Resolve placeholders and execute a Home Assistant script in one call."""

        override_models: list[PlaceholderOverrideModel] | None = None
        if placeholders is not None:
            try:
                override_models = [PlaceholderOverrideModel.model_validate(item) for item in placeholders]
            except ValidationError as exc:
                return {
                    "status": "failed",
                    "error": "Invalid placeholder overrides",
                    "details": exc.errors(),
                }

        return await executor.execute(
            script_id=script_id,
            placeholders=override_models,
            script_args=script_args,
            elicitation_state=elicitation_state,
        )


__all__ = [
    "QuickPlaceholderScriptExecutor",
    "register_quick_placeholder_script_tool",
    "normalize_search_terms",
    "normalize_confidence",
    "is_obvious_match",
]

