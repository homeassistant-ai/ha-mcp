"""Update path for simple (non-flow) helper types.

Core's storage-collection update replaces the whole item, so every update is
the stored item plus the caller's changes (:func:`merged_update`), validated by
Core itself. The component writes it in-process; without the component the
item is read from ``<type>/list`` and sent to ``<type>/update``.
"""

from typing import Any

from ...errors import ErrorCode, create_error_response
from ...utils.registry_update_lock import registry_update_lock
from ..component_helper_collections import (
    collection_payload,
    native_result,
    read_helper_item,
    tag_entity_id,
    tag_item_id,
    write_helper_item,
)
from ..config_write_helpers import apply_entity_category
from ..helpers import raise_tool_error, ws_failure_code
from ..ws_waiters import wait_for_entity_registered
from .core_payload import check_core_gaps, merged_update
from .registry import _ws_error_msg
from .schemas import (
    _attach_helper_skill,
    _helper_response,
    _simple_helper_error_context,
)


async def _stored_item(
    client: Any, helper_type: str, entity_id: str, unique_id: str
) -> dict[str, Any]:
    """The stored item from ``<type>/list`` (person nests its storage items)."""
    list_result = await client.send_websocket_message({"type": f"{helper_type}/list"})
    if not list_result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to fetch {helper_type} config list: "
                f"{list_result.get('error', 'Unknown')}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    listed = list_result.get("result") or []
    items = listed.get("storage", []) if isinstance(listed, dict) else listed
    existing = next(
        (i for i in items if isinstance(i, dict) and i.get("id") == unique_id), None
    )
    if not existing:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_NOT_FOUND,
                f"{helper_type} config not found for id: {unique_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    return existing


