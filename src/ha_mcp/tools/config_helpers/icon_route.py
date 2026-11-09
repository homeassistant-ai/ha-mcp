"""Route a storage helper's icon by the running Core collection's own fields."""

from typing import Any

from ..component_helper_collections import fetch_helper_schemas


async def icon_targets(
    client: Any, helper_type: str, action: str, icon: str | None
) -> tuple[str | None, str | None]:
    """Where an icon is written: ``(stored item, entity registry)``.

    The item takes the icon when Core's ``action`` schema for the type
    declares an ``icon`` field (person and tag declare none), and then the
    registry does not: the helper and zone dialogs write the icon only to the
    item, and a registry override would hide every later change made there
    (#3001). A type without the field keeps its icon in the registry.

    When the schemas cannot be read (no component, or a failed read) or do
    not list the type or action, nothing is guessed: the icon goes to the
    registry, as the entity settings set it, and a cleared icon ('') clears
    both, since clearing only the registry would bring an older stored icon
    back. A zone keeps its icon in the item even then: its schema has always
    declared one, and the Zones editor shows only the stored icon.
    """
    if icon is None:
        return None, None
    schemas = await fetch_helper_schemas(client)
    fields = schemas.get(helper_type, {}).get(action) if schemas else None
    if isinstance(fields, list):
        if any(isinstance(f, dict) and f.get("name") == "icon" for f in fields):
            return icon, None
        return None, icon
    if helper_type == "zone":
        return icon, None
    return (icon, icon) if icon == "" else (None, icon)
