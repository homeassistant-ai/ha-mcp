"""Every test of another program's error wording in the component.

These errors carry no code or type that tells the cases apart, so their
wording is the only signal. ``scripts/error_wording.py`` refuses a wording
test in any other module.
"""

from __future__ import annotations

import re

from homeassistant.exceptions import HomeAssistantError

_EXPORT_NEEDED = re.compile(r"Error: export-needed (\d+):([\w$]+)")
_REFERENCE_ERROR = re.compile(r"ReferenceError: ([\w$]+) is not defined")


def unknown_entity(exc: Exception) -> bool:
    """homeassistant/exposed_entities.py async_get_entity_settings raises a
    plain HomeAssistantError("Unknown entity"), the type of its other faults."""
    return isinstance(exc, HomeAssistantError) and "unknown entity" in str(exc).lower()


def export_needed(error: str) -> re.Match[str] | None:
    """The card engine's own request for a module export; groups: module id, name."""
    return _EXPORT_NEEDED.fullmatch(error)


def missing_reference(error: str) -> re.Match[str] | None:
    """The JavaScript engine's ReferenceError; group 1 is the undefined name."""
    return _REFERENCE_ERROR.match(error)
