"""A label, category, area or floor in its snapshot and on restore.

Core refuses an update of an id it no longer holds (a label's, an area's and a
floor's with "Unknown error"), so a deleted item is created again. A created
label, area or floor gets the id Core derives from its name, a created category
an id Core picks: neither is the snapshot's after a rename, or for a category
at all, and the restore says so. A second restore takes the row whose fields all
match those a create would send as the one the first restore created. Names are
unique in these registries, so returning that row is right, and the alternative
is Core's "already in use" refusal. A restore Core refuses for that reason names
the item that holds the name, or says Core refused it. The diff finds the live
item that holds a deleted item's name, and reports an id that differs. It
compares the row of an entity and of a device through ``compared_row`` as well.

Which keys a restore sends, which lists match in any order and which points in
time a diff leaves out are asked of the component's
``ha_mcp_tools/registry_fields``; that contract and its fallbacks are in
``docs/agents/native-core-contracts.md`` (Choose the authoritative path).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from .backup_entity_ids import _manager
from .client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)

logger = logging.getLogger(__name__)


async def registry_rows(client: Any, kind: str) -> list[Any]:
    """The rows of the label, area or floor registry, in the order Core lists them."""
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
    # Same component-first routing as ``registry_rows``; categories are scoped, so the
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


def _element_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=repr)


def _same(a: Any, b: Any) -> bool:
    # Two lists are the same when they hold the same items, in any order;
    # ``reordered_lists`` names the ones whose order differs. Elements keep
    # their type: 1 is not "1".
    if isinstance(a, list) and isinstance(b, list):
        return Counter(map(_element_key, a)) == Counter(map(_element_key, b))
    return bool(a == b)


def reordered_lists(current: dict[str, Any], stored: dict[str, Any]) -> list[str]:
    """The lists of the live row that hold the snapshot's items in another order."""
    return [
        key
        for key, value in current.items()
        if isinstance(value, list)
        and isinstance(stored.get(key), list)
        and value != stored[key]
        and _same(value, stored[key])
    ]


def _well_formed(answer: dict[str, Any]) -> bool:
    """Whether a ``registry_fields`` answer names the fields as this module
    reads them."""
    return isinstance(answer.get("update_fields"), list) and all(
        key in answer and isinstance(answer[key], list | None)
        for key in ("create_fields", "set_fields", "stamped_fields")
    )


async def _registry_fields(
    client: Any, registry: str, item_id: Any = None, scope: Any = None
) -> dict[str, Any] | None:
    """The component's ``registry_fields`` answer for a registry and, with
    ``item_id``, for one of its entries; None when the component cannot be asked
    (no usable component, one without the command, command not sent or left
    without an answer), refuses or answers in another shape."""
    from .tools.component_api import ask_component  # import cycle, as registry_rows

    params = {"registry": registry}
    if item_id is not None:
        params["item_id"] = item_id
    if scope:
        params["scope"] = scope
    try:
        answer = await ask_component(client, "registry_fields", **params)
    except HomeAssistantConnectionError as exc:
        logger.warning(
            "registry_fields got no answer for %s; going on without it: %r",
            params,
            exc,
        )
        return None
    if answer is not None and not _well_formed(answer):
        logger.warning(
            "registry_fields answered %s in another shape (keys %s); going on "
            "without it",
            params,
            sorted(answer),
        )
        return None
    return answer


def _only(row: dict[str, Any], keys: Any) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k in (keys or ())}


def _without(row: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in keys}


def _get_the_component() -> str:
    # Import cycle, as registry_rows.
    from .tools.component_api import COMPONENT_VIA_HACS

    return (
        "If the component is missing or older than this server, install or "
        f"update it {COMPONENT_VIA_HACS}"
    )


def _order_untold(what: str, lists: list[str]) -> str:
    """The warning for lists whose order the component did not rule on."""
    return (
        f"{what} the items in {', '.join(sorted(lists))} in another order than in "
        "the backup. Home Assistant keeps some lists of a registry entry in a "
        "set, whose order means nothing. Whether that holds for these lists only "
        f"the ha_mcp_tools component can tell, and it did not. {_get_the_component()}"
    )


