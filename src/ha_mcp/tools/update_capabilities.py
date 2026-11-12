"""Native capabilities shared by update discovery surfaces."""

from typing import Any

# Home Assistant's UpdateEntityFeature.INSTALL; Core is not a server dependency.
_INSTALL = 1


def can_install_update(attributes: dict[str, Any]) -> bool:
    """Report native Install support only while no installation is running."""
    features = attributes.get("supported_features", 0)
    return (
        type(features) is int
        and bool(features & _INSTALL)
        and not attributes.get("in_progress", False)
    )
