"""Create path for simple (non-flow) helper types."""

from typing import Any

from ...errors import ErrorCode, create_error_response
from ...utils.registry_update_lock import registry_update_lock
from ..component_helper_collections import (
    collection_payload,
    native_result,
    tag_entity_id,
    write_helper_item,
)
from ..config_write_helpers import apply_entity_category
from ..helpers import raise_tool_error, ws_failure_code
from ..ws_waiters import wait_for_entity_registered
from .core_payload import check_core_gaps, with_create_defaults
from .registry import _ws_error_msg
from .schemas import (
    _attach_helper_skill,
    _helper_response,
    _simple_helper_error_context,
)


def _build_create_message(
    helper_type: str, name: str, icon: str | None, fields: dict[str, Any]
) -> dict[str, Any]:
    """The WebSocket {type}/create message: the caller's fields under Core's names."""
    message: dict[str, Any] = {"type": f"{helper_type}/create", "name": name}
    if icon:
        message["icon"] = icon
    message.update(with_create_defaults(helper_type, fields))
    check_core_gaps(helper_type, message)
    return message


async def _apply_create_entity_registry(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    helper_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply area/labels registry update after a simple-helper create; echo into helper_data."""
    if area_id is None and labels is None:
        return
    update_message: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if area_id is not None:
        update_message["area_id"] = area_id if area_id else None
    if labels is not None:
        update_message["labels"] = labels
    async with registry_update_lock("entity", entity_id):
        update_result = await client.send_websocket_message(update_message)
    if update_result.get("success"):
        if icon is not None:
            helper_data["icon"] = icon if icon else None
        if area_id is not None:
            helper_data["area_id"] = area_id if area_id else None
        if labels is not None:
            helper_data["labels"] = labels
    else:
        warnings.append(
            f"Helper created but entity registry update failed: {_ws_error_msg(update_result)}"
        )


async def _apply_create_category(
    client: Any,
    entity_id: str,
    category: str | None,
    helper_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply category to a newly created helper entity."""
    if not (category and entity_id):
        return
    cat_result: dict[str, Any] = {}
    await apply_entity_category(
        client, entity_id, category, "helpers", cat_result, "helper"
    )
    if "category" in cat_result:
        helper_data["category"] = cat_result["category"]
    elif cat_result.get("warnings"):
        warnings.extend(cat_result["warnings"])


async def _execute_create_simple_helper(
    client: Any,
    helper_type: str,
    name: str | None,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    wait: bool,
    MandatoryBPS: bool,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Execute the create path for a simple (non-flow) helper type."""
    if not name or not name.strip():
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"name is required for create action. Include "
                f'"name" as a top-level argument, e.g. '
                f'{{"helper_type": "{helper_type}", "name": "My Helper"}}.',
                suggestions=[
                    'Add "name": "My Helper" at the top level of the JSON arguments',
                    'Or pass "helper_id": "my_helper" if you intended to update an existing helper',
                ],
                context=_simple_helper_error_context(helper_type),
            )
        )

    message = _build_create_message(helper_type, name, icon, fields)
    native = await _create_via_component(
        client, helper_type, message, area_id, labels, category
    )
    if native is not None:
        helper_data, entity_id, warnings = native
        create_response = _helper_response(
            "create",
            helper_type,
            data=helper_data,
            entity_id=entity_id,
            message=f"Successfully created {helper_type}: {name}",
            warnings=warnings,
        )
        _attach_helper_skill(create_response, MandatoryBPS)
        return create_response

    result = await client.send_websocket_message(message)
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ws_failure_code(result),
                f"Failed to create helper: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context(helper_type, name=name),
            )
        )

    helper_data = result.get("result", {})
    entity_id = helper_data.get("entity_id")
    if helper_type == "tag":
        entity_id = await tag_entity_id(client, helper_data.get("id")) or entity_id
    if not entity_id and helper_data.get("id"):
        entity_id = f"{helper_type}.{helper_data['id']}"

    warnings = []
    # Tags live in their own tag registry and never appear in /api/states/<entity_id> —
    # polling there always 404s for the full timeout (~10s per tag), burning CI time.
    if wait and entity_id and helper_type != "tag":
        try:
            registered = await wait_for_entity_registered(client, entity_id)
            if not registered:
                warnings.append(
                    f"Helper created but {entity_id} not yet queryable. It may take a moment to become available."
                )
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Helper created but verification failed: {e}")

    if entity_id:
        await _apply_create_entity_registry(
            client, entity_id, icon, area_id, labels, helper_data, warnings
        )
        await _apply_create_category(client, entity_id, category, helper_data, warnings)

    create_response = _helper_response(
        "create",
        helper_type,
        data=helper_data,
        entity_id=entity_id,
        message=f"Successfully created {helper_type}: {name}",
        warnings=warnings,
    )
    _attach_helper_skill(create_response, MandatoryBPS)
    return create_response


async def _create_via_component(
    client: Any,
    helper_type: str,
    message: dict[str, Any],
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
) -> tuple[dict[str, Any], str, list[str]] | None:
    """Create through Core's collection in-process; ``None`` uses the WS command.

    The entity exists when Core's create returns, so no ``wait`` polling is needed.
    """
    registry = {
        key: value
        for key, value in (
            ("area_id", area_id),
            ("labels", labels),
            ("category", category),
        )
        if value is not None
    }
    result = await write_helper_item(
        client,
        helper_type,
        "create",
        collection_payload(helper_type, message),
        registry=registry,
        error_context=_simple_helper_error_context(
            helper_type, name=message.get("name")
        ),
    )
    return None if result is None else native_result(helper_type, result)
