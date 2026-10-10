"""
Configuration management tools for Home Assistant zones.

This module provides tools for listing, creating/updating, and removing
Home Assistant zones (location-based areas for presence automation).
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response, create_validation_error
from .auto_backup import with_auto_backup
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .config_helpers.create import _execute_create_simple_helper
from .config_helpers.update import _execute_update_simple_helper
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
    ws_failure_code,
)
from .tool_hints import read_only_hints, write_hints
from .ws_waiters import wait_for_entity_removed

logger = logging.getLogger(__name__)


def _build_zone_result(
    zones: list[dict[str, Any]], zone_id: str | None
) -> dict[str, Any]:
    """Assemble the ha_get_zone response from a list of zone records.

    Shared by the legacy ``zone/list`` path and the component ``helpers_list``
    path so the two produce an identical envelope. Without ``zone_id`` returns
    the full list; with one, returns that single zone or raises
    RESOURCE_NOT_FOUND.
    """
    if zone_id is None:
        return {
            "success": True,
            "count": len(zones),
            "zones": zones,
            "message": f"Found {len(zones)} zone(s)",
        }

    zone = next((z for z in zones if z.get("id") == zone_id), None)
    if zone is None:
        available_ids = [z.get("id") for z in zones[:10]]  # Show first 10
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Zone not found: {zone_id}",
                context={
                    "zone_id": zone_id,
                    "available_zone_ids": available_ids,
                },
                suggestions=[
                    "Use ha_get_zone() without zone_id to see all available zones"
                ],
            )
        )
    return {
        "success": True,
        "zone_id": zone_id,
        "zone": zone,
    }


def _shape_component_zone_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Map one component ``helpers_list`` zone record onto the legacy zone row.

    The component supplies each zone entity's config as ``config``: the stored
    body for a storage zone, and the YAML or core-configuration body for the
    YAML and home zones that ``zone/list`` omits. The row is that body plus
    ``entity_id`` and an ``editable`` / ``source`` discriminator.
    """
    config = rec.get("config")
    out: dict[str, Any] = dict(config) if isinstance(config, dict) else {}
    # The component reads each zone entity's _config, so a zone outside the
    # storage (the home zone, a YAML zone) shows as one without a storage id.
    storage_id = rec.get("storage_id")
    is_yaml = storage_id is None
    # Storage zones keep their storage id (the key ha_get_zone matches on);
    # YAML zones get their object_id so they can still be fetched by zone_id.
    out["id"] = storage_id if storage_id is not None else rec.get("object_id")
    out["entity_id"] = rec.get("entity_id")
    out["editable"] = not is_yaml
    out["source"] = "yaml" if is_yaml else "storage"
    return out


def _shape_component_zone_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Reshape the component's zone ``helpers_list`` result into legacy rows."""
    raw = result.get("helpers")
    records = raw if isinstance(raw, list) else []
    return [
        _shape_component_zone_record(rec)
        for rec in records
        if isinstance(rec, dict) and rec.get("helper_type") == "zone"
    ]


