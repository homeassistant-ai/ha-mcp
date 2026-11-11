"""Route a storage helper's icon by the running Core collection's own fields."""

from typing import Any

from ...client.rest_client import HomeAssistantError
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


async def note_hidden_icon(
    client: Any, entity_id: str | None, item_icon: str | None, warnings: list[str]
) -> None:
    """Warn when the entity's registry icon hides the icon just stored or
    cleared.

    The entity shows a registry icon over the stored one, and older ha-mcp
    versions kept icons there (a zone's alone, every helper's on update), so
    a changed or cleared stored icon can stay invisible. The registry read is
    advisory: when it fails the write has still succeeded, so no warning is
    given.
    """
    if item_icon is None or not entity_id:
        return
    try:
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
    except (HomeAssistantError, OSError, TimeoutError):
        return
    entry = result.get("result") if result.get("success") else None
    override = entry.get("icon") if isinstance(entry, dict) else None
    if not override or override == item_icon:
        return
    shown = (
        f"shows its entity registry icon {override} over the stored icon {item_icon}"
        if item_icon
        else f"still shows its entity registry icon {override}"
    )
    warnings.append(
        f"{entity_id} {shown}; clear it with "
        f"ha_set_entity(entity_id='{entity_id}', icon='')."
    )
