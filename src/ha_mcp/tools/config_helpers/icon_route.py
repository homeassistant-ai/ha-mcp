"""Route a storage helper's icon by the running Core collection's own fields."""

from typing import Any

from ...backup_registry_icons import REGISTRY_SNAPSHOTS
from ..component_helper_collections import fetch_helper_schemas


async def collection_icon(
    client: Any, helper_type: str, action: str, icon: str | None
) -> str | None:
    """The icon for the stored item, or ``None`` when it goes to the registry.

    The item takes the icon only when Core's ``action`` schema for the type
    declares an ``icon`` field (person and tag declare none). When the schemas
    cannot be read (no component, or a failed read) or do not list the type or
    action, nothing is guessed: the icon is entity-registry metadata, as the
    entity settings set it.

    A cleared icon ('') is returned as is, so the stored item drops its icon
    too; clearing only the registry would bring an older stored icon back. A
    zone's icon is returned as is for the #2643 rule in ``merged_update``.
    """
    if icon is None or icon == "" or helper_type == "zone":
        return icon
    schemas = await fetch_helper_schemas(client)
    fields = schemas.get(helper_type, {}).get(action) if schemas else None
    if isinstance(fields, list) and any(
        isinstance(f, dict) and f.get("name") == "icon" for f in fields
    ):
        return icon
    return None


def unbacked_icon_warning(helper_type: str, icon: str | None) -> list[str]:
    """Say that a registry icon set or cleared here is not in the helper's
    automatic backups: only tag, zone and person snapshots hold their registry
    icon, so a restore cannot bring it back."""
    if icon is None or helper_type in REGISTRY_SNAPSHOTS:
        return []
    return [
        f"This {helper_type}'s icon is kept in the entity registry, which its "
        "automatic backups do not hold: restoring a backup does not bring an "
        "icon back."
    ]
