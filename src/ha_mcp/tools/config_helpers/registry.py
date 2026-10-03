"""Registry lookups, name-collision checks and entity-registry updates for helpers."""

import asyncio
import logging
from typing import Any

from ...client.rest_client import HomeAssistantAPIError, HomeAssistantAuthError
from ...errors import ErrorCode, create_auth_error, create_error_response
from ...utils.registry_update_lock import registry_update_lock
from ..component_registry_lookup import fetch_entities_for_config_entry_via_component
from ..config_entry_flow import FLOW_HELPER_TYPES
from ..helpers import exception_to_structured_error, raise_tool_error
from ..util_helpers import apply_entity_category
from .schemas import SIMPLE_HELPER_TYPES, _simple_helper_error_context

logger = logging.getLogger(__name__)


async def _ws_registry_lookup(
    client: Any, message: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]], Exception | None, str | None]:
    """Return (ok, items, exc, error_code). ok=False means the lookup failed.

    ok=True with empty list means the registry exists and is genuinely empty —
    distinct from failure so phantom IDs can still be rejected. The fail-open
    ok=False path keeps transient HA outages from blocking legitimate calls.
    ``exc`` carries the raising exception and ``error_code`` the failure
    envelope's preserved HA code (the two failure channels are exclusive), so
    a fail-closed caller can classify auth errors instead of blaming the
    connection.
    """
    try:
        result = await client.send_websocket_message(message)
    except Exception as e:  # noqa: BLE001
        logger.debug("_ws_registry_lookup: failed for %r: %s", message.get("type"), e)
        return False, [], e, None
    if isinstance(result, list):
        return True, result, None, None
    if isinstance(result, dict):
        if result.get("success") is False:
            code = result.get("error_code")
            return False, [], None, code if isinstance(code, str) else None
        inner = result.get("result", [])
        if isinstance(inner, list):
            return True, inner, None, None
    return False, [], None, None


def _registry_id_values(items: list[dict[str, Any]], field: str) -> list[str]:
    """Pull non-empty string values of `field` from a list of registry dicts."""
    return [
        v
        for it in items
        if isinstance(it, dict) and isinstance((v := it.get(field)), str)
    ]


def _ws_error_msg(response: dict[str, Any]) -> str:
    """Extract a human-readable error message from a failed WS response dict."""
    error_detail = response.get("error", {})
    if isinstance(error_detail, dict):
        msg: str = error_detail.get("message", "Unknown error")
        return msg
    return str(error_detail) if error_detail else "Unknown error"


def _raise_if_unknown_labels(
    ws_labels: list[dict[str, Any]], labels: list[str] | None
) -> None:
    """Raise VALIDATION_INVALID_PARAMETER if any label_id is not in the registry."""
    valid_label_ids = _registry_id_values(ws_labels, "label_id")
    # An empty string inside a non-empty list is NOT the clear sentinel
    # (that's the empty list itself) — treat it as an unknown ID so it can't
    # ride along into the registry write.
    unknown = [
        label_id
        for label_id in labels or []
        if not label_id or label_id not in valid_label_ids
    ]
    if unknown:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Unknown label_id(s): {unknown}. These do not exist in "
                "the label registry.",
                context={"labels": labels, "unknown_labels": unknown},
                suggestions=[
                    "Use ha_config_get_label() to list valid label IDs.",
                    "Use ha_config_set_label() to create a new label.",
                    f"Available label_ids: {sorted(valid_label_ids)}",
                ],
            )
        )


def _raise_if_unknown_area(areas: list[dict[str, Any]], area_id: str) -> None:
    """Raise VALIDATION_INVALID_PARAMETER if area_id is not in the registry."""
    valid_area_ids = _registry_id_values(areas, "area_id")
    if area_id not in valid_area_ids:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"area_id={area_id!r} does not exist in the area registry.",
                context={"area_id": area_id},
                suggestions=[
                    "Use ha_list_floors_areas() to list valid area IDs.",
                    'Pass area_id="" to clear the area assignment.',
                    f"Available area_ids: {sorted(valid_area_ids)}",
                ],
            )
        )