async def _execute_legacy_update(
    client: Any,
    helper_type: str,
    entity_id: str,
    unique_id: str,
    name: str | None,
    icon: str | None,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Update through Core's ``<type>/update`` WS command (no component)."""
    stored = await _stored_item(client, helper_type, entity_id, unique_id)
    body = merged_update(helper_type, stored, name, icon, fields, entity_id)
    check_core_gaps(helper_type, body)
    result = await client.send_websocket_message(
        {"type": f"{helper_type}/update", f"{helper_type}_id": unique_id, **body}
    )
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ws_failure_code(result),
                f"Failed to update {helper_type} config: "
                f"{result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    return result.get("result", {})  # type: ignore[no-any-return]


async def _resolve_update_unique_id(
    client: Any,
    helper_type: str,
    entity_id: str,
    helper_id: str | None,
    name: str | None,
) -> str:
    """Look up the unique_id for a helper entity via the entity registry."""
    registry_result = await client.send_websocket_message(
        {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }
    )
    if not registry_result.get("success"):
        suggestions = [
            f"Verify the helper_id={helper_id!r} exists "
            "(use ha_config_list_helpers to list current helpers)",
        ]
        if name:
            suggestions.append(
                f"If you meant to create a new helper named {name!r}, "
                "omit helper_id (or pass action='create')"
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"Could not find {helper_type} entity: {entity_id}",
                context=_simple_helper_error_context(
                    helper_type, entity_id=entity_id, helper_id=helper_id, name=name
                ),
                suggestions=suggestions,
            )
        )
    registry_entry = registry_result.get("result", {})
    if not isinstance(registry_entry, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Unexpected registry response for {entity_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    unique_id = registry_entry.get("unique_id")
    if not unique_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_NOT_FOUND,
                f"No unique_id found in entity registry for {entity_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    return unique_id  # type: ignore[no-any-return]


async def _apply_update_icon_area_labels(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    updated_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply icon/area/labels to the entity registry after a helper update."""
    if icon is None and area_id is None and labels is None:
        return
    registry_update: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if icon is not None:
        registry_update["icon"] = icon if icon else None
    if area_id is not None:
        registry_update["area_id"] = area_id if area_id else None
    if labels is not None:
        registry_update["labels"] = labels
    async with registry_update_lock("entity", entity_id):
        reg_result = await client.send_websocket_message(registry_update)
    if reg_result.get("success"):
        if icon is not None:
            updated_data["icon"] = icon if icon else None
        if area_id is not None:
            updated_data["area_id"] = area_id if area_id else None
        if labels is not None:
            updated_data["labels"] = labels
    else:
        warnings.append(
            f"Config updated but entity registry update failed: {_ws_error_msg(reg_result)}"
        )


async def _apply_update_registry_and_category(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    updated_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply icon/area/labels/category to the entity registry after a helper update."""
    await _apply_update_icon_area_labels(
        client, entity_id, icon, area_id, labels, updated_data, warnings
    )

    if category is not None:
        cat_result: dict[str, Any] = {}
        await apply_entity_category(
            client, entity_id, category, "helpers", cat_result, "helper"
        )
        if "category" in cat_result:
            updated_data["category"] = cat_result["category"]
        elif cat_result.get("warnings"):
            warnings.extend(cat_result["warnings"])


async def _execute_update_simple_helper(
    client: Any,
    helper_type: str,
    entity_id: str,
    helper_id: str | None,
    name: str | None,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    wait: bool,
    MandatoryBPS: bool,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Execute the update path for a simple (non-flow) helper type."""
    if not helper_id or not helper_id.strip():
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "helper_id is required for update action",
                context=_simple_helper_error_context(helper_type),
            )
        )

    warnings: list[str] = []
    native = await _update_via_component(
        client,
        helper_type,
        entity_id,
        helper_id,
        name,
        icon,
        area_id,
        labels,
        category,
        fields,
    )
    if native is not None:
        updated_data, entity_id, warnings = native
    elif helper_type == "tag":
        # Tags have their own registry entity whose id follows the tag's name.
        tag_id = await tag_item_id(client, helper_id)
        updated_data = await _execute_legacy_update(
            client, helper_type, entity_id, tag_id, name, icon, fields
        )
        tag_entity = await tag_entity_id(client, tag_id)
        if tag_entity:
            entity_id = tag_entity
            await _apply_update_registry_and_category(
                client,
                entity_id,
                None,
                area_id,
                labels,
                category,
                updated_data,
                warnings,
            )
    else:
        unique_id = await _resolve_update_unique_id(
            client, helper_type, entity_id, helper_id, name
        )
        updated_data = await _execute_legacy_update(
            client, helper_type, entity_id, unique_id, name, icon, fields
        )
        await _apply_update_registry_and_category(
            client, entity_id, icon, area_id, labels, category, updated_data, warnings
        )
        if wait:
            try:
                if not await wait_for_entity_registered(client, entity_id):
                    warnings.append(
                        f"Update applied but {entity_id} not yet queryable."
                    )
            except Exception as e:  # noqa: BLE001
                warnings.append(f"Update applied but verification failed: {e}")

    update_response = _helper_response(
        "update",
        helper_type,
        data=updated_data,
        entity_id=entity_id,
        message=f"Successfully updated {helper_type}: {entity_id}",
        warnings=warnings,
    )
    _attach_helper_skill(update_response, MandatoryBPS)
    return update_response


async def _update_via_component(
    client: Any,
    helper_type: str,
    entity_id: str,
    helper_id: str,
    name: str | None,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    fields: dict[str, Any],
) -> tuple[dict[str, Any], str, list[str]] | None:
    """Update through Core's collection in-process; ``None`` uses the WS commands.

    A helper the component cannot find also returns ``None``, so the legacy path
    reports it with its usual error.
    """
    if helper_type == "tag":
        target: dict[str, Any] = {"item_id": await tag_item_id(client, helper_id)}
    else:
        target = {"entity_id": entity_id}
    item = await read_helper_item(client, helper_type, **target)
    if item is None:
        return None
    body = merged_update(helper_type, item["item"], name, icon, fields, entity_id)
    check_core_gaps(helper_type, body)
    registry = {
        key: value
        for key, value in (
            ("icon", icon),
            ("area_id", area_id),
            ("labels", labels),
            ("category", category),
        )
        if value is not None
    }
    result = await write_helper_item(
        client,
        helper_type,
        "update",
        collection_payload(helper_type, body),
        item_id=item["item_id"],
        registry=registry,
        error_context=_simple_helper_error_context(helper_type, entity_id=entity_id),
    )
    return None if result is None else native_result(helper_type, result)
