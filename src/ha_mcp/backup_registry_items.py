"""A label, category, area or floor in its snapshot and on restore.

Core refuses an update of an id it no longer holds (a label's, an area's and a
floor's with "Unknown error"), so a deleted item is created again. A created
label, area or floor gets the id Core derives from its name, a created category
an id Core picks: neither is the snapshot's after a rename, or for a category
at all, and the restore says so. A second restore takes the row whose fields all
match the snapshot's as the one the first restore created. Names are unique in
these registries, so returning that row is right, and the alternative is Core's
"already in use" refusal. A restore Core refuses for that reason names the item
that holds the name, or says Core refused it. The diff finds the live item that
holds a deleted item's name, and reports an id that differs.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager
from .client.rest_client import HomeAssistantCommandError


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


def _has_fields(row: dict[str, Any], fields: dict[str, Any]) -> bool:
    return all(_same(row.get(key), value) for key, value in fields.items())


def _like(rows: list[Any], payload: dict[str, Any]) -> Any:
    """The row whose fields all match the payload, None when there is none."""
    return next((r for r in rows if _has_fields(r, payload)), None)


def _holding_name(rows: list[Any], payload: dict[str, Any]) -> Any:
    """The row that has the payload's name, None when there is none.

    Names are unique in these registries. The match is on the name as the
    snapshot holds it, which is the name a restore sends; a row Core takes for
    the same name under its own normalisation is not found, and Core's text
    stands alone then.
    """
    return next((r for r in rows if r.get("name") == payload.get("name")), None)


def _noting(row: Any, *warnings: str) -> Any:
    """The row with the warnings added to the ones it already carries."""
    if not warnings:
        return row
    return {**row, "warnings": [*(row.get("warnings") or []), *warnings]}


def _other_id_warning(row: Any, id_key: str, item_id: str) -> str | None:
    """The warning for a restore that gave the item an id other than its own."""
    new_id = row.get(id_key)
    if new_id == item_id:
        return None
    noun = id_key.removesuffix("_id")
    return (
        f"The {noun} came back as {new_id}, not {item_id}: anything that refers "
        f"to the {noun} by {item_id} still matches nothing."
    )


async def _restore_item(
    client: Any,
    rows: list[Any],
    registry: str,
    id_key: str,
    item_id: str,
    payload: dict[str, Any],
    edit_tool: str,
    extra_ids: dict[str, Any] | None = None,
) -> Any:
    """Update the item, or create it again when Core no longer holds its id.

    A row whose fields all match the payload stands for the item an earlier
    restore created, and is returned with its own fields. A create leaves out a
    field that is None: some create schemas (an area's icon and floor) take a
    string or nothing, and nothing is the default. The row carries a warning
    when its id is not the snapshot's. A create Core refuses is raised as
    ``not_applied``, since a refused write changes nothing. When the refusal is
    Core's ``invalid_info`` and a row holds the name, the error names that row
    and ``edit_tool``, the call that edits an item of this registry (``{id}``
    stands for its id). Removing the holder is not suggested: it can be the item
    an earlier restore of this backup created, and removing it takes it off
    everything that carries it.
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
    row = _like(rows, payload)
    if row is None:
        fields = {k: v for k, v in payload.items() if v is not None}
        try:
            row = await bm._ws_send(
                client, {"type": f"config/{registry}/create", **extra_ids, **fields}
            )
        except HomeAssistantCommandError as err:
            from .tools.helpers import core_error_message  # import cycle, as _rows

            noun = id_key.removesuffix("_id")
            core = core_error_message(err)
            holder = (
                _holding_name(rows, payload) if err.code == "invalid_info" else None
            )
            if holder is None:
                raise bm.BackupRestoreError(
                    f"{core}: the removed {noun} {item_id} was not created again. "
                    "Nothing was changed."
                ) from err
            held_id = holder.get(id_key)
            raise bm.BackupRestoreError(
                f"{core}: the {noun} {held_id} holds the name {payload.get('name')!r}, "
                f"so the removed {noun} {item_id} was not created again. It can be "
                "the one an earlier restore of this backup created. Nothing was "
                "changed.",
                suggestions=[
                    f"If {held_id} is the {noun} to keep, change it with "
                    + edit_tool.format(id=held_id)
                ],
            ) from err
    return _noting(row, *filter(None, [_other_id_warning(row, id_key, item_id)]))


async def fetch_label(client: Any, entity_id: str) -> Any:
    rows = await _rows(client, "label")
    return next((r for r in rows if r.get("label_id") == entity_id), None)


async def restore_label(client: Any, entity_id: str, config: Any) -> Any:
    rows = await _rows(client, "label")
    payload = _manager()._strip_readonly(config, "label_id")
    return await _restore_item(
        client,
        rows,
        "label_registry",
        "label_id",
        entity_id,
        payload,
        "ha_config_set_label(label_id={id})",
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
        f"ha_config_set_category(scope={scope}, category_id={{id}})",
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
    stored = _manager()._strip_readonly(config, "kind", f"{kind}_id")
    async with _manager().registry_update_lock(kind, real_id):
        rows = await _rows(client, kind)
        row = await _restore_item(
            client,
            rows,
            f"{kind}_registry",
            f"{kind}_id",
            real_id,
            stored,
            f"ha_set_area_or_floor(kind={kind}, id={{id}})",
        )
    if kind == "area":
        return _noting(row, *await _area_warnings(client, stored, row))
    return row


async def _area_warnings(client: Any, stored: dict[str, Any], row: Any) -> list[str]:
    """What Core accepted without complaint, read from the area it answered with.

    Core drops a label id it no longer holds from an area and still answers
    success. It does not check an area's ``floor_id`` either, so the check for a
    floor that is gone is an HA-MCP safeguard, not a Core rule.
    """
    warnings = []
    missing = sorted(set(stored.get("labels") or []) - set(row.get("labels") or []))
    if missing:
        warnings.append(
            f"The area was restored without the labels {', '.join(missing)}: "
            "they no longer exist."
        )
    floor_id = row.get("floor_id")
    if floor_id and floor_id not in {
        f.get("floor_id") for f in await _rows(client, "floor")
    }:
        warnings.append(
            f"The area is on floor {floor_id}, which no longer exists, and Core "
            "accepted it: give the area another floor with ha_set_area_or_floor."
        )
    return warnings


REGISTRY_ITEM_DOMAINS = frozenset({"label", "category", "area_or_floor"})


async def restored_item(client: Any, domain: str, entity_id: str, config: Any) -> Any:
    """The live item that holds a deleted snapshot item's name, as a diff
    compares it, None when there is none.

    A restore of a deleted item creates it again under the id Core derives (a
    renamed label, area or floor) or picks (a category), so a lookup by the
    stored id finds nothing. Names are unique, so the row with the snapshot's
    name is the item a restore created, or another that took the name since
    (which a restore refuses). Its id stays its own, so the diff reports that it differs;
    its read-only timestamps are the snapshot's, since they are no difference the
    snapshot can restore.
    """
    bm = _manager()
    keep = {k: v for k, v in config.items() if k in bm._REGISTRY_READONLY_KEYS}
    if domain == "label":
        row = _holding_name(await _rows(client, "label"), config)
        return row and {**row, **keep}
    if domain == "category":
        scope = entity_id.partition(":")[0]
        row = _holding_name(await _category_rows(client, scope), config)
        return row and {"scope": scope, **row, **keep}
    kind = entity_id.partition(":")[0]
    if domain != "area_or_floor" or kind not in _AREA_OR_FLOOR:
        return None
    row = _holding_name(await _rows(client, kind), config)
    return row and {"kind": kind, **row, **keep}