def _raise_if_unknown_category(
    ws_categories: list[dict[str, Any]], category: str, scope: str
) -> None:
    """Raise VALIDATION_INVALID_PARAMETER if category is not in the scope's registry."""
    valid_category_ids = _registry_id_values(ws_categories, "category_id")
    if category not in valid_category_ids:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"category={category!r} does not exist in the {scope} "
                "category registry.",
                context={"category": category, "scope": scope},
                suggestions=[
                    f"Use ha_config_get_category(scope='{scope}') to list valid category IDs.",
                    "Use ha_config_set_category() to create a new category.",
                    f"Available category_ids: {sorted(valid_category_ids)}",
                ],
            )
        )


def _raise_if_unknown_floor(floors: list[dict[str, Any]], floor_id: str) -> None:
    """Raise VALIDATION_INVALID_PARAMETER if floor_id is not in the registry."""
    valid_floor_ids = _registry_id_values(floors, "floor_id")
    if floor_id not in valid_floor_ids:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"floor_id={floor_id!r} does not exist in the floor registry.",
                context={"floor_id": floor_id},
                suggestions=[
                    "Use ha_list_floors_areas() to list valid floor IDs.",
                    'Pass floor_id="" to clear the floor assignment.',
                    f"Available floor_ids: {sorted(valid_floor_ids)}",
                ],
            )
        )


# kind -> (param name used in messages/context, registry name used in messages)
_REGISTRY_KINDS = {
    "area": ("area_id", "area"),
    "labels": ("labels", "label"),
    "category": ("category", "category"),
    "floor": ("floor_id", "floor"),
}


def _registry_check_context(kind: str, scope: str, value: Any) -> dict[str, Any]:
    """Error context naming the offending value for the given registry kind."""
    if kind == "category":
        return {"category": value, "scope": scope}
    return {_REGISTRY_KINDS[kind][0]: value}


def _raise_if_lookup_failed(ok: bool, kind: str, scope: str, value: Any) -> None:
    """Fail the write when a registry needed for validation cannot be read."""
    if ok:
        return
    param, registry = _REGISTRY_KINDS[kind]
    registry_name = f"{scope} {registry}" if scope else registry
    raise_tool_error(
        create_error_response(
            ErrorCode.CONNECTION_FAILED,
            f"Could not validate {param} because the {registry_name} "
            "registry is unavailable.",
            context=_registry_check_context(kind, scope, value),
        )
    )


def _registry_checks(
    area_id: str | None,
    labels: list[str] | None,
    categories: dict[str, str | None] | None,
    floor_id: str | None,
) -> list[tuple[str, str, Any, dict[str, Any]]]:
    """Build the (kind, scope, value, ws_message) lookups a validation needs.

    ``scope`` is only meaningful for category checks; other kinds carry "".
    Falsy values are skipped: None means "caller did not pass", and empty
    string / empty list are the documented "clear" sentinels HA accepts.
    """
    checks: list[tuple[str, str, Any, dict[str, Any]]] = []
    if area_id:
        checks.append(("area", "", area_id, {"type": "config/area_registry/list"}))
    if labels:
        checks.append(("labels", "", labels, {"type": "config/label_registry/list"}))
    for scope, category_id in (categories or {}).items():
        if category_id:
            checks.append(
                (
                    "category",
                    scope,
                    category_id,
                    {"type": "config/category_registry/list", "scope": scope},
                )
            )
    if floor_id:
        checks.append(("floor", "", floor_id, {"type": "config/floor_registry/list"}))
    return checks


# HA WS error codes that mean the caller's credentials were rejected —
# preserved by the client's failure envelope (rest_client keeps ``e.code``).
_AUTH_WS_ERROR_CODES = frozenset({"unauthorized"})


