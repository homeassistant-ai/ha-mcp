"""WebSocket message builders and parameter checks for the area/floor setter.

Kept apart from ``tools_areas`` so the tool module stays under the size ratchet;
these are pure functions over the parameters ``ha_set_area_or_floor`` accepts.
"""

from typing import Any

from pydantic.fields import FieldInfo

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error


class _Unset:
    """Default for the clearable params: tells "omitted" apart from explicit null."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


def resolve_clearable(value: Any) -> str | None:
    """Map a clearable param onto the builders' convention (None=keep, ""=clear).

    Omitted arrives as ``UNSET`` through FastMCP (``Field(default_factory=...)``)
    or as the ``FieldInfo`` itself on a direct Python call; an explicit JSON
    ``null`` arrives as ``None`` and means clear, same as ``""``.
    """
    if value is UNSET or isinstance(value, FieldInfo):
        return None
    if value is None:
        return ""
    return value


_AREA_PARAMS = (
    "name, id, floor_id, icon, aliases, picture, labels, "
    "temperature_entity_id, humidity_entity_id"
)


def validate_cross_kind_params(
    kind: str,
    level: int | None,
    floor_id: str | None,
    picture: str | None,
    labels: list[str] | None = None,
    temperature_entity_id: str | None = None,
    humidity_entity_id: str | None = None,
) -> None:
    """Reject params that don't belong to *kind* before building a set message."""
    # Reject cross-kind params loudly so silent intent loss can't happen
    # (e.g., kind='floor' with picture='...' previously dropped the picture
    # without a diagnostic). Floors have no labels in HA core.
    cross_kind_params: list[str] = []
    if kind == "area" and level is not None:
        cross_kind_params.append("level")
    elif kind == "floor":
        if floor_id is not None:
            cross_kind_params.append("floor_id")
        if picture is not None:
            cross_kind_params.append("picture")
        if labels is not None:
            cross_kind_params.append("labels")
        if temperature_entity_id is not None:
            cross_kind_params.append("temperature_entity_id")
        if humidity_entity_id is not None:
            cross_kind_params.append("humidity_entity_id")
    if cross_kind_params:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Parameter(s) {cross_kind_params} are not valid for kind={kind!r}",
                context={"kind": kind, "invalid_parameters": cross_kind_params},
                suggestions=[
                    f"For kind='area' use: {_AREA_PARAMS}",
                    "For kind='floor' use: name, id, level, icon, aliases",
                ],
            )
        )


def build_area_update_message(
    area_id: str,
    name: str | None,
    floor_id: str | None,
    icon: str | None,
    parsed_aliases: list[str] | None,
    picture: str | None,
    parsed_labels: list[str] | None,
    temperature_entity_id: str | None = None,
    humidity_entity_id: str | None = None,
) -> dict[str, Any]:
    """Build a WebSocket message for updating an existing area."""
    message: dict[str, Any] = {
        "type": "config/area_registry/update",
        "area_id": area_id,
    }
    if name is not None:
        message["name"] = name
    if floor_id is not None:
        message["floor_id"] = floor_id if floor_id else None
    if icon is not None:
        message["icon"] = icon if icon else None
    if parsed_aliases is not None:
        message["aliases"] = parsed_aliases
    if picture is not None:
        message["picture"] = picture if picture else None
    if parsed_labels is not None:
        message["labels"] = parsed_labels
    if temperature_entity_id is not None:
        message["temperature_entity_id"] = temperature_entity_id or None
    if humidity_entity_id is not None:
        message["humidity_entity_id"] = humidity_entity_id or None
    return message


def build_area_create_message(
    name: str,
    floor_id: str | None,
    icon: str | None,
    parsed_aliases: list[str] | None,
    picture: str | None,
    parsed_labels: list[str] | None,
    temperature_entity_id: str | None = None,
    humidity_entity_id: str | None = None,
) -> dict[str, Any]:
    """Build a WebSocket message for creating a new area."""
    message: dict[str, Any] = {
        "type": "config/area_registry/create",
        "name": name,
    }
    if floor_id:
        message["floor_id"] = floor_id
    if icon:
        message["icon"] = icon
    if parsed_aliases:
        message["aliases"] = parsed_aliases
    if picture:
        message["picture"] = picture
    if parsed_labels:
        message["labels"] = parsed_labels
    if temperature_entity_id:
        message["temperature_entity_id"] = temperature_entity_id
    if humidity_entity_id:
        message["humidity_entity_id"] = humidity_entity_id
    return message


def build_floor_update_message(
    floor_id: str,
    name: str | None,
    level: int | None,
    icon: str | None,
    parsed_aliases: list[str] | None,
) -> dict[str, Any]:
    """Build a WebSocket message for updating an existing floor."""
    message: dict[str, Any] = {
        "type": "config/floor_registry/update",
        "floor_id": floor_id,
    }
    if name is not None:
        message["name"] = name
    if level is not None:
        message["level"] = level
    if icon is not None:
        message["icon"] = icon if icon else None
    if parsed_aliases is not None:
        message["aliases"] = parsed_aliases
    return message


def build_floor_create_message(
    name: str,
    level: int | None,
    icon: str | None,
    parsed_aliases: list[str] | None,
) -> dict[str, Any]:
    """Build a WebSocket message for creating a new floor."""
    message: dict[str, Any] = {
        "type": "config/floor_registry/create",
        "name": name,
    }
    if level is not None:
        message["level"] = level
    if icon:
        message["icon"] = icon
    if parsed_aliases:
        message["aliases"] = parsed_aliases
    return message


# ============================================================
# AREA & FLOOR LISTING
# ============================================================
