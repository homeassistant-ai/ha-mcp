"""Find an entity registry entry by its id and write an entity backup back in process.

The server's entity backups (``ha_set_entity``, ``ha_remove_entity``) hold the
registry entry's settings and its ``id``. Core's ``config/entity_registry/*``
commands find an entry only by entity_id, merge ``categories`` scope by scope
and take only ``user`` for ``hidden_by`` and ``disabled_by``. The registry
itself finds the entry by its id, takes the whole ``categories`` dict and every
``RegistryEntryHider`` and ``RegistryEntryDisabler`` value, and keeps in
``deleted_entities`` the removed entries it can still give back. The server
leaves an enable to Core's update command (``backup_entities.py`` there).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_ENTITY_ENTRY = "ha_mcp_tools/entity_entry"
WS_ENTITY_RESTORE = "ha_mcp_tools/entity_restore"
COMMANDS = (WS_ENTITY_ENTRY, WS_ENTITY_RESTORE)
# Advertised by websocket_api's info; the server's entity backup restore uses
# these and falls back to Core's config/entity_registry commands.
CAPABILITIES = ("entity_entry", "entity_restore")


def _failure(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": message, **extra}}


def _live_entry(registry: Any, entry_id: str) -> Any:
    # ``async_get`` also takes an entity_id; only an entry with this id counts.
    entry = registry.async_get(entry_id)
    return entry if entry is not None and entry.id == entry_id else None


def _deleted(registry: Any, er: Any, entry_id: str) -> dict[str, Any] | None:
    """What Core keeps of a removed entry: while its config entry exists, or
    for 30 days after it was orphaned (``kept_until``, the earliest Core purges
    it). None when Core keeps nothing."""
    for deleted in registry.deleted_entities.values():
        if deleted.id != entry_id:
            continue
        kept_until = None
        if deleted.orphaned_timestamp is not None:
            kept_until = datetime.fromtimestamp(
                deleted.orphaned_timestamp + er.ORPHANED_ENTITY_KEEP_SECONDS, UTC
            ).isoformat()
        return {
            "entity_id": deleted.entity_id,
            "config_entry_id": deleted.config_entry_id,
            "kept_until": kept_until,
        }
    return None


def read_entry(registry: Any, er: Any, entry_id: str) -> dict[str, Any]:
    """The entry's current entity_id, or what Core keeps of it once removed."""
    entry = _live_entry(registry, entry_id)
    if entry is not None:
        return {"entry": {"id": entry.id, "entity_id": entry.entity_id}}
    return {"entry": None, "deleted": _deleted(registry, er, entry_id)}


def _parent_disabled(hass: HomeAssistant, entry: Any, disabler: str) -> bool:
    if disabler == "device":
        from homeassistant.helpers import device_registry as dr

        device = dr.async_get(hass).async_get(entry.device_id or "")
        return bool(device and device.disabled)
    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id or "")
    return bool(config_entry and config_entry.disabled_by)


def _fields(er: Any, settings: dict[str, Any]) -> dict[str, Any]:
    """The saved fields in the types ``async_update_entity`` takes."""
    update: dict[str, Any] = {
        key: settings[key]
        for key in ("name", "icon", "area_id", "device_class")
        if key in settings
    }
    if "aliases" in settings:
        update["aliases"] = [
            er.COMPUTED_NAME if alias is None else alias
            for alias in settings["aliases"]
        ]
    if "labels" in settings:
        update["labels"] = set(settings["labels"])
    if "categories" in settings:
        update["categories"] = dict(settings["categories"])
    return update


def _write_options(
    hass: HomeAssistant, registry: Any, entry: Any, options: dict[str, Any]
) -> Any:
    for domain, saved in options.items():
        expose = (saved or {}).get("should_expose")
        if isinstance(expose, bool) and expose != (entry.options.get(domain) or {}).get(
            "should_expose"
        ):
            # Ahead of the options write: Core tells the assistant only when
            # this call is the one that changes the value.
            from homeassistant.components.homeassistant.exposed_entities import (
                async_expose_entity,
            )

            async_expose_entity(hass, domain, entry.entity_id, expose)
        entry = registry.async_update_entity_options(entry.entity_id, domain, saved)
    return entry


def write_entry(
    hass: HomeAssistant, registry: Any, er: Any, msg: dict[str, Any]
) -> dict[str, Any]:
    """Write the saved settings onto the entry ``entry_id`` names.

    One ``async_update_entity`` carries the rename and every field, so a value
    Core refuses (a taken entity_id, an area on an entity without a name of its
    own) leaves the entry as it was. Options follow one domain at a time.
    """
    entry_id = msg["entry_id"]
    entry = _live_entry(registry, entry_id)
    if entry is None:
        return _failure(
            "entry_deleted",
            f"No entity registry entry has the id {entry_id}",
            deleted=_deleted(registry, er, entry_id),
        )
    if entry.entity_id != msg["entity_id"]:
        return _failure(
            "entity_identity_mismatch",
            f"The entry is {entry.entity_id} now, not {msg['entity_id']}",
        )
    settings = msg["settings"]
    warnings: list[str] = []
    update = _fields(er, settings)
    disabled_by = settings.get("disabled_by")
    if disabled_by in ("device", "config_entry") and not _parent_disabled(
        hass, entry, disabled_by
    ):
        # Core sets and clears these itself as the device or config entry
        # changes; written while it is enabled, the entity would stay
        # disabled for a reason that no longer holds.
        warnings.append(
            f"disabled_by stays {_value(entry.disabled_by)!r}: the backup holds "
            f"{disabled_by!r}, and the {disabled_by.replace('_', ' ')} is not disabled now"
        )
        disabled_by = None
    try:
        if "hidden_by" in settings:
            update["hidden_by"] = _enum(er.RegistryEntryHider, settings["hidden_by"])
        if disabled_by is not None:
            update["disabled_by"] = _enum(er.RegistryEntryDisabler, disabled_by)
        if msg.get("new_entity_id"):
            update["new_entity_id"] = msg["new_entity_id"]
        entry = registry.async_update_entity(entry.entity_id, **update)
    except ValueError as err:
        return _failure("invalid", str(err))
    entry = _write_options(hass, registry, entry, msg.get("options") or {})
    return {"success": True, "entity_entry": entry.extended_dict, "warnings": warnings}


def _enum(kind: Any, value: str | None) -> Any:
    return None if value is None else kind(value)


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def command_specs(vol: Any, er: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) rows websocket_api registers."""

    def do_entry(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return read_entry(er.async_get(hass), er, msg["entry_id"])

    def do_restore(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return write_entry(hass, er.async_get(hass), er, msg)

    text = vol.Any(str, None)
    settings = {
        vol.Optional("name"): text,
        vol.Optional("icon"): text,
        vol.Optional("area_id"): text,
        vol.Optional("device_class"): text,
        vol.Optional("aliases"): [text],
        vol.Optional("labels"): [str],
        vol.Optional("categories"): {str: str},
        vol.Optional("hidden_by"): text,
        vol.Optional("disabled_by"): text,
    }
    return [
        (
            {vol.Required("type"): WS_ENTITY_ENTRY, vol.Required("entry_id"): str},
            do_entry,
            None,
        ),
        (
            {
                vol.Required("type"): WS_ENTITY_RESTORE,
                vol.Required("entry_id"): str,
                vol.Required("entity_id"): str,
                vol.Optional("new_entity_id"): str,
                vol.Required("settings"): settings,
                vol.Optional("options"): {str: vol.Any(dict, None)},
            },
            do_restore,
            None,
        ),
    ]
