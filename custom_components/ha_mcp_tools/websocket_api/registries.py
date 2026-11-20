"""The ``registries`` and ``registry_fields`` read commands."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from typing import Any

import attr
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.json import json_bytes

from .registry import (
    _all_area_entries,
    _all_floor_entries,
    _all_label_entries,
    _resolve_registries,
    _safe,
    _substrate_unavailable,
    _unambiguous_device_entries,
)


# =============================================================================
# ha_mcp_tools/registries
# =============================================================================
def _do_registries(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return the requested registries as Core's own ``config/<x>_registry/list`` rows.

    ``{areas: [...], floors: [...], labels: [...], categories: {scope: [...]}}`` —
    only the requested ``registries`` keys are present. Each row is the one Core's
    WebSocket list sends (``AreaEntry.json_fragment`` for an area, the config
    component's ``_entry_dict`` for the rest), so a field Core adds reaches the
    caller without a change here. Those ``_entry_dict`` functions are private to
    Core: if a release drops one, the import error fails this command and the
    server falls back to the WebSocket list.

    ``category`` is scoped: ``category_scopes`` names which scopes to list (a
    ``{scope: [rows]}`` map) and is REQUIRED when ``category`` is requested — a
    scope-less category request raises ``HomeAssistantError`` (see
    :func:`_registries_missing_category_scopes`) rather than silently serving
    ``{}``, a shape indistinguishable from "every requested scope is empty". The
    category registry is imported function-locally (not needed at module top).
    Pure in-memory reads over the resolved registries.
    """
    requested = params.get("registries") or []
    category_scopes = params.get("category_scopes") or []
    if "category" in requested and not category_scopes:
        raise _registries_missing_category_scopes()

    view = _resolve_registries(hass)
    result: dict[str, Any] = {}
    # A requested registry whose accessor drifted (raised / renamed → ``None`` via
    # ``_safe``) RAISES → server command-error fallback to the legacy WS list,
    # rather than serving a well-formed empty list the capture pipeline would read
    # as "this entity does not exist" and silently skip. A present-but-empty
    # registry serves its empty list (correct).
    if "area" in requested:
        if view.area is None:
            raise _substrate_unavailable("area registry")
        result["areas"] = [_area_row(a) for a in _all_area_entries(view)]
    if "floor" in requested:
        if view.floor is None:
            raise _substrate_unavailable("floor registry")
        from homeassistant.components.config.floor_registry import (
            _entry_dict as floor_row,
        )

        result["floors"] = [floor_row(f) for f in _all_floor_entries(view)]
    if "label" in requested:
        if view.label is None:
            raise _substrate_unavailable("label registry")
        from homeassistant.components.config.label_registry import (
            _entry_dict as label_row,
        )

        result["labels"] = [label_row(x) for x in _all_label_entries(view)]
    if "category" in requested:
        result["categories"] = _category_rows(hass, category_scopes)
    return result


