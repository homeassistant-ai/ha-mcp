"""A label or category in its snapshot and on restore.

Core refuses an update of an id it no longer holds (a label's with "Unknown
error"), so a deleted item is created again: a created label gets the id Core
derives from its name, a created category an id Core picks. Neither is the
snapshot's after a rename, or for a category at all, so a second restore takes
the row whose fields all match the snapshot's as the one the first restore
created. Names are unique in both registries, so returning that row is right,
and the alternative is Core's "already in use" refusal.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager


async def _label_rows(client: Any) -> list[Any]:
    # Route the read through the component's ``registries`` capability when
    # available (one in-process read of the label registry) instead of dumping
    # the whole registry via WS. Lazy import to avoid the backup_manager →
    # tools → backup_manager cycle (same pattern as ``_fetch_device``). ``None``
    # from the helper means "component unavailable"; fall back to the full list.
    from .tools.component_registries import fetch_registries_via_component

    component_result = await fetch_registries_via_component(client, ["label"])
    if component_result is not None:
        return list(component_result.get("labels") or [])
    bm = _manager()
    rows: list[Any] = bm._require_list(
        await bm._ws_send(client, {"type": "config/label_registry/list"}),
        "config/label_registry/list",
    )
    return rows


async def _category_rows(client: Any, scope: str) -> list[Any]:
    # Same component-first routing as ``_label_rows``; categories are scoped,
    # so the requested scope rides ``category_scopes``.
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


def _has_fields(row: dict[str, Any], fields: dict[str, Any]) -> bool:
    return all(row.get(key) == value for key, value in fields.items())


async def fetch_label(client: Any, entity_id: str) -> Any:
    rows = await _label_rows(client)
    return next((r for r in rows if r.get("label_id") == entity_id), None)


async def restore_label(client: Any, entity_id: str, config: Any) -> Any:
    bm = _manager()
    payload = bm._strip_readonly(config, "label_id")
    rows = await _label_rows(client)
    if any(r.get("label_id") == entity_id for r in rows):
        return await bm._ws_send(
            client,
            {"type": "config/label_registry/update", "label_id": entity_id, **payload},
        )
    like = next((r for r in rows if _has_fields(r, payload)), None)
    if like is not None:
        return like
    return await bm._ws_send(
        client, {"type": "config/label_registry/create", **payload}
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
    bm = _manager()
    scope, _, cat_id = entity_id.partition(":")
    scope = scope or config.get("scope")
    payload = bm._strip_readonly(config, "category_id", "scope")
    rows = await _category_rows(client, scope)
    if any(r.get("category_id") == cat_id for r in rows):
        return await bm._ws_send(
            client,
            {
                "type": "config/category_registry/update",
                "scope": scope,
                "category_id": cat_id,
                **payload,
            },
        )
    like = next((r for r in rows if _has_fields(r, payload)), None)
    if like is not None:
        return like
    return await bm._ws_send(
        client, {"type": "config/category_registry/create", "scope": scope, **payload}
    )
