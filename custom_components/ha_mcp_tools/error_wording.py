"""Every test of an error's wording in the component, each under its source.

Core's error below has no code or type that tells its cases apart, and the
JavaScript engine reports an undefined name only as text. ``export_needed``
reads the card engine's own request (``card_definitions.py``) and sits with
them. ``scripts/error_wording.py`` refuses a wording test in any other module.
"""

from __future__ import annotations

import re

from homeassistant.exceptions import HomeAssistantError

_EXPORT_NEEDED = re.compile(r"Error: export-needed (\d+):([\w$]+)")
_REFERENCE_ERROR = re.compile(r"ReferenceError: ([\w$]+) is not defined")


def unknown_entity(exc: Exception) -> bool:
    """components/homeassistant/exposed_entities.py: async_get_entity_settings
    raises a plain HomeAssistantError("Unknown entity"), the type of its other
    faults."""
    return isinstance(exc, HomeAssistantError) and "unknown entity" in str(exc).lower()


def export_needed(error: str) -> re.Match[str] | None:
    """The card engine's own request for a module export; groups: module id, name."""
    return _EXPORT_NEEDED.fullmatch(error)


def missing_reference(error: str) -> re.Match[str] | None:
    """The JavaScript engine's ReferenceError; group 1 is the undefined name."""
    return _REFERENCE_ERROR.match(error)
