"""Storage-helper payloads built generically and validated by Core (#2632).

Home Assistant validates a storage helper's create and update against its own
schema, and its messages name the problem and the field ("value must be one of
['box', 'slider'] at 'mode'", "not a valid option, did you mean 'minimum'?").
So the payload is the caller's fields under Core's names, sent as-is; this
module only adds what Core does not do itself.
"""

from __future__ import annotations

from typing import Any

from ...errors import ErrorCode, create_error_response
from ..helpers import clear_or_keep, raise_tool_error
from .schemas import _simple_helper_error_context


def storage_fields(
    helper_type: str, config: Any, name: str | None, icon: str | None
) -> tuple[str | None, str | None, dict[str, Any]]:
    """A storage helper's ``config`` as the fields sent to Core, with its
    name and icon.

    ``config`` goes to Core as written, under Core's own field names: Core
    rejects a key its schema lacks and suggests the closest one. ``name`` and
    ``icon`` are fields of Core's schema too, so ``config`` may carry them.
    """
    if config in (None, ""):
        fields: dict[str, Any] = {}
    elif isinstance(config, dict):
        fields = dict(config)
    else:
        _reject(
            helper_type,
            f"config must be an object for helper_type='{helper_type}': a "
            "storage helper is written in one call.",
        )
    # These address the WebSocket request (its command, the item to update),
    # not a field of the item. A tag's ``tag_id`` is the one that is both:
    # Core's tag create takes it as a field.
    addressing = {"type", "id"} | ({f"{helper_type}_id"} - {"tag_id"})
    if reserved := sorted(addressing & fields.keys()):
        _reject(
            helper_type,
            f"config keys {', '.join(reserved)} address the request, not a "
            f"field of the {helper_type}.",
        )
    top_level = {"name": name, "icon": icon}
    for key, given in top_level.items():
        in_config = fields.pop(key, None)
        if in_config is None:
            continue
        if given is not None and given != in_config:
            _reject(
                helper_type,
                f"'{key}' was passed both as a parameter and in config with "
                "different values.",
            )
        top_level[key] = in_config
    return top_level["name"], clear_or_keep(top_level["icon"], "icon"), fields


def with_create_defaults(helper_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    """What Home Assistant's own dialog posts on create beyond the fields set."""
    fields = dict(fields)
    # A field set to null is left out, as the helper's dialog leaves out a
    # box that is emptied.
    fields = {key: value for key, value in fields.items() if value is not None}
    if helper_type == "tag":
        # Core's create reads tag_id unconditionally and generates the id of
        # an empty one; the tag dialog posts "" unless a custom id is typed.
        fields.setdefault("tag_id", "")
    return fields


def merged_update(
    helper_type: str,
    stored: dict[str, Any],
    name: str | None,
    icon: str | None,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """The stored item plus the changes: Core's update writes the whole item.

    A field set to null is removed from it, as the helper's dialog drops a
    box that is emptied. A cleared icon ('') is left out, since Core rejects
    an empty icon. A tag's
    name lives in the entity registry, and Core's tag update writes the name
    it is sent there: the listed one is not echoed, or a tag that was never
    named would get its default "Tag <id>" pinned as a registry override.
    """
    body = {k: v for k, v in stored.items() if k != "id"}
    if helper_type == "tag":
        body.pop("name", None)
    if name is not None:
        body["name"] = name
    if icon is not None:
        if icon:
            body["icon"] = icon
        else:
            body.pop("icon", None)
    for key, value in fields.items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    return body


def _reject(helper_type: str, message: str, **context: Any) -> None:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            message,
            context=_simple_helper_error_context(helper_type, **context),
        )
    )