def _auth_error_in_chain(exc: BaseException | None) -> bool:
    """True when the failure or its ``__cause__`` chain is an auth rejection."""
    depth = 0
    while exc is not None and depth < 10:
        if isinstance(exc, HomeAssistantAuthError):
            return True
        exc = exc.__cause__
        depth += 1
    return False


def _apply_registry_check(
    kind: str,
    scope: str,
    value: Any,
    ok: bool,
    items: list[dict[str, Any]],
    exc: Exception | None,
    error_code: str | None,
    fail_closed: bool,
) -> None:
    """Reject an unknown ID, or an unreadable registry when failing closed."""
    if fail_closed and not ok:
        if exc is not None:
            # The transport wrap re-raises acquisition failures as
            # HomeAssistantConnectionError ``from`` the original exception
            # (phase-before-type, pinned by
            # test_failure_to_acquire_a_client_raises), so an auth rejection
            # during connect survives only in the ``__cause__`` chain —
            # discriminate on it the same way the envelope branch below
            # discriminates on ``error_code``.
            if _auth_error_in_chain(exc):
                raise_tool_error(
                    create_auth_error(
                        f"Could not validate {_REGISTRY_KINDS[kind][0]}: Home "
                        "Assistant rejected the credentials.",
                        context=_registry_check_context(kind, scope, value),
                    )
                )
            # Preserve the original failure class — a timeout must surface
            # as a timeout, not as generic connection guidance.
            exception_to_structured_error(
                exc, context=_registry_check_context(kind, scope, value)
            )
        if error_code in _AUTH_WS_ERROR_CODES:
            # Failure-shaped envelope with HA's preserved code: past-
            # acquisition auth rejections arrive on this channel, not as a
            # raised exception.
            raise_tool_error(
                create_auth_error(
                    f"Could not validate {_REGISTRY_KINDS[kind][0]}: Home "
                    "Assistant rejected the request as unauthorized.",
                    context=_registry_check_context(kind, scope, value),
                )
            )
        _raise_if_lookup_failed(ok, kind, scope, value)
    if not ok:
        return
    if kind == "area":
        _raise_if_unknown_area(items, value)
    elif kind == "labels":
        _raise_if_unknown_labels(items, value)
    elif kind == "category":
        _raise_if_unknown_category(items, value, scope)
    else:
        _raise_if_unknown_floor(items, value)


async def validate_registry_ids(
    client: Any,
    area_id: str | None,
    labels: list[str] | None,
    categories: dict[str, str | None] | None,
    *,
    floor_id: str | None = None,
    fail_closed: bool = False,
) -> None:
    """Validate that registry references point at entries that actually exist.

    Bug 16 (issue #1150) / issue #2159: HA's registry-update APIs accept any
    string for a cross-registry reference and answer with a success envelope
    either way, so without this check a typo or a since-deleted ID passes as a
    completed write. What happens to the bad value differs per reference:
    area_id / floor_id / category are stored verbatim and become a dangling
    reference, while an unknown label_id is dropped (the area registry filters
    the requested set through the label registry), leaving the caller with a
    success it cannot distinguish from an applied one. Validate before sending
    so the caller gets a clear error with the available IDs to choose from.

    ``categories`` maps a category SCOPE ("helpers", "automation", "script",
    "scene") to the category_id being assigned in that scope; each distinct
    scope is a separate registry lookup. Every lookup runs concurrently.

    Skips:
      - None values (caller did not pass — no change to apply). Inside
        ``categories``, a None value means "clear" for that scope.
      - Empty string area_id / category / floor_id (these mean "clear").
      - Empty list labels (clear semantics).

    Returns None. Raises ToolError with VALIDATION_INVALID_PARAMETER on the
    first unknown ID encountered, with the available IDs included in the
    suggestions list so the caller can correct. ``fail_closed`` additionally
    rejects the write with CONNECTION_FAILED when any requested registry cannot
    be read — a degraded lookup must never let a dangling reference through.
    """
    checks = _registry_checks(area_id, labels, categories, floor_id)
    if not checks:
        return

    results = await asyncio.gather(
        *(_ws_registry_lookup(client, message) for *_, message in checks)
    )
    for (kind, scope, value, _), (ok, items, exc, error_code) in zip(
        checks, results, strict=True
    ):
        _apply_registry_check(
            kind, scope, value, ok, items, exc, error_code, fail_closed
        )