class ZoneTools:
    """Zone configuration management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_zone",
        tags={"Zones"},
        annotations=read_only_hints("Get Zone", open_world=False),
    )
    @log_tool_usage
    async def ha_get_zone(
        self,
        zone_id: Annotated[
            str | None,
            Field(
                description="Zone ID to get details for (from ha_get_zone() list).",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get zone information - list all zones or get details for a specific one.

        Without a zone_id: Lists all Home Assistant zones with their coordinates and radius.
        With a zone_id: Returns detailed configuration for a specific zone.

        EXAMPLES:
        - List all zones: ha_get_zone()
        - Get specific zone: ha_get_zone(zone_id="abc123")

        YAML-defined zones, including the 'home' zone, are listed with
        ``editable=false`` / ``source="yaml"``; zones created in the UI or by
        ha_set_zone are ``source="storage"``. Each zone carries its entity_id.
        """
        try:
            # Prefer the ha_mcp_tools component's helpers_list, one call for
            # storage and YAML zones. Without it, Core's zone/list (storage
            # only) is joined with the registry and the YAML zones' states;
            # the fallback taxonomy lives in ``_get_zone_via_component``.
            caps = await get_component_caps(self._client)
            if component_supports(caps, "helpers_list"):
                component_response = await self._get_zone_via_component(zone_id)
                if component_response is not None:
                    return component_response

            return await self._legacy_zone_result(zone_id)

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error getting zone(s) (zone_id={zone_id}): {e}")
            exception_to_structured_error(
                e,
                context={"zone_id": zone_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                    "Use ha_get_zone() without zone_id to see all available zones",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _get_zone_via_component(
        self, zone_id: str | None
    ) -> dict[str, Any] | None:
        """Serve ha_get_zone from the component's helpers_list; ``None`` ⇒ legacy.

        Error taxonomy mirrors ``_list_helpers_via_component`` in
        tools_config_helpers (§ 4). ``zone`` is always in the collection
        universe, so there is a legacy fallback for every failure:

        - ``unknown_command`` (cached caps went stale after a component
          downgrade): invalidate the caps and return ``None`` so the caller
          serves the legacy listing, silently.
        - any other ``HomeAssistantCommandError`` / ``HomeAssistantCommandTimeout``:
          serve the correct result from the legacy zone list, append a
          ``warnings[]`` entry, and ``log.warning``.
        - a response that does not authoritatively enumerate ``zone`` (an older
          component with no ``covered_types``): fall back to legacy silently.
        - ``HomeAssistantConnectionError`` (pooled-WS drop) or the plain
          ``Exception`` ``get_websocket_client()`` raises on a failed (re)connect:
          served from the legacy ``zone/list`` (which rides the swallowing
          ``send_websocket_message`` bridge, so a transport failure surfaces there
          as a structured error rather than dying identically), with a
          ``warnings[]`` entry + ``log.warning``; a transport failure must not
          escape.
        """
        try:
            raw = await self._send_component_zone_list()
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
                return None
            response = await self._legacy_zone_result(zone_id)
            response.setdefault("warnings", []).append(
                f"component zone listing failed ({exc}); served via legacy path"
            )
            logger.warning(
                "ha_mcp_tools/helpers_list (zone) failed; fell back to legacy: %r",
                exc,
            )
            return response
        except Exception as exc:  # noqa: BLE001
            response = await self._legacy_zone_result(zone_id)
            response.setdefault("warnings", []).append(
                f"component zone listing connection error ({exc}); "
                "served via legacy path"
            )
            logger.warning(
                "ha_mcp_tools/helpers_list (zone) connection error; "
                "fell back to legacy: %r",
                exc,
            )
            return response
        result = raw.get("result") or {}
        covered = result.get("covered_types")
        if not (isinstance(covered, list) and "zone" in covered):
            # The component did not authoritatively enumerate zones (older
            # component with no covered_types): don't trust its list — fall back
            # to the legacy listing silently.
            return None
        return _build_zone_result(_shape_component_zone_rows(result), zone_id)

    async def _send_component_zone_list(self) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/helpers_list`` zone query over the per-client WS."""
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        return await ws.send_command(
            "ha_mcp_tools/helpers_list",
            helper_types=["zone"],
            include_flow_helpers=False,
        )

    async def _legacy_zone_result(self, zone_id: str | None) -> dict[str, Any]:
        """Serve ha_get_zone from Core's commands, with a warning per missing part."""
        rows, warnings = await self._legacy_zone_rows()
        response = _build_zone_result(rows, zone_id)
        if warnings:
            response["warnings"] = warnings
        return response

    async def _legacy_zone_rows(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Storage zones from Core's ``zone/list`` plus the YAML zones it omits.

        Rows match the component's: the stored body or, for the home zone and
        YAML zones, their state attributes, plus ``entity_id``, ``editable`` and
        ``source``. The registry links a storage zone to its entity_id (its
        ``unique_id`` is the zone_id); every other zone state is not stored.
        """
        result = await self._client.send_websocket_message({"type": "zone/list"})
        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    result.get("error", "Failed to get zones"),
                    context={},
                )
            )
        warnings: list[str] = []
        entity_ids: dict[str, str] = {}
        try:
            entity_ids = {
                e["unique_id"]: e["entity_id"]
                for e in await self._zone_registry_entries()
                if e.get("unique_id")
            }
        except ToolError as exc:
            warnings.append(f"Zone entity_ids are missing: {exc}")
        rows = [
            {
                **item,
                "entity_id": entity_ids.get(item.get("id", "")),
                "editable": True,
                "source": "storage",
            }
            for item in result.get("result") or []
            if isinstance(item, dict)
        ]
        try:
            states = await self._client.get_states()
        except (HomeAssistantAPIError, HomeAssistantConnectionError) as exc:
            warnings.append(f"YAML-defined zones such as 'home' are missing: {exc}")
            states = []
        if warnings:
            # Without the registry no state can be told apart from a stored zone.
            return rows, warnings
        storage_ids = {v: k for k, v in entity_ids.items()}
        stored = {row["entity_id"] for row in rows}
        for state in states:
            entity_id = state.get("entity_id", "") if isinstance(state, dict) else ""
            if not entity_id.startswith("zone.") or entity_id in stored:
                continue
            attrs = state.get("attributes") or {}
            rows.append(
                {
                    **attrs,
                    "id": storage_ids.get(entity_id, entity_id.split(".", 1)[1]),
                    "entity_id": entity_id,
                    "editable": False,
                    "source": "yaml",
                }
            )
        return rows, warnings

    async def _zone_registry_entries(self) -> list[dict[str, Any]]:
        """The zone integration's entity registry entries."""
        listed = await self._client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
        if not listed.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Could not read the entity registry to find the zone: "
                    f"{listed.get('error', 'Unknown error')}",
                )
            )
        return [
            e
            for e in listed.get("result") or []
            if isinstance(e, dict) and e.get("platform") == "zone"
        ]

    async def _zone_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            state = await self._client.get_entity_state(entity_id)
        except HomeAssistantAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        return state if isinstance(state, dict) else None

    async def _resolve_zone(self, zone_id: str) -> tuple[str, str]:
        """The entity_id and storage zone_id of an editable zone, given either.

        Core's zone commands take the storage id, which is the registry
        ``unique_id`` of the zone's entity. The home zone and YAML zones are not
        in that storage, and Core gives them no ``unique_id``.
        """
        entity_id = zone_id if zone_id.startswith("zone.") else f"zone.{zone_id}"
        entries = await self._zone_registry_entries()
        entry = next(
            (e for e in entries if e.get("unique_id") == zone_id), None
        ) or next((e for e in entries if e.get("entity_id") == entity_id), None)
        if entry is not None:
            entity_id = entry["entity_id"]
        state = await self._zone_state(entity_id)
        stored = entry is not None and bool(entry.get("unique_id"))
        if state is not None and not stored:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Zone {entity_id} is not a stored zone: it is defined in YAML "
                    "or, for zone.home, by the home location in the general "
                    "settings, so the zone tools cannot change or remove it.",
                    context={"zone_id": zone_id, "entity_id": entity_id},
                    suggestions=[
                        "Edit a YAML zone in configuration.yaml",
                        "Change the home location under Settings > System > General",
                    ],
                )
            )
        if entry is None or not stored:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Zone not found: {zone_id}",
                    context={"zone_id": zone_id},
                    suggestions=[
                        "Use ha_get_zone() without zone_id to see all available zones"
                    ],
                )
            )
        return entity_id, entry["unique_id"]

    @tool(
        name="ha_set_zone",
        tags={"Zones"},
        annotations=write_hints(
            "Set Zone", destructive=True, idempotent=False, open_world=False
        ),
    )
    @with_auto_backup(
        domain="zone",
        id_fn=lambda kw: str(kw.get("zone_id") or kw.get("name") or ""),
    )
    @log_tool_usage
    async def ha_set_zone(
        self,
        name: Annotated[
            str | None,
            Field(
                description="Display name for the zone",
                default=None,
            ),
        ] = None,
        latitude: Annotated[
            float | None,
            Field(
                description="Latitude coordinate of the zone center",
                default=None,
            ),
        ] = None,
        longitude: Annotated[
            float | None,
            Field(
                description="Longitude coordinate of the zone center",
                default=None,
            ),
        ] = None,
        zone_id: Annotated[
            str | None,
            Field(
                description="Zone ID or entity_id of the zone to update (from ha_get_zone)",
                default=None,
            ),
        ] = None,
        radius: Annotated[
            float | None,
            Field(
                description="Radius of the zone in meters; Home Assistant uses 100 when it is omitted on create",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:briefcase'). On update, '' clears it, except an icon stored in the zone itself (as the Home Assistant UI stores it; ha_get_zone shows it), which cannot be removed",
                default=None,
            ),
        ] = None,
        passive: Annotated[
            bool | None,
            Field(
                description="Passive mode: the zone is hidden in the frontend and not used for device tracker state, but automations can still use it (false when omitted on create)",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create or update a Home Assistant zone.

        Omit zone_id to create a new zone (name, latitude, longitude required).
        Provide zone_id to update an existing zone (only specified fields change).
        Zones defined in YAML, and the home zone, cannot be changed here.

        EXAMPLES:
        - Create: ha_set_zone(name="Office", latitude=40.7128, longitude=-74.0060, radius=150, icon="mdi:briefcase")
        - Update: ha_set_zone(zone_id="abc123", radius=200)
        """
        operation = "create"
        try:
            # ``None`` stays the documented "create-new" sentinel; explicit
            # empty/whitespace ``zone_id`` would silently route to the
            # create branch below and surface "name, latitude, longitude
            # required" instead of the actual cause (unusable ``zone_id``).
            if zone_id is not None:
                validate_identifier_not_empty(
                    zone_id,
                    "zone_id",
                    suggestions=[
                        "Omit zone_id entirely to create a new zone",
                        "Pass a valid zone_id to update an existing zone",
                    ],
                    context={"action": "set"},
                )
            fields = {
                key: value
                for key, value in (
                    ("latitude", latitude),
                    ("longitude", longitude),
                    ("radius", radius),
                    ("passive", passive),
                )
                if value is not None
            }
            if zone_id:
                operation = "update"
                updated = [
                    key
                    for key, value in (
                        ("name", name),
                        ("latitude", latitude),
                        ("longitude", longitude),
                        ("radius", radius),
                        ("icon", icon),
                        ("passive", passive),
                    )
                    if value is not None
                ]
                if not updated:
                    raise_tool_error(
                        create_validation_error(
                            "No fields to update. Provide at least one field to change.",
                            context={"zone_id": zone_id},
                        )
                    )
                entity_id, storage_id = await self._resolve_zone(zone_id)
                # The helper write path merges into the stored zone and keeps
                # a new icon in the registry, where it stays clearable (#2643).
                result = await _execute_update_simple_helper(
                    self._client,
                    "zone",
                    entity_id,
                    entity_id,
                    name,
                    icon,
                    None,
                    None,
                    None,
                    True,
                    False,
                    fields,
                )
                response = _zone_write_response(result, storage_id, "updated")
                response["updated_fields"] = updated
                return response

            if name is None or latitude is None or longitude is None:
                raise_tool_error(
                    create_validation_error(
                        "name, latitude, and longitude are required when creating a zone.",
                    )
                )
            result = await _execute_create_simple_helper(
                self._client,
                "zone",
                name,
                icon,
                None,
                None,
                None,
                True,
                False,
                fields,
            )
            return _zone_write_response(result, None, "created")

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Error in ha_set_zone ({operation}, zone_id={zone_id}, name={name}): {e}"
            )
            exception_to_structured_error(
                e,
                context={"zone_id": zone_id, "operation": operation},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify coordinates are valid"
                    if operation == "create"
                    else "Verify zone_id exists using ha_get_zone()",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_remove_zone",
        tags={"Zones"},
        annotations=write_hints(
            "Remove Zone", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(domain="zone", id_param="zone_id")
    @log_tool_usage
    async def ha_remove_zone(
        self,
        zone_id: Annotated[
            str,
            Field(description="Zone ID or entity_id of the zone to remove"),
        ],
    ) -> dict[str, Any]:
        """
        Remove a Home Assistant zone.

        EXAMPLES:
        - Remove zone: ha_remove_zone("abc123")

        **WARNING:** Removing a zone used in automations may cause those automations to fail.
        Use ha_get_zone() to find the zone_id for the zone you want to remove.

        **NOTE:** Zones defined in YAML, and the home zone, cannot be removed here.
        """
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                zone_id,
                "zone_id",
                suggestions=["Use ha_get_zone() to find existing zone_ids"],
                context={"operation": "remove_zone"},
            )
            entity_id, storage_id = await self._resolve_zone(zone_id)
            result = await self._client.send_websocket_message(
                {"type": "zone/delete", "zone_id": storage_id}
            )
            if not result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ws_failure_code(result),
                        f"Failed to remove zone {entity_id}: "
                        f"{result.get('error', 'Unknown error')}",
                        context={"zone_id": zone_id, "entity_id": entity_id},
                        suggestions=[
                            "Use ha_get_zone() without zone_id to see all available zones",
                        ],
                    )
                )
            response: dict[str, Any] = {
                "success": True,
                "zone_id": storage_id,
                "entity_id": entity_id,
                "message": f"Successfully removed zone: {entity_id}",
            }
            try:
                if not await wait_for_entity_removed(self._client, entity_id):
                    response["warnings"] = [
                        f"Deletion confirmed but {entity_id} is still present "
                        "after the wait window."
                    ]
            except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                response["warnings"] = [
                    f"Deletion confirmed but removal verification failed: {e}"
                ]
            return response

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error removing zone (zone_id={zone_id}): {e}")
            exception_to_structured_error(
                e,
                context={"zone_id": zone_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify zone_id exists using ha_get_zone()",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises


def _zone_write_response(
    result: dict[str, Any], storage_id: str | None, verb: str
) -> dict[str, Any]:
    """Reshape the helper write path's response into ha_set_zone's."""
    data = result.get("data") or {}
    response: dict[str, Any] = {
        "success": True,
        "zone_data": data,
        "zone_id": data.get("id", storage_id),
        "entity_id": result.get("entity_id"),
        "message": f"Successfully {verb} zone: {data.get('name', storage_id)}",
    }
    if result.get("warnings"):
        response["warnings"] = result["warnings"]
    return response


def register_zone_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant zone configuration tools."""
    register_tool_methods(mcp, ZoneTools(client))
