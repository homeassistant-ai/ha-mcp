"""The category ``ha_manage_updates`` files an update entity under."""

from typing import Any

# The integration whose update entities are store repositories.
_STORE_PLATFORM = "marketplace"
# The hassio platform names its system update entities with these fixed titles
# and every other entity it adds is an app; the frontend's getUpdateType tells
# them apart by the same test.
_HASSIO_TITLES = {
    "Home Assistant Core": "core",
    "Home Assistant Operating System": "os",
    "Home Assistant Supervisor": "supervisor",
}


def categorize_update(platform: str | None, attributes: dict[str, Any]) -> str:
    """Categorize an update entity by its integration and Core's attributes."""
    if platform == "hassio":
        return _HASSIO_TITLES.get(attributes.get("title") or "", "addons")
    if platform == _STORE_PLATFORM:
        return "hacs"
    if attributes.get("device_class") == "firmware":
        return "devices"
    return "other"