def _slugify_helper_name(name: str) -> str:
    """Derive the slug HA generates from a helper display name.

    Mirrors HA's collection-storage logic: lowercase the name, replace spaces
    with underscores, then strip any non-alphanumeric/underscore characters.
    Used by the Bug 12 collision check so we can compare a caller-supplied
    `name` against existing helpers' IDs without an extra round trip.
    """
    lowered = name.lower().replace(" ", "_")
    return "".join(c for c in lowered if c.isalnum() or c == "_")


async def _find_collision_in_flow_helpers(
    client: Any, helper_type: str, target_slug: str
) -> str | None:
    """Search config-entry registry for a flow helper whose title slugifies to target_slug."""
    try:
        result = await client.send_websocket_message(
            {"type": "config_entries/get", "domain": helper_type}
        )
    except (HomeAssistantAPIError, ConnectionError, TimeoutError):
        # Connectivity issue — fail open so a transient outage doesn't block legit creates;
        # HA will auto-suffix duplicates on its own.
        return None
    entries = result.get("result", []) if isinstance(result, dict) else result
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        if isinstance(title, str) and _slugify_helper_name(title) == target_slug:
            return entry.get("entry_id") or entry.get("id")
    return None


def _flatten_helper_list_result(result: Any) -> list[Any]:
    """Flatten a {type}/list WS response into a flat list of helper dicts.

    Handles the person/list shape ({"storage": [...], "config": [...]}) and
    the standard list shape ([...]). An unrecognised shape yields an empty
    list, which downstream is indistinguishable from a helper type that has no
    entries, so it is logged rather than dropped in silence.
    """
    if isinstance(result, dict):
        inner = result.get("result", [])
        if isinstance(inner, dict):
            items: list[Any] = []
            matched = False
            for key in ("storage", "config"):
                sub = inner.get(key)
                if isinstance(sub, list):
                    matched = True
                    items.extend(sub)
            if not matched:
                logger.warning(
                    "Helper listing has neither a 'storage' nor a 'config' list "
                    "(keys: %s); treating it as empty",
                    sorted(inner),
                )
            return items
        if isinstance(inner, list):
            return inner
    if isinstance(result, list):
        return result
    logger.warning(
        "Cannot flatten a helper listing: expected a list or a storage/config "
        "split, got %s; treating it as empty",
        type(result).__name__,
    )
    return []


async def _find_collision_in_simple_helpers(
    client: Any, helper_type: str, target_slug: str
) -> str | None:
    """Search simple-helper {type}/list for an entry whose id or name slug matches target_slug."""
    try:
        result = await client.send_websocket_message({"type": f"{helper_type}/list"})
    except (HomeAssistantAPIError, ConnectionError, TimeoutError):
        # Connectivity issue — fail open so a transient outage doesn't block legit creates;
        # HA will auto-suffix duplicates on its own.
        return None
    for item in _flatten_helper_list_result(result):
        if not isinstance(item, dict):
            continue
        existing_slug = item.get("id") or item.get("tag_id")
        if isinstance(existing_slug, str) and existing_slug == target_slug:
            return existing_slug
        existing_name = item.get("name")
        if (
            isinstance(existing_name, str)
            and _slugify_helper_name(existing_name) == target_slug
        ):
            return item.get("id") or item.get("tag_id") or target_slug
    return None


_REGISTRY_JOIN_STALE_WARNING = (
    "Could not read the entity registry, so a renamed helper's 'name' may be the "
    "creation-time name and no current 'entity_id' is shown; use ha_search to find "
    "the helper by name for the authoritative current values."
)


