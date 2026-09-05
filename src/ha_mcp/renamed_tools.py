"""Tools whose names moved, and the stored state that has to follow them.

Home Assistant 2026.2 renamed add-ons to apps, and this server's two add-on
tools moved with it. Issue #2329 then folded the two blueprint tools into one
``ha_manage_blueprints``. Neither is a breaking change for callers — clients
resolve by name at runtime, and ``RenamedToolAliasMiddleware`` keeps the old
name callable for one that has not re-listed yet.

What does not repair itself is state a user already stored under the old name:
a tool they disabled or pinned in the settings UI, a ``DISABLED_TOOLS`` entry,
a policy rule naming the tool. Those keys are read through the mapping here,
so the setting survives the rename instead of falling back to the default —
which for a disabled write tool means silently re-enabling it.

A consolidation adds two wrinkles a plain rename does not have. Two retired
names can now carry different stored states for one current tool, so the
readers pick the more restrictive one (``rename_retired_keys``'s ``prefer``).
And a call on a retired name carries the old signature, which the merged tool
cannot dispatch without the ``action`` it never had —
``adapt_retired_arguments`` fills that in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

# Retired tool name -> the name that serves it today.
RENAMED_TOOLS: dict[str, str] = {
    "ha_get_addon": "ha_get_app",
    "ha_manage_addon": "ha_manage_app",
    # #2329: consolidated, not renamed — both retired tools answer to one
    # action-dispatched tool. A call on either old name needs an ``action``
    # (see ``adapt_retired_arguments``), and stored state for the two can
    # disagree (see ``rename_retired_keys``).
    "ha_get_blueprint": "ha_manage_blueprints",
    "ha_import_blueprint": "ha_manage_blueprints",
}


def current_tool_name(name: str) -> str:
    """The name a tool answers to today, given any name it has had."""
    return RENAMED_TOOLS.get(name, name)


def rename_retired_keys[T](
    states: Mapping[str, T], *, prefer: Callable[[T, T], T] | None = None
) -> dict[str, T]:
    """Re-key a per-tool mapping onto the current tool names.

    A value already stored under the current name wins: it was set against
    the tool as it is now, while the retired key is inherited.

    When two retired names were folded into one current tool and both carry
    a value, ``prefer(a, b)`` picks the one to keep; readers pass the more
    restrictive choice for their value type (a disabled tool must not come
    back enabled because its sibling was merely pinned). Without ``prefer``
    the later key in iteration order wins, which is only acceptable for
    mappings that cannot collide.
    """
    renamed: dict[str, T] = {}
    for name, value in states.items():
        if name not in RENAMED_TOOLS:
            continue
        current = current_tool_name(name)
        if current in renamed and prefer is not None:
            renamed[current] = prefer(renamed[current], value)
        else:
            renamed[current] = value
    kept = {name: value for name, value in states.items() if name not in RENAMED_TOOLS}
    return {**renamed, **kept}


def _adapt_get_blueprint(arguments: dict[str, Any]) -> dict[str, Any]:
    # ``ha_get_blueprint`` listed without a path and read one with it.
    return {"action": "get" if arguments.get("path") else "list", **arguments}


def _adapt_import_blueprint(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"action": "import", **arguments}


# Retired name -> how its call arguments map onto the current tool's. Only a
# consolidation needs one; a plain rename forwards the arguments unchanged.
_RETIRED_ARGUMENT_ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "ha_get_blueprint": _adapt_get_blueprint,
    "ha_import_blueprint": _adapt_import_blueprint,
}


def adapt_retired_arguments(
    name: str, arguments: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Rewrite a retired tool's call arguments for the tool that serves it now.

    ``name`` is the name the caller used. Names without an adapter — current
    names and plain renames — return the arguments untouched (``None`` stays
    ``None``). An explicit ``action`` in the arguments is respected.
    """
    adapter = _RETIRED_ARGUMENT_ADAPTERS.get(name)
    if adapter is None:
        return dict(arguments) if arguments is not None else None
    return adapter(dict(arguments or {}))
