"""Tools whose names moved, and the stored state that has to follow them.

Home Assistant 2026.2 renamed add-ons to apps, and this server's two add-on
tools moved with it. Renaming is not a breaking change for callers — clients
resolve by name at runtime, and ``RenamedToolAliasMiddleware`` keeps the old
name callable for one that has not re-listed yet.

What does not repair itself is state a user already stored under the old name:
a tool they disabled or pinned in the settings UI, a ``DISABLED_TOOLS`` entry,
a policy rule naming the tool. Those keys are read through the mapping here,
so the setting survives the rename instead of falling back to the default —
which for a disabled write tool means silently re-enabling it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar

T = TypeVar("T")

# Retired tool name -> the name that serves it today.
RENAMED_TOOLS: dict[str, str] = {
    "ha_get_addon": "ha_get_app",
    "ha_manage_addon": "ha_manage_app",
}


def current_tool_name(name: str) -> str:
    """The name a tool answers to today, given any name it has had."""
    return RENAMED_TOOLS.get(name, name)


def rename_retired_keys(states: Mapping[str, T]) -> dict[str, T]:
    """Re-key a per-tool mapping onto the current tool names.

    A value already stored under the current name wins: it was set against
    the tool as it is now, while the retired key is inherited.
    """
    renamed = {
        current_tool_name(name): value
        for name, value in states.items()
        if name in RENAMED_TOOLS
    }
    kept = {
        name: value for name, value in states.items() if name not in RENAMED_TOOLS
    }
    return {**renamed, **kept}
