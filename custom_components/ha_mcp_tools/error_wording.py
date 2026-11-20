"""Every test of an error's wording in the component, each under its source.

Core's error below has no code or type that tells its cases apart, and the
JavaScript engine reports an undefined name only as text.
``scripts/error_wording.py`` refuses a wording test in any other module.
"""

from __future__ import annotations

import re

from homeassistant.exceptions import HomeAssistantError

_REFERENCE_ERROR = re.compile(r"ReferenceError: ([\w$]+) is not defined")


def unknown_entity(exc: Exception) -> bool:
    """components/homeassistant/exposed_entities.py: async_get_entity_settings
    raises a plain HomeAssistantError("Unknown entity"), the type of its other
    faults."""
    return isinstance(exc, HomeAssistantError) and "unknown entity" in str(exc).lower()


def missing_reference(error: str) -> re.Match[str] | None:
    """The JavaScript engine's ReferenceError; group 1 is the undefined name."""
    return _REFERENCE_ERROR.match(error)
