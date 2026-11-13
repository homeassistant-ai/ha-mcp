"""A label, category, area or floor in its snapshot and on restore.

Core refuses an update of an id it no longer holds (a label's, an area's and a
floor's with "Unknown error"), so a deleted item is created again. A created
label, area or floor gets the id Core derives from its name, a created category
an id Core picks: neither is the snapshot's after a rename, or for a category
at all. A second restore therefore takes the row whose fields all match the
snapshot's as the one the first restore created. Names are unique in these
registries, so returning that row is right, and the alternative is Core's
"already in use" refusal.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager


async def _rows(client: Any, kind: str) -> list[Any]:
    # Route the read through the component's ``registries`` capability when
    # available (one in-process read of the registry) instead of dumping the
    # whole registry via WS. Lazy import to avoid the backup_manager → tools →
    # backup_manager cycle (same pattern as ``_fetch_device``). ``None`` from
    # the helper means "component unavailable"; fall back to the full list.
    from .tools.component_registries import fetch_registries_via_component

    component_result = await fetch_registries_via_component(client, [kind])
    if component_result is not None:
        return list(component_result.get(f"{kind}s") or [])
    bm = _manager()
    list_type = f"config/{kind}_registry/list"
    rows: list[Any] = bm._require_list(
        await bm._ws_send(client, {"type": list_type}), list_type
    )
    return rows


async def _category_rows(client: Any, scope: str) -> list[Any]:
    # Same component-first routing as ``_rows``; categories are scoped, so the
    # requested scope rides ``category_scopes``.
    from .tools.component_registries import fetch_registries_via_component

    component_result = await fetch_registries_via_component(
        client, ["category"], category_scopes=[scope]
    )
    if component_result is not None:
        return list((component_result.get("categories") or {}).get(scope, []))
    bm = _manager()
    rows: list[Any] = bm._require_list(
        await bm._ws_send(
            client, {"type": "config/category_registry/list", "scope": scope}
        ),
        "config/category_registry/list",
    )
    return rows


_AREA_OR_FLOOR = ("area", "floor")


def _same(a: Any, b: Any) -> bool:
    # Core keeps aliases and labels in sets and lists them in hash order, so a
    # list value matches in any order.
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(str, a)) == sorted(map(str, b))
    return bool(a == b)


def _has_fields(
    row: dict[str, Any], fields: dict[str, Any], subset_keys: tuple[str, ...] = ()
) -> bool:
    # A ``subset_keys`` list may hold fewer entries than the snapshot's: Core
    # drops a label id it no longer holds from a created area.
    return all(
        set(map(str, row.get(key) or [])) <= set(map(str, value or []))
        if key in subset_keys
        else _same(row.get(key), value)
        for key, value in fields.items()
    )


async def _restore_item(
    client: Any,
    rows: list[Any],
    registry: str,
    id_key: str,
    item_id: str,
    payload: dict[str, Any],
    extra_ids: dict[str, Any] | None = None,
    subset_keys: tuple[str, ...] = (),
) -> Any:
    """Update the item, or create it again when Core no longer holds its id.

    A row whose fields all match the payload stands for the item an earlier
    restore created, and is returned as it is (a ``subset_keys`` list may have
    lost entries Core dropped, see ``_has_fields``). A create leaves out a field that
    is None: some create schemas (an area's icon and floor) take a string or
    nothing, and nothing is the default.
    """
    bm = _manager()
    extra_ids = extra_ids or {}
    if any(r.get(id_key) == item_id for r in rows):
        return await bm._ws_send(
            client,
            {
                "type": f"config/{registry}/update",
                **extra_ids,
                id_key: item_id,
                **payload,
            },
        )
    like = next((r for r in rows if _has_fields(r, payload, subset_keys)), None)
    if like is not None:
        return like
    fields = {k: v for k, v in payload.items() if v is not None}
    return await bm._ws_send(
        client, {"type": f"config/{registry}/create", **extra_ids, **fields}
    )


async def fetch_label(client: Any, entity_id: str) -> Any:
    rows = await _rows(client, "label")
    return next((r for r in rows if r.get("label_id") == entity_id), None)


async def restore_label(client: Any, entity_id: str, config: Any) -> Any:
    rows = await _rows(client, "label")
    payload = _manager()._strip_readonly(config, "label_id")
    return await _restore_item(
        client, rows, "label_registry", "label_id", entity_id, payload
    )


async def fetch_category(client: Any, entity_id: str) -> Any:
    scope, _, cat_id = entity_id.partition(":")
    if not cat_id:
        return None
    for item in await _category_rows(client, scope):
        if item.get("category_id") == cat_id:
            return {"scope": scope, **item}
    return None


async def restore_category(client: Any, entity_id: str, config: Any) -> Any:
    scope, _, cat_id = entity_id.partition(":")
    scope = scope or config.get("scope")
    rows = await _category_rows(client, scope)
    payload = _manager()._strip_readonly(config, "category_id", "scope")
    return await _restore_item(
        client,
        rows,
        "category_registry",
        "category_id",
        cat_id,
        payload,
        {"scope": scope},
    )


async def fetch_area_or_floor(client: Any, entity_id: str) -> Any:
    kind, _, real_id = entity_id.partition(":")
    if kind not in _AREA_OR_FLOOR or not real_id:
        return None
    for item in await _rows(client, kind):
        if item.get(f"{kind}_id") == real_id:
            return {"kind": kind, **item}
    return None


async def restore_area_or_floor(client: Any, entity_id: str, config: Any) -> Any:
    kind, _, real_id = entity_id.partition(":")
    if kind not in _AREA_OR_FLOOR:
        raise ValueError(f"Unknown area/floor kind: {kind!r}")
    # ``floor_id`` is the id of a floor but a field of an area.
    payload = _manager()._strip_readonly(config, "kind", f"{kind}_id")
    async with _manager().registry_update_lock(kind, real_id):
        rows = await _rows(client, kind)
        row = await _restore_item(
            client,
            rows,
            f"{kind}_registry",
            f"{kind}_id",
            real_id,
            payload,
            subset_keys=("labels",),
        )
    # Core drops a label id it no longer holds and still answers success.
    missing = sorted(set(payload.get("labels") or []) - set(row.get("labels") or []))
    if missing:
        return {
            **row,
            "warnings": [
                f"The area was restored without the labels {', '.join(missing)}: "
                "they no longer exist."
            ],
        }
    return row
