"""Storage-helper payloads built generically and validated by Core (#2632).

Home Assistant validates a storage helper's create and update against its own
schema, and its messages name the problem and the field ("value must be one of
['box', 'slider'] at 'mode'", "not a valid option, did you mean 'minimum'?").
So the payload is the caller's fields under Core's names, sent as-is; this
module only adds what Core does not do itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any

from ...errors import ErrorCode, create_error_response
from ..helpers import raise_tool_error
from .schemas import _simple_helper_error_context

# The tool's own range parameters, under the names each type's Core schema uses.
_RANGE_KEYS: dict[str, tuple[str, str]] = {"counter": ("minimum", "maximum")}
_DEFAULT_RANGE_KEYS = ("min", "max")


def core_fields(
    helper_type: str, type_kw: dict[str, Any], passthrough: dict[str, Any]
) -> dict[str, Any]:
    """The caller's fields under Core's names; unknown keys go to Core as given.

    Core rejects a key its schema lacks and suggests the closest one, which is
    more useful than a local "unknown key" error.
    """
    low, high = _RANGE_KEYS.get(helper_type, _DEFAULT_RANGE_KEYS)
    rename = {"min_value": low, "max_value": high}
    fields = {rename.get(k, k): v for k, v in type_kw.items() if v is not None}
    # These address the WebSocket request (its command, the item to update),
    # not a field of the item.
    if reserved := sorted({"type", "id", f"{helper_type}_id"} & passthrough.keys()):
        _reject(
            helper_type,
            f"config keys {', '.join(reserved)} address the request, not a "
            f"field of the {helper_type}.",
        )
    fields.update(passthrough)
    return fields


def with_create_defaults(helper_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Defaults this tool documents that Core does not apply on create."""
    fields = dict(fields)
    if helper_type == "input_datetime" and not {"has_date", "has_time"} & set(fields):
        # Core rejects an input_datetime with neither component.
        fields.update(has_date=True, has_time=True)
    if helper_type == "tag":
        fields.setdefault("tag_id", uuid.uuid4().hex)
    return fields


def merged_update(
    helper_type: str,
    stored: dict[str, Any],
    name: str | None,
    icon: str | None,
    fields: dict[str, Any],
    entity_id: str,
) -> dict[str, Any]:
    """The stored item plus the changes: Core's update writes the whole item.

    A cleared icon ('') is left out, since Core rejects an empty icon. A tag's
    name lives in the entity registry, and Core's tag update writes the name
    it is sent there: the listed one is not echoed, or a tag that was never
    named would get its default "Tag <id>" pinned as a registry override.
    """
    body = {k: v for k, v in stored.items() if k != "id"}
    if helper_type == "tag":
        body.pop("name", None)
    if name is not None:
        body["name"] = name
    if helper_type == "zone":
        icon = _zone_item_icon(stored, icon, entity_id)
    if icon is not None:
        if icon:
            body["icon"] = icon
        else:
            body.pop("icon", None)
    body.update(fields)
    return body


def _zone_item_icon(
    stored: dict[str, Any], icon: str | None, entity_id: str
) -> str | None:
    """The icon to write into a zone's stored item, ``None`` to leave it (#2643).

    The zone entity shows the registry icon over the item's, and Core's zone
    update merges into the item and rejects an empty icon, so an icon stored
    there can never be removed. A new icon therefore goes into the item only
    when it already holds one; otherwise the registry alone carries it and
    stays clearable. Clearing an icon the item holds is refused.
    """
    if not stored.get("icon"):
        return None
    if icon == "":
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"The icon of {entity_id} cannot be removed: the zone's stored "
                f"icon ({stored['icon']}) is kept by Home Assistant's zone update "
                "and would show again.",
                context=_simple_helper_error_context("zone", entity_id=entity_id),
                suggestions=["Set a different icon instead of an empty one"],
            )
        )
    return icon


def _reject(helper_type: str, message: str, **context: Any) -> None:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            message,
            context=_simple_helper_error_context(helper_type, **context),
        )
    )


def check_core_gaps(
    helper_type: str, body: dict[str, Any], changed: Collection[str] | None = None
) -> None:
    """Reject the few unusable configurations Core accepts.

    Core enforces every other rule this tool used to check by hand; these three
    it stores as given, producing a counter or slider that cannot work. On an
    update ``changed`` names the caller's fields: a range Core already stores
    is judged only when one of its bounds or the step is among them, so a
    rename or an icon change of such a helper is not refused.
    """
    if helper_type not in ("counter", "input_number"):
        return
    low_key, high_key = _RANGE_KEYS.get(helper_type, _DEFAULT_RANGE_KEYS)
    if changed is not None and not {low_key, high_key, "step"} & set(changed):
        return
    low, high, step = body.get(low_key), body.get(high_key), body.get("step")
    if helper_type == "counter":
        if low is not None and high is not None and low >= high:
            _reject(
                helper_type,
                f"counter {low_key} ({low}) must be less than {high_key} ({high}).",
                **{low_key: low, high_key: high},
            )
        if step is not None and step <= 0:
            _reject(helper_type, f"counter step must be > 0 (got {step}).", step=step)
    if (
        step is not None
        and low is not None
        and high is not None
        and 0 < high - low < step
    ):
        _reject(
            helper_type,
            f"step ({step}) is larger than the range {low_key}..{high_key} "
            f"({high - low}); the control could not reach any value in between.",
            step=step,
            **{low_key: low, high_key: high},
        )
