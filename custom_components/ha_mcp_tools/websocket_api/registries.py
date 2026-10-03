"""The ``registries`` read command (area, floor, label and category registries)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .registry import (
    _all_area_entries,
    _all_floor_entries,
    _all_label_entries,
    _resolve_registries,
    _safe,
    _substrate_unavailable,
    _timestamp,
)


# =============================================================================
# ha_mcp_tools/registries
# =============================================================================
def _do_registries(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return the requested registries as the FULL-FIELD ``config/<x>_registry/list`` shapes.

    ``{areas: [...], floors: [...], labels: [...], categories: {scope: [...]}}`` —
    only the requested ``registries`` keys are present. Each row is byte-compatible
    with the legacy WS list response the consumers parse (verified against core's
    registry serializers): area = aliases / area_id / floor_id / icon / labels /
    name / picture / created_at / modified_at, plus humidity_entity_id /
    temperature_entity_id ONLY on core >= 2024.12 (emitted conditionally so an older
    core's restore does not get None-valued keys it rejects — see
    :func:`_area_row`); floor = aliases / created_at / floor_id / icon /
    level / name / modified_at (floor rows carry NO ``labels`` — core omits it);
    label = color / created_at / description / icon / label_id / name /
    modified_at; category = category_id / created_at / icon / modified_at / name.
    Timestamps are floats (``.timestamp()``), matching core.

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
        result["floors"] = [_floor_row(f) for f in _all_floor_entries(view)]
    if "label" in requested:
        if view.label is None:
            raise _substrate_unavailable("label registry")
        result["labels"] = [_label_row(x) for x in _all_label_entries(view)]
    if "category" in requested:
        result["categories"] = _category_rows(hass, category_scopes)
    return result


def _registries_missing_category_scopes() -> Exception:
    """Build a ``HomeAssistantError`` for a scope-less ``category`` request.

    Mirrors :func:`_backup_unavailable`: imported function-locally (test-stubbable)
    so a ``registries`` request naming ``category`` without a non-empty
    ``category_scopes`` raises instead of silently returning ``{}`` (a caller
    could otherwise mistake that for "no categories in any scope").
    """
    from homeassistant.exceptions import HomeAssistantError

    err: Exception = HomeAssistantError(
        "ha_mcp_tools/registries: category_scopes is required when "
        "'category' is requested"
    )
    return err


def _area_row(area: Any) -> dict[str, Any]:
    """One area as core's ``AreaEntry.json_fragment`` shape (id renamed to area_id)."""
    row: dict[str, Any] = {
        "aliases": sorted(str(a) for a in (getattr(area, "aliases", None) or [])),
        "area_id": getattr(area, "id", None) or getattr(area, "area_id", None),
        "floor_id": getattr(area, "floor_id", None),
        "icon": getattr(area, "icon", None),
        "labels": sorted(str(x) for x in (getattr(area, "labels", None) or [])),
        "name": getattr(area, "name", None),
        "picture": getattr(area, "picture", None),
        "created_at": _timestamp(getattr(area, "created_at", None)),
        "modified_at": _timestamp(getattr(area, "modified_at", None)),
    }
    # humidity_entity_id / temperature_entity_id were added to AreaEntry in core
    # 2024.12. Emit each ONLY when the running core's AreaEntry actually has the
    # attribute — a pre-2024.12 core would otherwise get a None-valued key injected
    # here that the restore path's config/area_registry/update schema rejects
    # ("extra keys not allowed"). ``hasattr`` (not ``getattr(..., None)``)
    # distinguishes "core has the field, value is None" from "core has no field".
    for attr in ("humidity_entity_id", "temperature_entity_id"):
        if hasattr(area, attr):
            row[attr] = getattr(area, attr, None)
    return row


def _floor_row(floor: Any) -> dict[str, Any]:
    """One floor as core's ``config/floor_registry/list`` shape (NO ``labels`` field)."""
    return {
        "aliases": sorted(str(a) for a in (getattr(floor, "aliases", None) or [])),
        "created_at": _timestamp(getattr(floor, "created_at", None)),
        "floor_id": getattr(floor, "floor_id", None),
        "icon": getattr(floor, "icon", None),
        "level": getattr(floor, "level", None),
        "name": getattr(floor, "name", None),
        "modified_at": _timestamp(getattr(floor, "modified_at", None)),
    }


def _label_row(label: Any) -> dict[str, Any]:
    """One label as core's ``config/label_registry/list`` shape."""
    return {
        "color": getattr(label, "color", None),
        "created_at": _timestamp(getattr(label, "created_at", None)),
        "description": getattr(label, "description", None),
        "icon": getattr(label, "icon", None),
        "label_id": getattr(label, "label_id", None),
        "name": getattr(label, "name", None),
        "modified_at": _timestamp(getattr(label, "modified_at", None)),
    }


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
    return {
        scope: [_category_row(c) for c in _list_categories(registry, scope)]
        for scope in scopes
    }


def _category_row(category: Any) -> dict[str, Any]:
    """One category as core's ``config/category_registry/list`` shape."""
    return {
        "category_id": getattr(category, "category_id", None),
        "created_at": _timestamp(getattr(category, "created_at", None)),
        "icon": getattr(category, "icon", None),
        "modified_at": _timestamp(getattr(category, "modified_at", None)),
        "name": getattr(category, "name", None),
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
