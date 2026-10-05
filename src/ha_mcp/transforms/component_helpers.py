"""Advertise Core's own simple-helper fields when the component serves helper writes."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ha_mcp._vendor.fastmcp.server.transforms import Transform
from ha_mcp._vendor.fastmcp.tools import Tool

from ..tools.component_api import component_supports, get_component_caps
from ..tools.component_helper_collections import (
    HELPER_CAPABILITIES,
    fetch_helper_schemas,
)
from ..tools.config_helpers.schemas import (
    _SIMPLE_CONFIG_KEYS_DESCRIPTION,
    SIMPLE_HELPER_TYPES,
)

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp.server.transforms import GetToolNext
    from ha_mcp._vendor.fastmcp.utilities.versions import VersionSpec

logger = logging.getLogger(__name__)

_TOOL = "ha_config_set_helper"
# Rendering order; input_button has no fields beyond name/icon.
_ORDER = (
    "input_select",
    "input_number",
    "input_text",
    "input_datetime",
    "input_boolean",
    "counter",
    "timer",
    "schedule",
    "zone",
    "person",
    "tag",
)
_WEEKDAYS = frozenset(
    {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
)
# Semantics Core's schemas don't carry.
_NOTES = (
    "On input_* types `initial` — even false/0 — disables last-state restore and "
    "forces that value on every HA restart; counter `initial` is the starting "
    "value. input_text min/max are lengths. input_select options is a list. An "
    "input_datetime created with neither has_date nor has_time gets both. timer "
    "duration is 'HH:MM:SS' or seconds. Schedule days are lists of "
    "{'from': 'HH:MM', 'to': 'HH:MM'} with an optional 'data' dict. zone radius "
    "is in meters; a passive zone doesn't trigger person state changes. person "
    "device_trackers is a list of device_tracker entity IDs. Omit tag_id on "
    "create to auto-generate it."
)


def _render_field(field: dict[str, Any]) -> str:
    options = field.get("options")
    kind = (
        "|".join(str(o[0] if isinstance(o, list | tuple) else o) for o in options)
        if options
        else field.get("type")
    )
    parts = [str(kind)] if kind else []
    if field.get("required"):
        parts.append("required")
    if field.get("default") is not None:
        parts.append(f"default {field['default']}")
    return f"{field['name']} ({', '.join(parts)})" if parts else str(field["name"])


def render_core_keys(schemas: dict[str, Any]) -> str:
    """The SIMPLE-type key list, from Core's create schemas."""
    lines = []
    for helper_type in _ORDER:
        fields = [
            f
            for f in schemas[helper_type]["create"]
            if f.get("name") not in ("name", "icon")
        ]
        rendered = [_render_field(f) for f in fields if f.get("name") not in _WEEKDAYS]
        # Core builds schedule's day keys from a set; one entry keeps this stable.
        if any(f.get("name") in _WEEKDAYS for f in fields):
            rendered.append("monday..sunday")
        if rendered:
            lines.append(f"{helper_type}: {', '.join(rendered)}.")
    return f"{' '.join(lines)} {_NOTES}"


class ComponentHelperSchemaTransform(Transform):
    """Swap the static helper key list for Core's.

    ``wait`` stays: flow helpers and a write that falls back to Core's
    commands still poll for the entity.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _core_schemas(self) -> dict[str, Any] | None:
        try:
            caps = await get_component_caps(self._client)
            if not all(component_supports(caps, c) for c in HELPER_CAPABILITIES):
                return None
            schemas = await fetch_helper_schemas(self._client)
        except Exception:
            # Catalog discovery is best-effort; keep the static contract.
            logger.debug("Helper schema capability probe failed", exc_info=True)
            return None
        if not schemas or not schemas.keys() >= SIMPLE_HELPER_TYPES:
            return None
        return schemas

    @staticmethod
    def _rewrite(tool: Tool, schemas: dict[str, Any]) -> Tool:
        parameters = deepcopy(tool.parameters)
        properties = parameters.get("properties", {})
        config = properties.get("config")
        if config and _SIMPLE_CONFIG_KEYS_DESCRIPTION in config.get("description", ""):
            config["description"] = config["description"].replace(
                _SIMPLE_CONFIG_KEYS_DESCRIPTION, render_core_keys(schemas)
            )
        return tool.model_copy(update={"parameters": parameters})

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        if not any(tool.name == _TOOL for tool in tools):
            return tools
        schemas = await self._core_schemas()
        if schemas is None:
            return tools
        return [self._rewrite(t, schemas) if t.name == _TOOL else t for t in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        if tool is None or tool.name != _TOOL:
            return tool
        schemas = await self._core_schemas()
        return tool if schemas is None else self._rewrite(tool, schemas)
