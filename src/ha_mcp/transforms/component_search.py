"""Advertise unified search when the connected component supports its contract."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ha_mcp._vendor.fastmcp.server.transforms import Transform
from ha_mcp._vendor.fastmcp.tools import Tool

from ..tools.component_api import component_supports, get_component_caps

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp.server.transforms import GetToolNext
    from ha_mcp._vendor.fastmcp.utilities.versions import VersionSpec

logger = logging.getLogger(__name__)

_UNIFIED_CAPABILITIES = ("search", "device_registry_child_semantics", "search_unified")
_LEGACY_PARAMETERS = frozenset({"search_types", "include_config", "config_time_budget"})
_DESCRIPTION = (
    "Search Home Assistant entities and configuration contents.\n\n"
    "Use dedicated get/list tools first when the resource type is known: "
    "ha_config_get_scene lists or searches scenes, ha_config_get_dashboard "
    "lists or reads dashboards (mode='search' searches across their contents), "
    "and ha_config_get_automation, "
    "ha_config_get_script, and ha_config_list_helpers handle their own types.\n\n"
    "Use ha_search for broader discovery and deep searches across entities, "
    "automations, scripts, scenes, and helpers. Pass an exact "
    "entity_id to discover references before a rename or deletion. Omit query "
    "to enumerate entities using domain, area, or state filters. "
    "To search full stored scene attribute values, use "
    "ha_config_get_scene(query=..., search_in_config=True).\n\n"
    "Results are paginated; partial results are incomplete, not proof that "
    "no matches exist. Inspect warnings and partial_reason. Read full "
    "configurations with the corresponding get tool and current entity state "
    "with ha_get_state. For controls with exclusions, request is_group and "
    "member_entity_ids in result_fields and avoid aggregates whose membership "
    "cannot be verified."
)
_QUERY_DESCRIPTION = (
    "Entity name fragment, free-text configuration term, or exact entity_id "
    "for broader discovery and deep search. Use dedicated get/list tools "
    "first for a known resource type. Use an exact entity_id for reference "
    "checks before a rename or deletion. Omit to enumerate entities by "
    "domain_filter, area_filter, and/or state_filter."
)


class ComponentSearchSchemaTransform(Transform):
    """Trim advertised metadata while retaining the original callable signature.

    FunctionTool validates arguments against its function, not ``parameters``.
    Copying the advertised schema leaves cached schemas and old-client calls
    intact. The shared capability cache remains scoped to the HA client.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _unified_search_available(self) -> bool:
        try:
            caps = await get_component_caps(self._client)
        except Exception:
            # Catalog discovery is best-effort; a failed probe must not hide
            # the usable legacy contract or prevent listing unrelated tools.
            logger.debug("Search schema capability probe failed", exc_info=True)
            return False
        return all(component_supports(caps, name) for name in _UNIFIED_CAPABILITIES)

    @staticmethod
    def _rewrite(tool: Tool) -> Tool:
        parameters = deepcopy(tool.parameters)
        properties = parameters.get("properties", {})
        for name in _LEGACY_PARAMETERS:
            properties.pop(name, None)
        if "required" in parameters:
            parameters["required"] = [
                name
                for name in parameters["required"]
                if name not in _LEGACY_PARAMETERS
            ]
        if "query" in properties:
            properties["query"]["description"] = _QUERY_DESCRIPTION
        return tool.model_copy(
            update={"parameters": parameters, "description": _DESCRIPTION}
        )

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        if not any(tool.name == "ha_search" for tool in tools):
            return tools
        if not await self._unified_search_available():
            return tools
        return [self._rewrite(t) if t.name == "ha_search" else t for t in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        if tool is None or tool.name != "ha_search":
            return tool
        return self._rewrite(tool) if await self._unified_search_available() else tool
