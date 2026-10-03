"""Read, write and describe simple helpers through Core's own storage collections.

Collection helpers (input_*, counter, timer, schedule) keep their
``StorageCollection`` setup-local; zone, person and tag store theirs under
domain-specific ``hass.data`` keys. The one reference all twelve share is the
``<type>/create`` WebSocket handler Core registers: it wraps a bound method of
the ``StorageCollectionWebsocket`` that owns the collection and the exact
create/update schemas Core validates against. Resolving that owner gives a
uniform, in-process path that runs the same validation as Core's WS commands.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SIMPLE_HELPER_TYPES = (
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "counter",
    "timer",
    "schedule",
    "zone",
    "person",
    "tag",
)

WS_HELPER_SCHEMAS = "ha_mcp_tools/helper_schemas"
WS_HELPER_ITEM = "ha_mcp_tools/helper_item"
WS_HELPER_WRITE = "ha_mcp_tools/helper_write"
COMMANDS = (WS_HELPER_SCHEMAS, WS_HELPER_ITEM, WS_HELPER_WRITE)
# Advertised by websocket_api's info; the server routes ha_config_set_helper's
# simple-helper writes through these and falls back to Core's WS commands.
CAPABILITIES = ("helper_schemas", "helper_item", "helper_write")

# ``websocket_api.async_register_command`` stores ``{command: (handler, schema)}``
# under the integration's domain key.
_WS_HANDLERS_KEY = "websocket_api"


def collection_owner(hass: HomeAssistant, helper_type: str) -> Any:
    """Return the ``StorageCollectionWebsocket`` serving ``helper_type``, or None."""
    entry = (hass.data.get(_WS_HANDLERS_KEY) or {}).get(f"{helper_type}/create")
    if not entry:
        return None
    owner = getattr(inspect.unwrap(entry[0]), "__self__", None)
    if not all(
        hasattr(owner, attr)
        for attr in ("storage_collection", "create_schema", "update_schema")
    ):
        return None
    return owner


def _optional_attr(module: str, name: str) -> Any:
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        return None


# Resolved at import, off the event loop: Core 2026.9+ serializes with
# probatio, earlier Core with voluptuous_serialize.
_TO_FIELD_LIST = _optional_attr("probatio", "to_field_list")
_VS_CONVERT = _optional_attr("voluptuous_serialize", "convert")


def _convert(schema: Any) -> list[dict[str, Any]]:
    """Serialize with Core's serializer for this Core version."""
    from homeassistant.helpers import config_validation as cv

    errors: list[str] = []
    for convert in (_TO_FIELD_LIST, _VS_CONVERT):
        if convert is None:
            continue
        try:
            return list(convert(schema, custom_serializer=cv.custom_serializer))
        except Exception as err:
            errors.append(f"{type(err).__name__}: {err}")
    raise ValueError("; ".join(errors) or "no schema serializer available")


def _serialize(schema: dict[Any, Any]) -> list[dict[str, Any]]:
    """Serialize a collection schema the way Core serializes flow ``data_schema``.

    Collection schemas use validators Core's serializer can't express (cv.icon,
    input_select's unique-options check, ...), so fields go one at a time; one
    it can't express keeps its name and requiredness.
    """
    fields: list[dict[str, Any]] = []
    for key, validator in schema.items():
        try:
            fields.extend(_convert({key: validator}))
        except ValueError:
            required = type(key).__name__ == "Required"
            fields.append({"name": str(key), "required": required})
    return fields


def describe_schemas(hass: HomeAssistant) -> dict[str, Any]:
    """Core's create/update field lists for every resolvable simple helper type."""
    types: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for helper_type in SIMPLE_HELPER_TYPES:
        owner = collection_owner(hass, helper_type)
        if owner is None:
            errors[helper_type] = "no storage collection"
            continue
        try:
            types[helper_type] = {
                "create": _serialize(owner.create_schema),
                "update": _serialize(owner.update_schema),
            }
        except Exception as err:
            _LOGGER.debug("Cannot serialize %s schemas", helper_type, exc_info=True)
            errors[helper_type] = f"{type(err).__name__}: {err}"
    return {"types": types, "unavailable": list(errors), "errors": errors}


