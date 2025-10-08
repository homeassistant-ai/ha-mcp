"""
Fuzzy entity search utilities for Home Assistant MCP server.
"""

import logging
from typing import Any

from fuzzywuzzy import fuzz, process

logger = logging.getLogger(__name__)


class FuzzyEntitySearcher:
    """Advanced fuzzy entity search with AI-optimized scoring."""

    def __init__(self, threshold: int = 60):
        """Initialize with fuzzy matching threshold."""
        self.threshold = threshold
        self.entity_cache: dict[str, Any] = {}

    def search_entities(
        self, entities: list[dict[str, Any]], query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Search entities with fuzzy matching and intelligent scoring.

        Args:
            entities: List of Home Assistant entity states
            query: Search query (can be partial, with typos)
            limit: Maximum number of results

        Returns:
            List of matched entities with scores
        """
        if not query or not entities:
            return []

        matches = []
        query_lower = query.lower().strip()

        for entity in entities:
            entity_id = entity.get("entity_id", "")
            attributes = entity.get("attributes", {})
            friendly_name = attributes.get("friendly_name", entity_id)
            domain = entity_id.split(".")[0] if "." in entity_id else ""

            # Calculate comprehensive score
            score = self._calculate_entity_score(
                entity_id, friendly_name, domain, query_lower
            )

            if score >= self.threshold:
                matches.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": friendly_name,
                        "domain": domain,
                        "state": entity.get("state", "unknown"),
                        "attributes": attributes,
                        "score": score,
                        "match_type": self._get_match_type(
                            entity_id, friendly_name, domain, query_lower
                        ),
                    }
                )

        # Sort by score descending
        matches.sort(key=lambda x: x["score"], reverse=True)
        return matches[:limit]

    def _calculate_entity_score(
        self, entity_id: str, friendly_name: str, domain: str, query: str
    ) -> int:
        """Calculate comprehensive fuzzy score for an entity."""
        score = 0

        # Exact matches get highest scores
        if query == entity_id.lower():
            score += 100
        elif query == friendly_name.lower():
            score += 95
        elif query == domain.lower():
            score += 90

        # Partial exact matches
        if query in entity_id.lower():
            score += 85
        if query in friendly_name.lower():
            score += 80

        # Fuzzy matching scores
        entity_id_ratio = fuzz.ratio(query, entity_id.lower())
        friendly_ratio = fuzz.ratio(query, friendly_name.lower())
        domain_ratio = fuzz.ratio(query, domain.lower())

        # Partial ratio for substring matching
        entity_partial = fuzz.partial_ratio(query, entity_id.lower())
        friendly_partial = fuzz.partial_ratio(query, friendly_name.lower())

        # Token sort ratio for word order independence
        entity_token = fuzz.token_sort_ratio(query, entity_id.lower())
        friendly_token = fuzz.token_sort_ratio(query, friendly_name.lower())

        # Weight the scores
        score += max(entity_id_ratio, entity_partial, entity_token) * 0.7
        score += max(friendly_ratio, friendly_partial, friendly_token) * 0.8
        score += domain_ratio * 0.6

        # Room/area keyword boosting
        room_keywords = [
            "salon",
            "chambre",
            "cuisine",
            "salle",
            "living",
            "bedroom",
            "kitchen",
        ]
        for keyword in room_keywords:
            if keyword in query and keyword in friendly_name.lower():
                score += 15

        # Device type boosting
        device_keywords = [
            "light",
            "switch",
            "sensor",
            "climate",
            "lumiere",
            "interrupteur",
        ]
        for keyword in device_keywords:
            if keyword in query and (
                keyword in domain or keyword in friendly_name.lower()
            ):
                score += 10

        return int(score)

    def _get_match_type(
        self, entity_id: str, friendly_name: str, domain: str, query: str
    ) -> str:
        """Determine the type of match for user feedback."""
        if query == entity_id.lower():
            return "exact_id"
        elif query == friendly_name.lower():
            return "exact_name"
        elif query == domain.lower():
            return "exact_domain"
        elif query in entity_id.lower():
            return "partial_id"
        elif query in friendly_name.lower():
            return "partial_name"
        else:
            return "fuzzy_match"

    def search_by_area(
        self, entities: list[dict[str, Any]], area_query: str
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Group entities by area/room based on fuzzy matching.

        Args:
            entities: List of Home Assistant entity states
            area_query: Area/room name to search for

        Returns:
            Dictionary with area matches grouped by inferred area
        """
        area_matches: dict[str, list[dict[str, Any]]] = {}
        area_lower = area_query.lower().strip()

        for entity in entities:
            entity_id = entity.get("entity_id", "")
            attributes = entity.get("attributes", {})
            friendly_name = attributes.get("friendly_name", entity_id)

            # Check area_id attribute first
            if "area_id" in attributes:
                area_id = attributes["area_id"]
                if area_lower in area_id.lower():
                    if area_id not in area_matches:
                        area_matches[area_id] = []
                    area_matches[area_id].append(entity)
                    continue

            # Fuzzy match on friendly name for room inference
            area_score = fuzz.partial_ratio(area_lower, friendly_name.lower())
            if area_score >= self.threshold:
                inferred_area = self._infer_area_from_name(friendly_name)
                if inferred_area not in area_matches:
                    area_matches[inferred_area] = []
                area_matches[inferred_area].append(entity)

        return area_matches

    def _infer_area_from_name(self, friendly_name: str) -> str:
        """Infer area/room from entity friendly name."""
        name_lower = friendly_name.lower()

        # Common French room names
        french_rooms = {
            "salon": "salon",
            "chambre": "chambre",
            "cuisine": "cuisine",
            "salle": "salle_de_bain",
            "bureau": "bureau",
            "garage": "garage",
            "jardin": "jardin",
            "terrasse": "terrasse",
        }

        # Common English room names
        english_rooms = {
            "living": "living_room",
            "bedroom": "bedroom",
            "kitchen": "kitchen",
            "bathroom": "bathroom",
            "office": "office",
            "garage": "garage",
            "garden": "garden",
            "patio": "patio",
        }

        all_rooms = {**french_rooms, **english_rooms}

        for keyword, room in all_rooms.items():
            if keyword in name_lower:
                return room

        return "unknown_area"

    def get_smart_suggestions(
        self, entities: list[dict[str, Any]], query: str
    ) -> list[str]:
        """
        Generate smart suggestions for failed searches.

        Args:
            entities: List of Home Assistant entity states
            query: Original search query

        Returns:
            List of suggested search terms
        """
        suggestions = []

        # Extract unique domains
        domains = set()
        areas = set()

        for entity in entities:
            entity_id = entity.get("entity_id", "")
            if "." in entity_id:
                domains.add(entity_id.split(".")[0])

            friendly_name = entity.get("attributes", {}).get("friendly_name", "")
            inferred_area = self._infer_area_from_name(friendly_name)
            if inferred_area != "unknown_area":
                areas.add(inferred_area)

        # Fuzzy match against domains
        domain_matches = process.extract(query, domains, limit=3, scorer=fuzz.ratio)
        suggestions.extend([match[0] for match in domain_matches if match[1] >= 60])

        # Fuzzy match against areas
        area_matches = process.extract(query, areas, limit=3, scorer=fuzz.ratio)
        suggestions.extend([match[0] for match in area_matches if match[1] >= 60])

        # Add common search patterns
        if not suggestions:
            suggestions.extend(
                [
                    "light",
                    "switch",
                    "sensor",
                    "climate",
                    "salon",
                    "chambre",
                    "cuisine",
                    "living",
                    "bedroom",
                    "kitchen",
                ]
            )

        return suggestions[:5]


def create_fuzzy_searcher(threshold: int = 60) -> FuzzyEntitySearcher:
    """Create a new fuzzy entity searcher instance."""
    return FuzzyEntitySearcher(threshold)
