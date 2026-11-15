"""The ``registries`` and ``registry_set_fields`` read commands."""

from __future__ import annotations

import dataclasses
import json
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
# ha_mcp_tools/registry_set_fields
# =============================================================================
def _do_registry_set_fields(
    hass: HomeAssistant, params: dict[str, Any]
) -> dict[str, Any]:
    """Name the fields of one registry entry that Core keeps in a set.

    ``{set_fields: [...] | None}``: the declared fields of the entry's class
    whose value on this entry is a ``set`` or ``frozenset``, sorted; ``None``
    when Core holds no such entry. Core lists a set in hash order, which differs
    between runs, so a caller that compares two rows of the entry takes a list
    named here as equal in any order, and every other list by position. The
    answer is read from the entry, so a field Core starts or stops keeping in a
    set is answered without a change here.

    A field is named, not a row key: in every row Core 2026.11 serves a list of
    more than one item of a set carries its field's name (a device's
    ``config_entries`` holds its one config entry), and a list whose key names
    no such field is compared by position. Only declared fields are read, never
    a row key as an attribute: ``DeviceEntry.config_entries`` and its siblings
    are deprecated properties that report their use.

    A device is looked up where ``device_get`` looks it up (main and child
    devices). A category id is unique within its scope only, so ``category``
    REQUIRES ``scope`` and raises ``HomeAssistantError`` without it. A registry
    whose accessor drifted raises as in :func:`_do_registries`.
    """
    kind, item_id = params["registry"], params["item_id"]
    if kind == "category":
        scope = params.get("scope")
        if not scope:
            raise HomeAssistantError(
                "ha_mcp_tools/registry_set_fields: scope is required for a category"
            )
        registry = _category_registry(hass)
        if registry is None:
            raise _substrate_unavailable("category registry")
        entry = registry.async_get_category(scope=scope, category_id=item_id)
    else:
        view = _resolve_registries(hass)
        registry = getattr(view, kind)
        if registry is None:
            raise _substrate_unavailable(f"{kind} registry")
        if kind == "device":
            entry = _unambiguous_device_entries(view).get(item_id)
        else:
            getter = "async_get" if kind == "entity" else f"async_get_{kind}"
            entry = getattr(registry, getter)(item_id)
    if entry is None:
        return {"set_fields": None}
    return {
        "set_fields": sorted(
            field.name
            for field in declared_fields(entry)
            if isinstance(getattr(entry, field.name), (set, frozenset))
        )
    }


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