async def _enrich_helpers_with_current_registry(
    client: Any, helper_type: str, items: list[Any]
) -> list[str]:
    """Join the entity registry onto storage-collection helper records.

    The ``{helper_type}/list`` response carries the immutable storage ``id``
    (the unique_id) and the creation-time ``name``; after a UI rename the
    current ``entity_id`` and display name live only in the entity registry, so
    the raw list goes stale (issue #1794). For each record matched by
    ``unique_id`` **and** ``platform == helper_type`` this adds the current
    ``entity_id``, moves the storage name to ``original_name``, and sets
    ``name`` to the current display name (registry ``name``, falling back to
    ``original_name``). ``items`` is mutated in place.

    Storage types without a matching entity (e.g. ``tag``) and platform
    mismatches are left untouched. Returns a warnings list — non-empty only
    when the registry read failed, so the caller can flag the un-enriched
    result instead of silently returning stale values.
    """
    if not items:
        # Nothing to enrich — skip the full-registry fetch entirely.
        return []
    # Degrade-open: enrichment is cosmetic, so any failure — the registry read,
    # an unexpected registry shape (e.g. a non-hashable ``id`` breaking the
    # lookup), or a malformed response — flags the result rather than raising
    # out and letting the caller's handler turn a list call into a failure.
    # Mirrors _get_entities_for_config_entry's degrade-open behavior (it now
    # routes through the component's registry_lookup first, falling back to this
    # same config/entity_registry/list read).
    # send_websocket_message answers a command HA rejected with
    # {"success": false, ...}, so the malformed-response check below is the
    # branch production takes for that case; a dead transport raises instead
    # (#1947) and the degrade-open except below turns it back into an empty
    # enrichment, which is correct for a cosmetic field.
    try:
        reg_result = await client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
        # A missing / non-list ``result`` (or a non-dict / unsuccessful response)
        # is a malformed read, not an empty registry — flag it rather than
        # silently returning un-enriched records. A present-but-empty ``[]`` is a
        # legitimate no-match and passes through below without a warning.
        if not (
            isinstance(reg_result, dict)
            and reg_result.get("success")
            and isinstance(reg_result.get("result"), list)
        ):
            logger.debug(
                "list_helpers registry enrichment: malformed registry response: %r",
                reg_result,
            )
            return [_REGISTRY_JOIN_STALE_WARNING]
        registry = reg_result["result"]
        reg_by_uid = {
            entry["unique_id"]: entry
            for entry in registry
            if isinstance(entry, dict)
            and entry.get("platform") == helper_type
            and entry.get("unique_id")
        }
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = reg_by_uid.get(item.get("id"))
            if entry is None:
                continue
            item["entity_id"] = entry.get("entity_id")
            item["original_name"] = item.get("name")
            item["name"] = (
                entry.get("name") or entry.get("original_name") or item.get("name")
            )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"list_helpers registry enrichment failed: {e}")
        return [_REGISTRY_JOIN_STALE_WARNING]
    return []