def _failure(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": message}}


def _entity_id_for(registry: Any, helper_type: str, item_id: str) -> str | None:
    try:
        return registry.async_get_entity_id(helper_type, helper_type, item_id)  # type: ignore[no-any-return]
    except Exception:
        return None


def read_item(
    hass: HomeAssistant,
    registry: Any,
    helper_type: str,
    entity_id: str | None,
    item_id: str | None,
) -> dict[str, Any]:
    """Return a stored collection item by item id or by its entity's unique_id."""
    owner = collection_owner(hass, helper_type)
    if owner is None:
        return _failure("unavailable", f"No {helper_type} storage collection")
    if item_id is None:
        entry = registry.async_get(entity_id) if entity_id else None
        if entry is None or entry.platform != helper_type:
            return _failure(
                "not_found", f"Could not find {helper_type} entity: {entity_id}"
            )
        item_id = str(entry.unique_id)
    item = owner.storage_collection.data.get(item_id)
    if item is None:
        return _failure(
            "not_found", f"{helper_type} config not found for id: {item_id}"
        )
    return {
        "success": True,
        "item_id": item_id,
        "item": dict(item),
        "entity_id": entity_id or _entity_id_for(registry, helper_type, item_id),
    }


def _invalid_errors() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = [ValueError]
    for module in ("voluptuous", "probatio"):
        try:
            invalid = importlib.import_module(module).Invalid
        except (ImportError, AttributeError):
            continue
        if isinstance(invalid, type) and issubclass(invalid, BaseException):
            errors.append(invalid)
    return tuple(errors)


# Resolved at import, off the event loop.
_INVALID_ERRORS = _invalid_errors()


def _item_not_found() -> type[BaseException]:
    try:
        from homeassistant.helpers.collection import ItemNotFound
    except ImportError:
        return LookupError
    return ItemNotFound  # type: ignore[no-any-return]


def _apply_registry(
    registry: Any,
    entity_id: str,
    changes: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Apply icon/area/labels/category to the entity registry; return what landed."""
    entry = registry.async_get(entity_id)
    if entry is None:
        warnings.append(f"Entity registry entry not found for {entity_id}")
        return {}
    # An empty icon/area clears it, as Core's registry update expects None.
    applied: dict[str, Any] = {
        key: changes[key] or None if key in ("icon", "area_id") else changes[key]
        for key in ("icon", "area_id", "labels", "category")
        if key in changes
    }
    update: dict[str, Any] = {
        key: applied[key] for key in ("icon", "area_id") if key in applied
    }
    if "labels" in applied:
        update["labels"] = set(applied["labels"] or ())
    if "category" in applied:
        update["categories"] = {**entry.categories, "helpers": applied["category"]}
    try:
        registry.async_update_entity(entity_id, **update)
    except Exception as err:
        warnings.append(f"Entity registry update failed: {err}")
        return {}
    return applied


async def async_write_item(
    hass: HomeAssistant, registry: Any, msg: dict[str, Any]
) -> dict[str, Any]:
    """Create or update a stored item, then apply its entity-registry fields.

    Core's ``async_create_item`` awaits entity creation, so the entity is
    registered when it returns and the registry update needs no wait.
    """
    helper_type = msg["helper_type"]
    owner = collection_owner(hass, helper_type)
    if owner is None:
        return _failure("unavailable", f"No {helper_type} storage collection")
    collection = owner.storage_collection
    data = dict(msg["data"])
    try:
        if msg["action"] == "create":
            item = await collection.async_create_item(data)
        else:
            item = await collection.async_update_item(msg["item_id"], data)
    except _item_not_found() as err:
        return _failure("not_found", f"{helper_type} config not found: {err}")
    except _INVALID_ERRORS as err:
        return _failure("invalid", str(err))

    item = dict(item)
    entity_id = _entity_id_for(registry, helper_type, item.get("id", ""))
    warnings: list[str] = []
    applied: dict[str, Any] = {}
    changes = msg.get("registry") or {}
    if changes:
        if entity_id:
            applied = _apply_registry(registry, entity_id, changes, warnings)
        else:
            warnings.append(f"No entity registered for {helper_type} {item.get('id')}")
    return {
        "success": True,
        "item": item,
        "entity_id": entity_id,
        "registry_applied": applied,
        "warnings": warnings,
    }


def command_specs(vol: Any, er: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) rows websocket_api registers.

    Built from websocket_api's ``vol`` and ``er`` so its registration stays the
    one place those are bound.
    """
    helper_type = vol.In(SIMPLE_HELPER_TYPES)

    def do_schemas(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return describe_schemas(hass)

    def do_item(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return read_item(
            hass,
            er.async_get(hass),
            msg["helper_type"],
            msg.get("entity_id"),
            msg.get("item_id"),
        )

    async def write_prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        if msg["action"] == "update" and not msg.get("item_id"):
            return {"result": _failure("invalid", "item_id is required for update")}
        return {"result": await async_write_item(hass, er.async_get(hass), msg)}

    def do_write(
        hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    registry_fields = {
        vol.Optional("icon"): vol.Any(str, None),
        vol.Optional("area_id"): vol.Any(str, None),
        vol.Optional("labels"): [str],
        vol.Optional("category"): vol.Any(str, None),
    }
    return [
        ({vol.Required("type"): WS_HELPER_SCHEMAS}, do_schemas, None),
        (
            {
                vol.Required("type"): WS_HELPER_ITEM,
                vol.Required("helper_type"): helper_type,
                vol.Exclusive("entity_id", "target"): str,
                vol.Exclusive("item_id", "target"): str,
            },
            do_item,
            None,
        ),
        (
            {
                vol.Required("type"): WS_HELPER_WRITE,
                vol.Required("helper_type"): helper_type,
                vol.Required("action"): vol.In(("create", "update")),
                vol.Optional("item_id"): str,
                vol.Required("data"): dict,
                vol.Optional("registry"): registry_fields,
            },
            do_write,
            write_prep,
        ),
    ]
