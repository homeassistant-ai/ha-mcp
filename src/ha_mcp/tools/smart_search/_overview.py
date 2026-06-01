"""AI-friendly system overview with intelligent categorization."""

import asyncio
import logging
import random
from typing import Any

from ..helpers import exception_to_structured_error
from ._base import _SearchBase
from ._config import _simplify_states_summary

logger = logging.getLogger(__name__)


class SystemOverviewMixin(_SearchBase):
    """``get_system_overview`` and its analysis/format/paginate helpers."""

    async def get_system_overview(
        self,
        detail_level: str = "standard",
        max_entities_per_domain: int | None = None,
        include_state: bool | None = None,
        include_entity_id: bool | None = None,
        domains_filter: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        Get AI-friendly system overview with intelligent categorization.

        Args:
            detail_level: Level of detail to return:
                - "minimal": 10 entities/domain sample, top-5 states (friendly_name only)
                - "standard": ALL entities, top-10 states (friendly_name only)
                - "full": ALL entities with entity_id + friendly_name + state + full states
            max_entities_per_domain: Override default entity cap (0 = no limit)
            include_state: Override whether to include state field
            include_entity_id: Override whether to include entity_id field
            domains_filter: Only include these domains (None = all)
            limit: Max total entities to include across all domains.
                Defaults to None (no limit) for minimal, 200 for standard/full.
                Domain counts and states_summary are always complete regardless.
            offset: Number of entities to skip for pagination (default: 0)

        Returns:
            System overview optimized for AI understanding at requested detail level
        """
        try:
            # Fetch all data in parallel. return_exceptions=True so a degraded
            # registry/service fetch doesn't abort the whole overview.
            results = await asyncio.gather(
                self.client.get_states(),
                self.client.get_services(),
                self.client.send_websocket_message(
                    {"type": "config/area_registry/list"}
                ),
                self.client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                ),
                self.client.send_websocket_message(
                    {"type": "config/device_registry/list"}
                ),
                return_exceptions=True,
            )

            # Entities are mandatory — surface connection/auth errors immediately.
            if isinstance(results[0], Exception):
                raise results[0]
            entities = results[0]

            # Services failure affects total count + catalog; log at warning.
            partial_warnings: list[str] = []
            if isinstance(results[1], Exception):
                logger.warning(f"Could not fetch services: {results[1]}")
                partial_warnings.append(f"Services unavailable: {results[1]}")
                services: Any = []
            else:
                services = results[1]

            # Registry failures degrade area enrichment only; logged at debug.
            area_registry = self._extract_registry_list(results[2], "area registry")
            entity_registry = self._extract_registry_list(results[3], "entity registry")
            device_registry = self._extract_registry_list(results[4], "device registry")
            entity_area_map = self._build_entity_area_map(
                entity_registry, device_registry
            )

            (
                max_entities_per_domain,
                uncap_all,
                include_state,
                include_entity_id,
            ) = self._resolve_overview_display_opts(
                detail_level, max_entities_per_domain, include_state, include_entity_id
            )

            # Pre-populate area_stats so empty areas still appear
            area_stats = self._init_area_stats(area_registry)

            domains_filter_set: set[str] | None = None
            if domains_filter:
                domains_filter_set = {d.strip().lower() for d in domains_filter}

            # Count all domains before filtering (for system_summary)
            all_domains = {e["entity_id"].split(".")[0] for e in entities}

            domain_stats, device_types = self._analyze_entities_by_domain(
                entities,
                domains_filter_set,
                area_stats,
                entity_area_map,
                include_state,
                include_entity_id,
            )

            sorted_domains = sorted(
                domain_stats.items(), key=lambda x: x[1]["count"], reverse=True
            )
            service_stats, total_services = self._build_service_stats(services)
            ai_insights = self._build_ai_insights(domain_stats, sorted_domains)
            formatted_domain_stats = self._format_domain_stats(
                sorted_domains, max_entities_per_domain, detail_level, uncap_all
            )
            pagination_metadata = self._paginate_overview_entities(
                formatted_domain_stats, limit, offset, detail_level
            )

            # totals always reflect the full system, regardless of filtering
            system_summary: dict[str, Any] = {
                "total_entities": len(entities),
                "total_domains": len(all_domains),
                "total_services": total_services,
                "total_areas": len(area_registry),
            }
            if domains_filter_set:
                system_summary["filtered_domains"] = sorted(domains_filter_set)

            return self._assemble_overview_response(
                system_summary=system_summary,
                formatted_domain_stats=formatted_domain_stats,
                area_stats=area_stats,
                ai_insights=ai_insights,
                detail_level=detail_level,
                pagination_metadata=pagination_metadata,
                partial_warnings=partial_warnings,
                device_types=device_types,
                service_stats=service_stats,
            )

        except Exception as e:
            logger.error(f"Error in get_system_overview: {e}")
            exception_to_structured_error(
                e,
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify API token permissions",
                    "Try test_connection first",
                ],
                context={
                    "total_entities": 0,
                    "entity_summary": {},
                    "controllable_devices": {},
                },
            )

    @staticmethod
    def _build_entity_area_map(
        entity_registry: list[dict[str, Any]],
        device_registry: list[dict[str, Any]],
    ) -> dict[str, str | None]:
        """Map entity_id -> area_id. Priority: entity direct area_id > device area_id."""
        device_area_map: dict[str, str | None] = {}
        for device in device_registry:
            device_id = device.get("id", "")
            if device_id:
                device_area_map[device_id] = device.get("area_id")

        entity_area_map: dict[str, str | None] = {}
        for entry in entity_registry:
            entity_id = entry.get("entity_id")
            area_id = entry.get("area_id")
            if not area_id:
                device_id = entry.get("device_id")
                if device_id:
                    area_id = device_area_map.get(device_id)
            if entity_id:
                entity_area_map[entity_id] = area_id
        return entity_area_map

    @staticmethod
    def _resolve_overview_display_opts(
        detail_level: str,
        max_entities_per_domain: int | None,
        include_state: bool | None,
        include_entity_id: bool | None,
    ) -> tuple[int | None, bool, bool, bool]:
        """Resolve detail-level display defaults.

        ``max_entities_per_domain == 0`` means "uncap everything" (entities +
        states). standard/full keep no default cap (None = all entities).
        """
        uncap_all = max_entities_per_domain == 0
        if max_entities_per_domain is None and detail_level == "minimal":
            max_entities_per_domain = 10
        if include_state is None:
            include_state = detail_level == "full"
        if include_entity_id is None:
            include_entity_id = detail_level == "full"
        return max_entities_per_domain, uncap_all, include_state, include_entity_id

    @staticmethod
    def _init_area_stats(
        area_registry: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Pre-populate per-area stats so areas with no entities still appear."""
        area_stats: dict[str, dict[str, Any]] = {}
        for area in area_registry:
            area_id = area.get("area_id", "")
            if area_id:
                area_stats[area_id] = {
                    "name": area.get("name", area_id),
                    "count": 0,
                    "domains": {},
                }
        return area_stats

    @staticmethod
    def _record_entity_area(
        area_stats: dict[str, dict[str, Any]],
        entity_area_map: dict[str, str | None],
        entity_id: str,
        domain: str,
    ) -> None:
        """Increment per-area + per-area-domain counts for one entity."""
        area_id = entity_area_map.get(entity_id)
        if area_id and area_id in area_stats:
            area_stats[area_id]["count"] += 1
            domains = area_stats[area_id]["domains"]
            domains[domain] = domains.get(domain, 0) + 1

    @staticmethod
    def _record_device_type(
        device_types: dict[str, int], attributes: dict[str, Any]
    ) -> None:
        """Increment the device_class tally for one entity, if it has one."""
        device_class = attributes.get("device_class")
        if device_class:
            device_types[device_class] = device_types.get(device_class, 0) + 1

    def _analyze_entities_by_domain(
        self,
        entities: list[dict[str, Any]],
        domains_filter_set: set[str] | None,
        area_stats: dict[str, dict[str, Any]],
        entity_area_map: dict[str, str | None],
        include_state: bool,
        include_entity_id: bool,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
        """Tally per-domain stats, area stats (mutated in place), and device types."""
        domain_stats: dict[str, dict[str, Any]] = {}
        device_types: dict[str, int] = {}

        for entity in entities:
            entity_id = entity["entity_id"]
            domain = entity_id.split(".")[0]
            if domains_filter_set and domain not in domains_filter_set:
                continue

            attributes = entity.get("attributes", {})
            state = entity.get("state", "unknown")

            stats = domain_stats.setdefault(
                domain, {"count": 0, "states_summary": {}, "all_entities": []}
            )
            stats["count"] += 1
            stats["states_summary"][state] = stats["states_summary"].get(state, 0) + 1

            entity_data: dict[str, Any] = {
                "friendly_name": attributes.get("friendly_name", entity_id),
            }
            if include_entity_id:
                entity_data["entity_id"] = entity_id
            if include_state:
                entity_data["state"] = state
            stats["all_entities"].append(entity_data)

            self._record_entity_area(area_stats, entity_area_map, entity_id, domain)
            self._record_device_type(device_types, attributes)

        return domain_stats, device_types

    @staticmethod
    def _build_service_stats(
        services: Any,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        """Summarize the service catalog into per-domain counts and a grand total."""
        service_stats: dict[str, dict[str, Any]] = {}
        total_services = 0
        if isinstance(services, list):
            for domain_obj in services:
                domain = domain_obj.get("domain", "unknown")
                domain_services = domain_obj.get("services", {})
                service_stats[domain] = {
                    "count": len(domain_services),
                    "services": list(domain_services.keys()),
                }
                total_services += len(domain_services)
        return service_stats, total_services

    @staticmethod
    def _build_ai_insights(
        domain_stats: dict[str, dict[str, Any]],
        sorted_domains: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        """Derive coarse AI-facing hints (common/controllable/monitoring domains)."""
        return {
            "most_common_domains": [domain for domain, _ in sorted_domains[:5]],
            "controllable_devices": [
                domain
                for domain in domain_stats
                if domain in ["light", "switch", "climate", "media_player", "cover"]
            ],
            "monitoring_sensors": [
                domain
                for domain in domain_stats
                if domain in ["sensor", "binary_sensor", "camera"]
            ],
            "automation_ready": "automation" in domain_stats
            and domain_stats["automation"]["count"] > 0,
        }

    @staticmethod
    def _format_domain_stats(
        sorted_domains: list[tuple[str, dict[str, Any]]],
        max_entities_per_domain: int | None,
        detail_level: str,
        uncap_all: bool,
    ) -> dict[str, dict[str, Any]]:
        """Apply the per-domain entity cap and simplify state summaries."""
        formatted_domain_stats: dict[str, dict[str, Any]] = {}
        for domain, stats in sorted_domains:
            all_entities = stats["all_entities"]
            if max_entities_per_domain and len(all_entities) > max_entities_per_domain:
                if detail_level == "minimal":
                    # Random sample so minimal isn't biased to early entities
                    selected_entities = random.sample(
                        all_entities, max_entities_per_domain
                    )
                else:
                    selected_entities = all_entities[:max_entities_per_domain]
                truncated = True
            else:
                selected_entities = all_entities
                truncated = False

            formatted_domain_stats[domain] = {
                "count": stats["count"],
                "states_summary": _simplify_states_summary(
                    stats["states_summary"],
                    "full" if uncap_all else detail_level,
                ),
                "entities": selected_entities,
                "truncated": truncated,
            }
        return formatted_domain_stats

    @staticmethod
    def _allocate_page_one(
        formatted_domain_stats: dict[str, dict[str, Any]],
        effective_limit: int,
        total_entity_count: int,
    ) -> int:
        """Distribute the page-1 budget: a min allocation per domain, rest proportional.

        Gives each domain a minimum slice so the LLM sees entities from every
        domain, then distributes the remaining budget proportionally. Mutates
        ``formatted_domain_stats`` in place; returns the count included.
        """
        min_per_domain = 3
        num_domains = len(formatted_domain_stats)
        reserved = min(min_per_domain * num_domains, effective_limit)
        remaining_budget = effective_limit - reserved

        entities_included = 0
        for domain_data in formatted_domain_stats.values():
            domain_entities = domain_data["entities"]
            domain_len = len(domain_entities)
            base = min(min_per_domain, domain_len)
            if total_entity_count > 0 and remaining_budget > 0:
                extra = int(remaining_budget * domain_len / total_entity_count)
            else:
                extra = 0
            take = min(base + extra, domain_len)
            if take < domain_len:
                domain_data["entities"] = domain_entities[:take]
                domain_data["truncated"] = True
            entities_included += len(domain_data["entities"])
        return entities_included

    @staticmethod
    def _allocate_subsequent_pages(
        formatted_domain_stats: dict[str, dict[str, Any]],
        effective_limit: int,
        offset: int,
    ) -> int:
        """Apply pages-2+ sequential skip/take across domains. Mutates in place."""
        entities_skipped = 0
        entities_included = 0
        for domain_data in formatted_domain_stats.values():
            domain_entities = domain_data["entities"]
            domain_len = len(domain_entities)

            skip_from_domain = max(0, min(domain_len, offset - entities_skipped))
            budget_left = effective_limit - entities_included
            take_from_domain = max(0, min(domain_len - skip_from_domain, budget_left))

            if skip_from_domain > 0 or take_from_domain < domain_len:
                domain_data["entities"] = domain_entities[
                    skip_from_domain : skip_from_domain + take_from_domain
                ]
                if take_from_domain < domain_len:
                    domain_data["truncated"] = True

            entities_skipped += skip_from_domain
            entities_included += take_from_domain
        return entities_included

    def _paginate_overview_entities(
        self,
        formatted_domain_stats: dict[str, dict[str, Any]],
        limit: int | None,
        offset: int,
        detail_level: str,
    ) -> dict[str, Any] | None:
        """Apply global entity pagination across domains; returns metadata or None.

        Default limit: None for minimal (already capped per-domain), 200 for
        standard/full. Domain counts/states_summary stay complete regardless.
        """
        effective_limit = limit
        if effective_limit is None and detail_level != "minimal":
            effective_limit = 200
        if effective_limit is None:
            return None

        total_entity_count = sum(
            len(ds["entities"]) for ds in formatted_domain_stats.values()
        )
        if offset == 0:
            entities_included = self._allocate_page_one(
                formatted_domain_stats, effective_limit, total_entity_count
            )
        else:
            entities_included = self._allocate_subsequent_pages(
                formatted_domain_stats, effective_limit, offset
            )

        has_more = (offset + entities_included) < total_entity_count
        return {
            "total_entity_results": total_entity_count,
            "offset": offset,
            "limit": effective_limit,
            "entities_returned": entities_included,
            "has_more": has_more,
            "next_offset": offset + effective_limit if has_more else None,
        }

    @staticmethod
    def _assemble_overview_response(
        *,
        system_summary: dict[str, Any],
        formatted_domain_stats: dict[str, dict[str, Any]],
        area_stats: dict[str, dict[str, Any]],
        ai_insights: dict[str, Any],
        detail_level: str,
        pagination_metadata: dict[str, Any] | None,
        partial_warnings: list[str],
        device_types: dict[str, int],
        service_stats: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Assemble the final overview response and attach level-specific fields."""
        base_response: dict[str, Any] = {
            "success": True,
            "system_summary": system_summary,
            "domain_stats": formatted_domain_stats,
            "area_analysis": (
                {area: {"count": info["count"]} for area, info in area_stats.items()}
                if detail_level == "minimal"
                else area_stats
            ),
            "ai_insights": ai_insights,
        }

        if pagination_metadata:
            base_response["pagination"] = pagination_metadata

        if partial_warnings:
            base_response["partial"] = True
            base_response["warnings"] = partial_warnings

        # Full: add device types and service catalog
        if detail_level == "full":
            base_response["device_types"] = device_types
            base_response["service_availability"] = service_stats

        return base_response