async def _check_name_collision(
    client: Any,
    helper_type: str,
    name: str | None,
) -> None:
    """Reject create requests whose name collides with an existing helper (Bug 12).

    HA's create endpoints auto-suffix duplicate names with `_2` / `_3` etc., so
    a caller asking to "create" a helper that already exists silently gets a
    duplicate entity instead of updating the original. Detect and reject before
    we send the create message, pointing the caller at the existing helper_id.

    Empty / missing / whitespace-only `name` is left to the existing
    name-required check downstream so the user sees the standard "name is
    required" error rather than a spurious collision miss (or a wasted WS
    round-trip on a name the validator is about to reject anyway).
    """
    if not name or not name.strip():
        return
    target_slug = _slugify_helper_name(name)
    if not target_slug:
        # Name normalises to empty (e.g. all punctuation). HA's create call
        # will reject; let it surface that error rather than guessing.
        return

    if helper_type in FLOW_HELPER_TYPES:
        existing_id = await _find_collision_in_flow_helpers(
            client, helper_type, target_slug
        )
    else:
        existing_id = await _find_collision_in_simple_helpers(
            client, helper_type, target_slug
        )

    if existing_id is None:
        return

    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"A {helper_type} helper named {name!r} already exists "
            f"(id: {existing_id!r}). Pass helper_id={existing_id!r} to update it, "
            f"or use a different name to create a new helper.",
            context=_simple_helper_error_context(
                helper_type,
                name=name,
                existing_helper_id=existing_id,
            )
            if helper_type in SIMPLE_HELPER_TYPES
            else {
                "helper_type": helper_type,
                "name": name,
                "existing_helper_id": existing_id,
            },
            suggestions=[
                f"To update the existing helper, pass helper_id={existing_id!r} "
                "(and omit `name`).",
                "To create a separate helper, pick a name whose slug does not "
                f"already exist (current collision: {target_slug!r}).",
            ],
        )
    )


async def _get_entities_for_config_entry(
    client: Any, entry_id: str, warnings: list[str] | None = None
) -> list[dict[str, Any]]:
    """Return all entity_registry entries linked to the given config_entry_id.

    When the component advertises ``registry_lookup`` a single in-process
    ``registry_lookup(config_entry_id=...)`` read returns the rows already scoped
    to the entry (byte-identical ``as_partial_dict`` shape), replacing the whole
    ``config/entity_registry/list`` dump; on capability miss / component error the
    legacy dump runs. Either way, multi-entity helpers (e.g. utility_meter with
    tariffs) are handled naturally — all entities for the same entry are returned.

    On WebSocket failure (e.g. HA mid-restart, auth lost, connection drop) the
    caller would otherwise see `entity_ids: []` and be told that registry-update
    targets like `area_id` / `labels` were silently dropped. If `warnings` is
    provided, append a concrete message so the caller surfaces the partial
    failure instead. Each read is guarded separately so the warning names the
    call that actually failed (``registry_lookup`` for the component read vs
    ``entity_registry/list`` for the legacy dump). This consumer swallows ALL
    registry-read failures into ``warnings`` and returns ``[]`` (a flow-helper
    delete's REST step can still succeed), so a propagated
    ``HomeAssistantConnectionError`` from either read is converted here, not
    raised.
    """
    try:
        rows = await fetch_entities_for_config_entry_via_component(client, entry_id)
    except Exception as e:  # noqa: BLE001
        if warnings is not None:
            warnings.append(
                f"registry_lookup failed for config_entry_id={entry_id}: {e}"
            )
        return []
    if rows is not None:
        # Component-served rows are ALREADY scoped to the entry.
        return rows
    try:
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
    except Exception as e:  # noqa: BLE001
        if warnings is not None:
            warnings.append(
                f"entity_registry/list failed for config_entry_id={entry_id}: {e}"
            )
        return []

    # Success path: message can come back as a bare list or wrapped in
    # {"success": True, "result": [...]}. Treat a false success flag as an
    # error that should surface in warnings rather than silently returning [].
    if isinstance(result, dict) and result.get("success") is False:
        if warnings is not None:
            warnings.append(
                f"entity_registry/list failed for config_entry_id={entry_id}: "
                f"{_ws_error_msg(result)}"
            )
        return []

    entries = result if isinstance(result, list) else result.get("result", [])
    if not isinstance(entries, list):
        if warnings is not None:
            warnings.append(
                f"entity_registry/list returned unexpected shape for "
                f"config_entry_id={entry_id}"
            )
        return []
    return [e for e in entries if e.get("config_entry_id") == entry_id]