def _stamps_untold(keys: list[str]) -> str:
    """The warning for points in time the component did not rule on."""
    return (
        f"The diff reports {', '.join(keys)} as a difference. Home Assistant "
        "sets some points in time of a registry entry itself, and no restore "
        "puts those back. Whether that holds for these only the ha_mcp_tools "
        f"component can tell, and it did not. {_get_the_component()}"
    )


def _not_restored(
    noun: str,
    command: str,
    stored: dict[str, Any],
    sent: dict[str, Any],
    row: Any,
    taken: dict[str, Any] | None,
) -> list[str]:
    """The warning for the stored keys a restore left out of ``command``.

    ``taken`` is the component's answer and ``row`` the row Core has after the
    restore. A key that row carries and neither of Core's commands takes is one
    Core sets itself (``created_at``): no restore puts it back, and it is not
    named. Without the component's answer the restore leaves out
    ``created_at`` and ``modified_at`` only, and nothing is named.
    """
    if taken is None:
        return []
    writable = {*taken["update_fields"], *(taken["create_fields"] or ())}
    keys = sorted(
        key for key in stored if key not in sent and (key in writable or key not in row)
    )
    if not keys:
        return []
    return [
        f"Not restored: {', '.join(keys)}. The backup holds a value for each, and "
        f"Home Assistant's command to {command} a {noun} does not take it."
    ]


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
    id_key: str,
    item_id: str,
    stored: dict[str, Any],
    edit_tool: str,
    extra_ids: dict[str, Any] | None = None,
) -> Any:
    """Update the item, or create it again when Core no longer holds its id.

    ``stored`` is the snapshot's row without its own id and the ``kind`` or
    ``scope`` the snapshot adds. The keys sent, and when a list matches in
    another order, follow ``registry_fields`` (module docstring); the row
    carries a warning for the stored keys left out (``_not_restored``).

    A row whose fields all match the create's stands for the item an earlier
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
    noun = id_key.removesuffix("_id")
    registry = f"{noun}_registry"
    taken = await _registry_fields(client, noun)
    if taken is None:
        update = payload = bm._strip_readonly(stored)
    else:
        update = _only(stored, taken["update_fields"])
        payload = _only(stored, taken["create_fields"])
    # Read after the component was asked for the registry's fields, so that a
    # row removed from Core during that question is neither updated nor taken.
    if noun == "category":
        rows = await _category_rows(client, extra_ids["scope"])
    else:
        rows = await registry_rows(client, noun)
    if any(r.get(id_key) == item_id for r in rows):
        row = await bm._ws_send(
            client,
            {
                "type": f"config/{registry}/update",
                **extra_ids,
                id_key: item_id,
                **update,
            },
        )
        return _noting(row, *_not_restored(noun, "update", stored, update, row, taken))
    row = _like(rows, payload)
    order_warning = None
    if row is not None and (lists := reordered_lists(row, payload)):
        entry = await _registry_fields(
            client, noun, row.get(id_key), extra_ids.get("scope")
        )
        if entry is None:
            order_warning = _order_untold(
                f"The {noun} {row.get(id_key)} was taken as the one an earlier "
                "restore created, with",
                lists,
            )
        elif not set(lists) <= set(entry["set_fields"] or ()):
            row = None
    if row is None:
        fields = {k: v for k, v in payload.items() if v is not None}
        try:
            row = await bm._ws_send(
                client, {"type": f"config/{registry}/create", **extra_ids, **fields}
            )
        except HomeAssistantCommandError as err:
            # Import cycle, as registry_rows.
            from .tools.helpers import core_error_message

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
    return _noting(
        row,
        *filter(None, [order_warning, _other_id_warning(row, id_key, item_id)]),
        *_not_restored(noun, "create", stored, payload, row, taken),
    )


async def fetch_label(client: Any, entity_id: str) -> Any:
    rows = await registry_rows(client, "label")
    return next((r for r in rows if r.get("label_id") == entity_id), None)


async def restore_label(client: Any, entity_id: str, config: Any) -> Any:
    return await _restore_item(
        client,
        "label_id",
        entity_id,
        _without(config, "label_id"),
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
    return await _restore_item(
        client,
        "category_id",
        cat_id,
        _without(config, "category_id", "scope"),
        f"ha_config_set_category(scope={scope}, category_id={{id}})",
        {"scope": scope},
    )


async def fetch_area_or_floor(client: Any, entity_id: str) -> Any:
    kind, _, real_id = entity_id.partition(":")
    if kind not in _AREA_OR_FLOOR or not real_id:
        return None
    for item in await registry_rows(client, kind):
        if item.get(f"{kind}_id") == real_id:
            return {"kind": kind, **item}
    return None


async def restore_area_or_floor(client: Any, entity_id: str, config: Any) -> Any:
    kind, _, real_id = entity_id.partition(":")
    if kind not in _AREA_OR_FLOOR:
        raise ValueError(f"Unknown area/floor kind: {kind!r}")
    # ``floor_id`` is the id of a floor but a field of an area.
    stored = _without(config, "kind", f"{kind}_id")
    async with _manager().registry_update_lock(kind, real_id):
        row = await _restore_item(
            client,
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
        f.get("floor_id") for f in await registry_rows(client, "floor")
    }:
        warnings.append(
            f"The area is on floor {floor_id}, which no longer exists, and Core "
            "accepted it: give the area another floor with ha_set_area_or_floor."
        )
    return warnings


REGISTRY_ITEM_DOMAINS = frozenset({"label", "category", "area_or_floor"})


async def _restored_item(client: Any, domain: str, entity_id: str, config: Any) -> Any:
    """The live item that holds a deleted snapshot item's name, None when there
    is none.

    A restore of a deleted item creates it again under the id Core derives (a
    renamed label, area or floor) or picks (a category), so a lookup by the
    stored id finds nothing. Names are unique, so the row with the snapshot's
    name is the item a restore created, or another that took the name since
    (which a restore refuses). Its id stays its own, so the diff reports that it
    differs.
    """
    if domain == "label":
        return _holding_name(await registry_rows(client, "label"), config)
    if domain == "category":
        scope = entity_id.partition(":")[0]
        row = _holding_name(await _category_rows(client, scope), config)
        return row and {"scope": scope, **row}
    kind = entity_id.partition(":")[0]
    if domain != "area_or_floor" or kind not in _AREA_OR_FLOOR:
        return None
    row = _holding_name(await registry_rows(client, kind), config)
    return row and {"kind": kind, **row}


COMPARED_ROW_DOMAINS = REGISTRY_ITEM_DOMAINS | {"device", "entity"}


async def compared_row(
    client: Any, domain: str, entity_id: str, config: dict[str, Any], current: Any
) -> tuple[Any, list[str]]:
    """The live registry row of a snapshot's item, as a diff compares it, and
    the warnings for the comparison.

    A list Core keeps in a set gets the snapshot's order, and a point in time
    Core sets itself the snapshot's value, as ``registry_fields`` names them
    (module docstring). The component is asked once, and only when the rows
    differ.
    """
    if current is None:
        # A restored label, category, area or floor can carry another id.
        current = await _restored_item(client, domain, entity_id, config)
        if current is None:
            return None, []
    if current == config:
        return current, []
    warnings: list[str] = []
    registry = current["kind"] if domain == "area_or_floor" else domain
    item_id = current.get("id" if domain == "device" else f"{registry}_id")
    entry = await _registry_fields(client, registry, item_id, current.get("scope"))
    sets: Any = None if entry is None else entry["set_fields"]
    stamped: Any = None if entry is None else entry["stamped_fields"]
    if lists := reordered_lists(current, config):
        if sets is None:
            warnings.append(_order_untold("The diff reports as a difference", lists))
        else:
            in_order = {key: config[key] for key in lists if key in sets}
            current = {**current, **in_order}
    if stamped is None:
        readonly = _manager()._REGISTRY_READONLY_KEYS
        if domain in REGISTRY_ITEM_DOMAINS:
            stamped = readonly
        else:
            stamped = ()
            untold = sorted(
                k for k in readonly if k in config and current.get(k) != config[k]
            )
            if untold:
                warnings.append(_stamps_untold(untold))
    return {**current, **_only(config, stamped)}, warnings
