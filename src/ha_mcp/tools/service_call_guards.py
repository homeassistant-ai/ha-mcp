"""Guards on what ``ha_call_service`` may forward to Home Assistant.

Each one closes a bypass around a dedicated tool: the ``ha_mcp_tools``
services have their own wrappers, and reloading ha-mcp's own config entry is
refused like every other generic write to it.
"""

from typing import Any

from ..errors import create_validation_error
from .backup_access import guard_snapshot_route
from .config_entry_flow import (
    COMPONENT_DOMAIN,
    is_component_domain,
    refuse_component_entry,
)
from .helpers import exception_to_structured_error, raise_tool_error


def validate_service_call_params(
    domain: str | None, service: str | None
) -> tuple[str, str]:
    """Validate service-mode params and return the (domain, service) pair.

    Raises a structured ToolError when domain/service are missing (the caller
    likely wants the ws_command escape hatch), when the domain targets the
    reserved ha_mcp_tools namespace, or when backup controls block the service.
    """
    if not domain or not service:
        raise_tool_error(
            create_validation_error(
                "domain and service are required for a service call. To send "
                "a raw WebSocket command instead, pass ws_command.",
                parameter="domain" if not domain else "service",
            )
        )
    # ha_mcp_tools.* services are restricted to the ha-mcp server's dedicated
    # wrappers (which inject the required caller token). Block ha_call_service
    # from forwarding to that domain — it would otherwise be a bypass path
    # around the dedicated tools. HA core's service registry lowercases the
    # domain on fallback lookup (homeassistant/core.py
    # ServiceRegistry.async_call); is_component_domain normalises, so a
    # mixed-case `HA_MCP_TOOLS` can't slip past and still resolve downstream.
    if is_component_domain(domain):
        raise_tool_error(
            create_validation_error(
                (
                    "ha_call_service cannot invoke services in the "
                    "'ha_mcp_tools' domain. Use the dedicated MCP tool "
                    "instead: ha_list_files, ha_read_file, ha_write_file, "
                    "ha_delete_file, or ha_config_set_yaml."
                ),
                parameter="domain",
            )
        )
    guard_snapshot_route(domain=domain, service=service)
    return domain, service


def _target_ids(service_data: dict[str, Any], key: str) -> set[str]:
    """The ids a target key names, as Core's service schema reads them."""
    value = service_data.get(key)
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list):
        return set()
    ids = {item.strip() for item in items if isinstance(item, str)}
    if key == "entity_id":
        # Core's schema splits a comma-separated entity_id string and
        # lowercases each id; the other keys take ids as given.
        ids = {part.strip().lower() for item in ids for part in item.split(",")}
    return ids - {""}


def _labelled(rows: list[dict[str, Any]], labels: set[str], key: str) -> set[Any]:
    """The ``key`` of every registry row carrying one of ``labels``."""
    return {row.get(key) for row in rows if labels & set(row.get("labels") or [])}


def _targets_own_entry(
    targets: dict[str, set[str]],
    own_ids: set[str],
    entities: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    areas: list[dict[str, Any]],
) -> bool:
    """Whether entity, device, area, floor or label targets reach ``own_ids``.

    Follows Core's ``async_extract_config_entry_ids`` and
    ``helpers/target.py``: a label selects the entities, devices and areas
    carrying it, a floor its areas; a device counts by its own entries and by
    the entries of its entities, an area by the devices and entities in it
    (an entity without an area takes its device's). It skips Core's hidden
    and entity-category filters, so it can only refuse more, never less.
    Core's child and composite device expansion is not followed: the
    component's device has no parent and belongs to one entry.
    """
    labels = targets["label_id"]
    device_area = {d.get("id"): d.get("area_id") for d in devices}
    own_devices = {
        d.get("id") for d in devices if own_ids & set(d.get("config_entries") or [])
    }
    own_entities = [e for e in entities if e.get("config_entry_id") in own_ids]
    entity_ids = targets["entity_id"] | _labelled(entities, labels, "entity_id")
    device_ids = targets["device_id"] | _labelled(devices, labels, "id")
    area_ids = (
        targets["area_id"]
        | _labelled(areas, labels, "area_id")
        | {a.get("area_id") for a in areas if a.get("floor_id") in targets["floor_id"]}
    )
    return bool(
        entity_ids & {e.get("entity_id") for e in own_entities}
        or device_ids & (own_devices | {e.get("device_id") for e in own_entities})
        or area_ids & {device_area.get(d) for d in own_devices}
        or area_ids
        & {
            e.get("area_id") or device_area.get(e.get("device_id"))
            for e in own_entities
        }
    )


async def refuse_component_entry_reload(
    client: Any, domain: str, service: str, service_data: dict[str, Any]
) -> None:
    """Refuse ``homeassistant.reload_config_entry`` on ha-mcp's own entry.

    Checks the explicit ``entry_id`` and every target Core also resolves to
    entries (``entity_id``, ``device_id``, ``area_id``, ``floor_id``,
    ``label_id``); the component's update entity and its device reach the
    server entry. Group expansion (``expand_group``) is not resolved: Core
    deprecates reloading by target (homeassistant/services.py, removal in
    2027.4), so the remaining path is not worth more. Ids Core does not have
    are left to Core's own error. A failed lookup raises a structured error:
    nothing has been dispatched at this point.
    """
    if (
        domain.strip().lower() != "homeassistant"
        or service.strip().lower() != "reload_config_entry"
    ):
        return
    entry_id = service_data.get("entry_id")
    targets = {
        key: _target_ids(service_data, key)
        for key in ("entity_id", "device_id", "area_id", "floor_id", "label_id")
    }
    hit = False
    try:
        own_ids = {
            entry.get("entry_id")
            for entry in await client.list_config_entries()
            if is_component_domain(entry.get("domain"))
        }
        hit = isinstance(entry_id, str) and entry_id in own_ids
        if not hit and own_ids and any(targets.values()):
            # Only floors and labels need the area registry (an area target
            # matches by id); the client has no public helper for it.
            areas = (
                await client._list_registry("config/area_registry/list", "area")
                if targets["floor_id"] or targets["label_id"]
                else []
            )
            hit = _targets_own_entry(
                targets,
                own_ids,
                await client.list_entity_registry(),
                await client.list_device_registry(),
                areas,
            )
    except Exception as err:  # noqa: BLE001
        exception_to_structured_error(
            err,
            context={"operation": "reload_config_entry own-entry check"},
            suggestions=["Nothing was reloaded; retry once Home Assistant answers."],
        )
    if hit:
        refuse_component_entry(
            COMPONENT_DOMAIN,
            action="reload",
            entry_id=entry_id if isinstance(entry_id, str) else None,
        )