async def _entity_registry_update_coro(
    client: Any,
    entity_id: str,
    area_id: str | None,
    labels: list[str] | None,
    icon: str | None = None,
) -> Any:
    """Build and send a config/entity_registry/update WS message."""
    update_message: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if area_id is not None:
        update_message["area_id"] = area_id if area_id else None
    if labels is not None:
        update_message["labels"] = labels
    if icon is not None:
        update_message["icon"] = icon if icon else None
    async with registry_update_lock("entity", entity_id):
        return await client.send_websocket_message(update_message)


async def _category_apply_coro(
    client: Any, entity_id: str, category: str
) -> dict[str, Any]:
    """Apply category to entity and return the ack dict."""
    cat_ack: dict[str, Any] = {}
    await apply_entity_category(
        client, entity_id, category, "helpers", cat_ack, "helper"
    )
    return cat_ack


def _process_reg_update_result(
    reg_result: Any,
    area_id: str | None,
    labels: list[str] | None,
    icon: str | None,
    applied: dict[str, Any],
    entity_id: str,
    warnings: list[str],
) -> None:
    """Update applied dict and warnings list based on entity_registry/update outcome."""
    if isinstance(reg_result, BaseException):
        warnings.append(f"{entity_id}: entity registry update raised: {reg_result}")
    elif reg_result is not None:
        if reg_result.get("success"):
            if area_id is not None:
                applied["area_id"] = area_id if area_id else None
            if labels is not None:
                applied["labels"] = labels
            if icon is not None:
                applied["icon"] = icon if icon else None
        else:
            # area_id/labels/icon share one WS message, so a rejection of any
            # one sinks the others. Name the batched fields so the caller can
            # tell which touchups didn't land instead of guessing.
            sent_fields = [
                f
                for f, v in (("area_id", area_id), ("labels", labels), ("icon", icon))
                if v is not None
            ]
            field_note = f" (fields: {', '.join(sent_fields)})" if sent_fields else ""
            warnings.append(
                f"{entity_id}: entity registry update failed{field_note}: "
                f"{_ws_error_msg(reg_result)}"
            )


def _process_cat_apply_result(
    cat_result: Any,
    entity_id: str,
    applied: dict[str, Any],
    warnings: list[str],
) -> None:
    """Update applied dict and warnings list based on category apply outcome."""
    if isinstance(cat_result, BaseException):
        warnings.append(f"{entity_id}: category apply raised: {cat_result}")
    elif cat_result is not None:
        if "category" in cat_result:
            applied["category"] = cat_result["category"]
        elif cat_result.get("warnings"):
            warnings.extend(f"{entity_id}: {w}" for w in cat_result["warnings"])


async def _apply_registry_updates_to_entity(
    client: Any,
    entity_id: str,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    icon: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Apply area_id/labels/icon (single WS call) and category (shared helper) to one entity.

    Appends human-readable warning strings to `warnings` on any failure.
    Returns a small dict summarizing what was applied (for result building).
    """
    applied: dict[str, Any] = {"entity_id": entity_id}

    # Run the two independent registry calls concurrently.
    # `is not None` distinguishes "not provided" from "explicit clear" (empty
    # string / empty list). A transient raise on either call is captured via
    # return_exceptions so a multi-entity flow helper can still report partial success.
    needs_registry = area_id is not None or labels is not None or icon is not None
    needs_category = bool(category)
    if not (needs_registry or needs_category):
        return applied

    reg_task = (
        _entity_registry_update_coro(client, entity_id, area_id, labels, icon)
        if needs_registry
        else None
    )
    cat_task = (
        _category_apply_coro(client, entity_id, category)  # type: ignore[arg-type]
        if needs_category
        else None
    )
    coros = [c for c in (reg_task, cat_task) if c is not None]
    raw_results: list[Any] = list(await asyncio.gather(*coros, return_exceptions=True))
    reg_result = raw_results.pop(0) if needs_registry else None
    cat_result = raw_results.pop(0) if needs_category else None

    _process_reg_update_result(
        reg_result, area_id, labels, icon, applied, entity_id, warnings
    )
    _process_cat_apply_result(cat_result, entity_id, applied, warnings)
    return applied