# =============================================================================
# ha_mcp_tools/registry_fields
# =============================================================================
def _do_registry_fields(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Name the fields Core's commands take for a registry, and how Core keeps
    the fields of one of its entries.

    Returns ``update_fields`` and ``create_fields`` (the keys of the schemas
    Core registered for ``config/<registry>_registry/update`` and
    ``.../create``, without ``id`` and ``type``; ``create_fields`` is ``None``
    without a create command), and with ``item_id`` the entry's
    ``set_fields`` (declared fields holding a ``set`` or ``frozenset``) and
    ``stamped_fields`` (declared ``datetime`` fields ``update_fields`` does not
    name). Both are ``None`` without ``item_id`` or when Core holds no such
    entry. All lists are sorted.

    Only declared fields are named (read by attribute); a row key that is a
    property, such as a device's deprecated ``config_entries``, is never
    named; on Core 2026.10 and later, reading it reports its use.

    Raises ``HomeAssistantError`` when Core registers no update command for the
    registry, and, with ``item_id``, for a ``category`` without ``scope`` and
    for a registry whose accessor drifted (as :func:`_do_registries`).
    """
    kind = params["registry"]
    update_fields = _command_fields(hass, f"config/{kind}_registry/update")
    if update_fields is None:
        raise HomeAssistantError(
            f"ha_mcp_tools/registry_fields: Core registers no update command "
            f"for the {kind} registry"
        )
    result: dict[str, Any] = {
        "update_fields": update_fields,
        "create_fields": _command_fields(hass, f"config/{kind}_registry/create"),
        "set_fields": None,
        "stamped_fields": None,
    }
    item_id = params.get("item_id")
    if item_id is None:
        return result
    entry = _registry_entry(hass, kind, item_id, params.get("scope"))
    if entry is None:
        return result
    values = {
        field.name: getattr(entry, field.name) for field in declared_fields(entry)
    }
    result["set_fields"] = sorted(
        name for name, value in values.items() if isinstance(value, (set, frozenset))
    )
    result["stamped_fields"] = sorted(
        name
        for name, value in values.items()
        if isinstance(value, datetime) and name not in update_fields
    )
    return result


def _command_fields(hass: HomeAssistant, command: str) -> list[str] | None:
    """The keys of the schema Core registered for ``command``, without the
    transport keys ``id`` and ``type``; None when Core registers no such
    command."""
    registered = (hass.data.get("websocket_api") or {}).get(command)
    if registered is None:
        return None
    return sorted({str(key) for key in registered[1].schema} - {"id", "type"})


def _registry_entry(hass: HomeAssistant, kind: str, item_id: str, scope: Any) -> Any:
    """Core's entry of ``kind`` with ``item_id``, None when Core holds none."""
    if kind == "category":
        if not scope:
            raise HomeAssistantError(
                "ha_mcp_tools/registry_fields: scope is required for a category"
            )
        registry = _category_registry(hass)
        if registry is None:
            raise _substrate_unavailable("category registry")
        return registry.async_get_category(scope=scope, category_id=item_id)
    view = _resolve_registries(hass)
    registry = getattr(view, kind)
    if registry is None:
        raise _substrate_unavailable(f"{kind} registry")
    if kind == "device":
        return _unambiguous_device_entries(view).get(item_id)
    getter = "async_get" if kind == "entity" else f"async_get_{kind}"
    return getattr(registry, getter)(item_id)


def declared_fields(entry: Any) -> Any:
    """The fields a registry entry's class declares (attrs or dataclass). Test
    seam: a test replaces it to stand for a Core release whose entry keeps a
    field differently (a list where today's keeps a set)."""
    cls = type(entry)
    return attr.fields(cls) if attr.has(cls) else dataclasses.fields(cls)


def _registries_missing_category_scopes() -> Exception:
    """Build a ``HomeAssistantError`` for a scope-less ``category`` request.

    Raised so a ``registries`` request naming ``category`` without a non-empty
    ``category_scopes`` fails instead of silently returning ``{}`` (a caller
    could otherwise mistake that for "no categories in any scope").
    """
    err: Exception = HomeAssistantError(
        "ha_mcp_tools/registries: category_scopes is required when "
        "'category' is requested"
    )
    return err


def _area_row(area: Any) -> dict[str, Any]:
    """One area as Core's ``AreaEntry.json_fragment`` row."""
    # An ``orjson.Fragment`` exposes no bytes; serializing it yields them.
    row: dict[str, Any] = json.loads(json_bytes(area.json_fragment))
    return row


def _category_rows(hass: HomeAssistant, scopes: list[str]) -> dict[str, Any]:
    """``{scope: [category rows]}`` for each requested scope (categories are scoped).

    A drifted / absent category registry (``None`` — ``cr.async_get`` raised /
    renamed, or the module is missing on an old core) RAISES rather than serving
    ``{scope: []}`` for every scope, so the server falls back to the legacy
    ``config/category_registry/list`` instead of trusting an empty map.
    """
    registry = _category_registry(hass)
    if registry is None:
        raise _substrate_unavailable("category registry")
    from homeassistant.components.config.category_registry import (
        _entry_dict as category_row,
    )

    return {
        scope: [category_row(c) for c in _list_categories(registry, scope)]
        for scope in scopes
    }


def _category_registry(hass: HomeAssistant) -> Any:
    """The category registry (imported function-locally). Test seam. ``None`` on drift."""
    try:
        from homeassistant.helpers import category_registry as cr
    except ImportError:  # pragma: no cover - defensive; core drift
        return None
    return _safe(cr.async_get, hass)


def _list_categories(registry: Any, scope: str) -> list[Any]:
    """``registry.async_list_categories(scope=...)`` guarded (scope is keyword-only)."""
    if registry is None:
        return []
    lister = getattr(registry, "async_list_categories", None)
    if not callable(lister):
        return []
    try:
        return list(lister(scope=scope))
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return []
