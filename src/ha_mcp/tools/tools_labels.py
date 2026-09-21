"""
Label management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant labels. To assign labels to entities, devices, or areas, use
ha_set_entity / ha_set_device / ha_set_area_or_floor, or pass areas= to
ha_config_set_label.
"""

import json
import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backup_manager import get_backup_manager
from ..config import get_global_settings
from ..errors import TOOL_ERROR_LOG_LEVEL, ErrorCode, create_error_response
from ..utils.registry_update_lock import registry_update_lock
from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .util_helpers import (
    JSON_STRING_COERCION,
    parse_string_list_param,
    websocket_error_message,
)

logger = logging.getLogger(__name__)

# Appended to any failure raised out of the per-area assignment loop: the label
# itself is already written at that point, so "retry the call" is the one thing
# the caller must not do.
_PARTIAL_RETRY_SUGGESTIONS = [
    (
        "The label write already succeeded; retry only the remaining area "
        + "IDs (do not recreate the label)."
    ),
    (
        "Use ha_set_area_or_floor(kind='area', labels=...) to replace an "
        + "area's label set, or ha_list_floors_areas() to inspect current "
        + "assignments."
    ),
]


def _string_labels(entry: dict[str, Any] | None) -> list[str]:
    """Return the string label IDs stored on a registry entry."""
    if not isinstance(entry, dict):
        return []
    return [lbl for lbl in (entry.get("labels") or []) if isinstance(lbl, str)]


class LabelTools:
    """Label management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _list_labels(
        self, context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Return all labels from the registry (shared by get/set).

        Raises ToolError (SERVICE_CALL_FAILED) if the list call fails or returns
        an unexpected non-list envelope — a degraded response must not collapse
        to an empty registry, or callers would confidently report a real label
        as missing (mirrors ``backup_manager._require_list``).
        """
        result = await self._client.send_websocket_message(
            {"type": "config/label_registry/list"}
        )
        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    result.get("error", "Failed to get labels"),
                    context=context,
                )
            )
        # No ``[]`` default: a success envelope that omits ``result`` (or sends
        # a non-list) is a degraded response, not an empty registry — defaulting
        # would let callers confidently report a real label as missing.
        labels = result.get("result")
        if not isinstance(labels, list):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Label registry returned a missing or non-list result",
                    context=context,
                )
            )
        return labels

    async def _require_existing_label(self, label_id: str, name: str) -> None:
        """Raise RESOURCE_NOT_FOUND if ``label_id`` is not in the registry.

        Enforces the strict update-only contract (issue #1860): update of an
        unknown id yields an opaque "Unknown error", and create cannot honor a
        caller-supplied id (HA derives it from the name), so an unknown id is a
        clear error with a create hint rather than a silently divergent upsert.
        """
        existing = await self._list_labels(context={"name": name, "label_id": label_id})
        if any(lbl.get("label_id") == label_id for lbl in existing):
            return
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Label not found: {label_id}",
                context={
                    "name": name,
                    "label_id": label_id,
                    "available_label_ids": [
                        lbl.get("label_id") for lbl in existing[:10]
                    ],
                },
                suggestions=[
                    "To create a new label, omit label_id — Home Assistant "
                    + "derives the id from the name (e.g. 'vendor:tapo' becomes "
                    + "'vendor_tapo')",
                    "To update an existing label, pass its exact current "
                    + "label_id (list all with ha_config_get_label())",
                ],
            )
        )

    async def _list_area_registry(
        self, *, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return the area registry, failing closed on a degraded envelope."""
        list_result = await self._client.send_websocket_message(
            {"type": "config/area_registry/list"}
        )
        areas = list_result.get("result") if list_result.get("success") else None
        if not isinstance(areas, list):
            # An auth rejection, a protocol error and a malformed payload all
            # land here; without HA's own message they are indistinguishable.
            details = (
                websocket_error_message(list_result.get("error"))
                if not list_result.get("success")
                else "unexpected result type: "
                + type(list_result.get("result")).__name__
            )
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Failed to retrieve area registry while assigning label",
                    details=details,
                    context=context,
                    suggestions=[
                        "Check Home Assistant connection",
                        "Use ha_list_floors_areas() to list areas",
                    ],
                )
            )
        return areas

    def _unique_area_ids(self, area_ids: list[str]) -> list[str]:
        """Reject empty IDs and return unique area IDs in caller order."""
        seen: set[str] = set()
        unique: list[str] = []
        for area_id in area_ids:
            if not area_id:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "areas must be a list of non-empty area IDs",
                        context={"areas": area_ids},
                        suggestions=[
                            "Use ha_list_floors_areas() to list valid area IDs.",
                            "Omit areas to leave area assignments unchanged.",
                        ],
                    )
                )
            if area_id not in seen:
                seen.add(area_id)
                unique.append(area_id)
        return unique

    async def _require_areas_exist(self, area_ids: list[str]) -> None:
        """Reject empty or unknown area IDs before creating/updating a label.

        Home Assistant stores a dangling area_id nowhere here — the later
        area-registry update would fail opaquely or no-op — so fail closed
        up front. Empty strings are not the documented clear sentinel (that
        is omitting ``areas``); they are invalid IDs. All IDs are checked
        against one registry snapshot (not N sequential list calls), and every
        unknown ID is reported at once so a caller fixing a multi-area call
        does not need one round trip per typo.

        The error is raised here rather than through the shared
        ``_raise_if_unknown_area`` helper because that one speaks for tools
        with a single ``area_id`` parameter and suggests ``area_id=""`` to
        clear it — advice this tool has no parameter for.
        """
        unique = self._unique_area_ids(area_ids)
        areas = await self._list_area_registry(context={"areas": unique})
        known: set[str] = {
            area["area_id"]
            for area in areas
            if isinstance(area, dict) and isinstance(area.get("area_id"), str)
        }
        unknown = [area_id for area_id in unique if area_id not in known]
        if unknown:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "areas contains area IDs that do not exist in the area "
                    f"registry: {', '.join(repr(a) for a in unknown)}",
                    context={"areas": unique, "unknown_area_ids": unknown},
                    suggestions=[
                        "Use ha_list_floors_areas() to list valid area IDs.",
                        "Omit areas to leave area assignments unchanged.",
                        f"Available area_ids: {sorted(known)}",
                    ],
                )
            )

    async def _snapshot_areas_before_assign(self, area_ids: list[str]) -> None:
        """Best-effort pre-write snapshot of each target area (label restore
        cannot undo area assignments).
        """
        try:
            mgr = get_backup_manager(self._client, get_global_settings())
            for area_id in area_ids:
                await mgr.maybe_snapshot(
                    "area_or_floor",
                    f"area:{area_id}",
                    tool_name="ha_config_set_label",
                )
        except Exception:
            logger.warning(
                "Could not snapshot areas before label assignment",
                extra={"areas": area_ids},
                exc_info=True,
            )

    def _raise_area_label_assign_failure(
        self,
        *,
        label_id: str,
        area_id: str,
        message: str,
        assigned: list[str],
    ) -> NoReturn:
        """Raise SERVICE_CALL_FAILED including partial assignment progress."""
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                message,
                context={
                    "label_id": label_id,
                    "area_id": area_id,
                    "partial": True,
                    "assigned_areas": assigned,
                },
                suggestions=list(_PARTIAL_RETRY_SUGGESTIONS),
            )
        )

    def _area_from_registry(
        self, areas: list[dict[str, Any]], area_id: str
    ) -> dict[str, Any] | None:
        for area in areas:
            if isinstance(area, dict) and area.get("area_id") == area_id:
                return area
        return None

    async def _confirm_area_has_label(
        self, label_id: str, area_id: str, update_result: dict[str, Any]
    ) -> bool:
        """True when the update envelope or a fresh list contains ``label_id``."""
        result = update_result.get("result")
        if label_id in _string_labels(result if isinstance(result, dict) else None):
            return True
        areas = await self._list_area_registry(
            context={"label_id": label_id, "area_id": area_id}
        )
        return label_id in _string_labels(self._area_from_registry(areas, area_id))

    def _reraise_assign_failure(
        self,
        err: Exception,
        *,
        label_id: str,
        area_id: str,
        assigned: list[str],
    ) -> NoReturn:
        """Re-raise the failure with partial progress, keeping its own code.

        The label write and every earlier area update are already committed, so
        the error has to carry ``partial`` / ``assigned_areas`` on the way out.
        What it must not lose on the way is *why* the loop stopped: a timeout,
        an auth rejection and a bug in this module need three different
        recoveries, so the original error code and suggestions are preserved
        and the partial-progress fields are merged in rather than replacing
        everything with SERVICE_CALL_FAILED.
        """
        progress: dict[str, Any] = {
            "label_id": label_id,
            "area_id": area_id,
            "partial": True,
            "assigned_areas": assigned,
        }
        if isinstance(err, ToolError):
            parsed = self._parsed_structured_error(err)
            if parsed is None:
                # Not our structured JSON — there is no code to preserve.
                self._raise_area_label_assign_failure(
                    label_id=label_id,
                    area_id=area_id,
                    message=str(err)
                    or f"Failed to assign label {label_id!r} to area {area_id!r}",
                    assigned=assigned,
                )
            if parsed.get("partial") is True:
                # Raised by _raise_area_label_assign_failure — already complete.
                raise err
            raise ToolError(
                json.dumps(
                    {
                        **parsed,
                        **progress,
                        "error": self._with_retry_suggestions(parsed),
                    },
                    indent=2,
                    default=str,
                ),
                log_level=TOOL_ERROR_LOG_LEVEL,
            ) from err

        # Transport and programmer errors: let the shared classifier pick the
        # code (CONNECTION_TIMEOUT, AUTHENTICATION_FAILED, …) and log the
        # traceback, then chain the cause instead of flattening it.
        response = exception_to_structured_error(
            err,
            progress,
            raise_error=False,
            suggestions=list(_PARTIAL_RETRY_SUGGESTIONS),
        )
        raise ToolError(
            json.dumps(response, indent=2, default=str),
            log_level=TOOL_ERROR_LOG_LEVEL,
        ) from err

    @staticmethod
    def _parsed_structured_error(err: ToolError) -> dict[str, Any] | None:
        """Return the tool's structured JSON payload, or None if it isn't one."""
        try:
            parsed = json.loads(str(err))
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            return parsed
        return None

    @staticmethod
    def _with_retry_suggestions(parsed: dict[str, Any]) -> dict[str, Any]:
        """Append the partial-retry hints to an error's own suggestions."""
        original: dict[str, Any] = parsed["error"]
        existing = original.get("suggestions") or (
            [original["suggestion"]] if original.get("suggestion") else []
        )
        combined = [*existing, *_PARTIAL_RETRY_SUGGESTIONS]
        return {**original, "suggestion": combined[0], "suggestions": combined}

    async def _add_label_to_one_area(
        self, label_id: str, area_id: str, assigned: list[str]
    ) -> None:
        """Fresh read-modify-write for one area. HA has no atomic label-add."""
        async with registry_update_lock("area", area_id):
            areas = await self._list_area_registry(
                context={"label_id": label_id, "area_id": area_id}
            )
            area = self._area_from_registry(areas, area_id)
            if area is None:
                self._raise_area_label_assign_failure(
                    label_id=label_id,
                    area_id=area_id,
                    message=f"area_id={area_id!r} does not exist in the area registry.",
                    assigned=assigned,
                )
            current = _string_labels(area)
            if label_id in current:
                return
            update = await self._client.send_websocket_message(
                {
                    "type": "config/area_registry/update",
                    "area_id": area_id,
                    "labels": [*current, label_id],
                }
            )
            if not update.get("success"):
                self._raise_area_label_assign_failure(
                    label_id=label_id,
                    area_id=area_id,
                    message=(
                        f"Failed to assign label {label_id!r} to area {area_id!r}: "
                        f"{update.get('error', 'Unknown error')}"
                    ),
                    assigned=assigned,
                )
            if not await self._confirm_area_has_label(label_id, area_id, update):
                self._raise_area_label_assign_failure(
                    label_id=label_id,
                    area_id=area_id,
                    message=(
                        f"Area {area_id!r} update succeeded but label "
                        f"{label_id!r} is not present on the area."
                    ),
                    assigned=assigned,
                )

    async def _add_label_to_areas(
        self, label_id: str, area_ids: list[str]
    ) -> list[str]:
        """Add ``label_id`` to each area's label set without removing others."""
        unique = list(dict.fromkeys(area_ids))
        await self._snapshot_areas_before_assign(unique)
        assigned: list[str] = []
        for area_id in unique:
            try:
                await self._add_label_to_one_area(label_id, area_id, assigned)
            except Exception as err:
                # Catch ordinary failures (transport, ToolError). Cancellation
                # is BaseException and must propagate.
                self._reraise_assign_failure(
                    err, label_id=label_id, area_id=area_id, assigned=assigned
                )
            assigned.append(area_id)
        return assigned

    def _parse_areas_param(self, areas: str | list[str] | None) -> list[str] | None:
        try:
            return parse_string_list_param(areas, "areas")
        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid areas parameter: {e}",
                )
            )
        return None  # py/mixed-returns: explicit terminal; raise_tool_error is NoReturn

    async def _apply_label_to_requested_areas(
        self,
        parsed_areas: list[str] | None,
        resolved_id: str | None,
    ) -> list[str] | None:
        """Return assigned area IDs, or None when the caller omitted ``areas``."""
        if parsed_areas is None:
            return None
        if not parsed_areas:
            return []
        if not resolved_id:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Label write succeeded without a resolved label_id; "
                    "area assignment was not applied.",
                    context={"areas": parsed_areas},
                    suggestions=[
                        "Retry ha_config_get_label() to confirm the label, "
                        "then assign with ha_set_area_or_floor(kind='area').",
                    ],
                )
            )
        return await self._add_label_to_areas(resolved_id, parsed_areas)

    def _raise_label_set_failure(
        self, result: dict[str, Any], action: str, name: str, label_id: str | None
    ) -> NoReturn:
        """Raise for a failed label_registry create/update.

        The unknown-id case is caught up front by ``_require_existing_label``.
        This substring match only catches HA error texts containing
        ``"not found"``/``"doesn't exist"``; HA's label update does NOT emit
        those for an unknown/deleted id (it surfaces ``"Unknown error"``), so
        it is a best-effort guard for other/future phrasings only.
        """
        error_str = str(result.get("error", "")).lower()
        if "not found" in error_str or "doesn't exist" in error_str:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Label not found: {label_id}",
                    context={"name": name, "label_id": label_id},
                    suggestions=[
                        "Use ha_config_get_label() without label_id to see all labels",
                    ],
                )
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to {action} label: {result.get('error', 'Unknown error')}",
                context={"name": name, "label_id": label_id},
            )
        )

    async def _prepare_label_write(
        self,
        name: str,
        label_id: str | None,
        areas: str | list[str] | None,
    ) -> list[str] | None:
        """Validate create/update discriminator and optional area targets."""
        # ``None`` stays the documented "create-new" sentinel; explicit
        # empty/whitespace is rejected so the create/update discriminator
        # cannot silently route an intended update to create.
        if label_id is not None:
            validate_identifier_not_empty(
                label_id,
                "label_id",
                suggestions=[
                    "Omit label_id entirely to create a new label",
                    "Pass a valid label_id to update an existing label",
                ],
                context={"action": "set", "name": name},
            )
            # Strict update-only contract (issue #1860): routing to
            # label_registry/update with an unknown id returns an opaque
            # "Unknown error", and label_registry/create cannot honor a
            # caller-supplied id (HA derives it from the name). Verify the
            # id exists up front and return actionable guidance instead of
            # dispatching an update that fails cryptically.
            await self._require_existing_label(label_id, name)
        parsed_areas = self._parse_areas_param(areas)
        if parsed_areas:
            await self._require_areas_exist(parsed_areas)
        return parsed_areas

    def _build_label_set_message(
        self,
        name: str,
        label_id: str | None,
        color: str | None,
        icon: str | None,
        description: str | None,
    ) -> tuple[str, dict[str, Any]]:
        action = "update" if label_id else "create"
        message: dict[str, Any] = {
            "type": f"config/label_registry/{action}",
            "name": name,
        }
        if action == "update":
            message["label_id"] = label_id
        if color is not None:
            message["color"] = color
        if icon is not None:
            message["icon"] = icon
        if description is not None:
            message["description"] = description
        return action, message

    async def _label_set_success_response(
        self,
        result: dict[str, Any],
        action: str,
        name: str,
        label_id: str | None,
        parsed_areas: list[str] | None,
    ) -> dict[str, Any]:
        label_data = result.get("result", {})
        action_past = "created" if action == "create" else "updated"
        resolved_id = label_data.get("label_id") or label_id
        assigned_areas = await self._apply_label_to_requested_areas(
            parsed_areas, resolved_id
        )
        payload: dict[str, Any] = {
            "success": True,
            "label_id": resolved_id,
            "label_data": label_data,
            "message": f"Successfully {action_past} label: {name}",
        }
        if assigned_areas is not None:
            payload["assigned_areas"] = assigned_areas
        return payload

    @tool(
        name="ha_config_get_label",
        tags={"Labels & Categories"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Label",
        },
    )
    @log_tool_usage
    async def ha_config_get_label(
        self,
        label_id: Annotated[
            str | None,
            Field(
                description="ID of the label to retrieve. If omitted, lists all labels.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get label info - list all labels or get a specific one by ID.

        Without a label_id: Lists all Home Assistant labels with their configurations.
        With a label_id: Returns configuration for that specific label.

        LABEL PROPERTIES:
        - ID (label_id), Name
        - Color (optional), Icon (optional), Description (optional)

        EXAMPLES:
        - List all labels: ha_config_get_label()
        - Get specific label: ha_config_get_label("my_label_id")

        Use ha_config_set_label() to create or update labels.
        Use ha_set_entity(labels=["label1", "label2"]) to assign labels to entities,
        ha_set_device(labels=[...]) for devices, or
        ha_set_area_or_floor(kind="area", labels=[...]) for areas.
        """
        try:
            # ``None`` stays the documented "list-all" sentinel; explicit
            # empty/whitespace is rejected by ``validate_identifier_not_empty``.
            if label_id is not None:
                validate_identifier_not_empty(
                    label_id,
                    "label_id",
                    suggestions=[
                        "Omit label_id entirely to list all labels",
                        "Pass a valid non-empty label_id",
                    ],
                    context={"action": "get"},
                )
            labels = await self._list_labels(context={"label_id": label_id})

            if label_id is None:
                return {
                    "success": True,
                    "count": len(labels),
                    "labels": labels,
                    "message": f"Found {len(labels)} label(s)",
                }

            label = next(
                (lbl for lbl in labels if lbl.get("label_id") == label_id), None
            )

            if label:
                return {
                    "success": True,
                    "label_id": label_id,
                    "label": label,
                    "message": f"Found label: {label.get('name', label_id)}",
                }
            else:
                available_ids = [lbl.get("label_id") for lbl in labels[:10]]
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Label not found: {label_id}",
                        context={
                            "label_id": label_id,
                            "available_label_ids": available_ids,
                        },
                        suggestions=[
                            "Use ha_config_get_label() without label_id to see all labels"
                        ],
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting labels: {e}")
            exception_to_structured_error(
                e,
                context={"label_id": label_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    @tool(
        name="ha_config_set_label",
        tags={"Labels & Categories"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Create or Update Label",
        },
    )
    @with_auto_backup(domain="label", id_param="label_id")
    @log_tool_usage
    async def ha_config_set_label(
        self,
        name: Annotated[str, Field(description="Display name for the label")],
        label_id: Annotated[
            str | None,
            Field(
                description="Label ID for updates. If not provided, creates a new label.",
                default=None,
            ),
        ] = None,
        color: Annotated[
            str | None,
            Field(
                description="Color for the label (e.g., 'red', 'blue', 'green', or hex like '#FF5733')",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:tag', 'mdi:label')",
                default=None,
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description="Description of the label's purpose",
                default=None,
            ),
        ] = None,
        areas: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Area IDs to apply this label to (adds the label without "
                    "removing existing ones). Omit to leave area assignments "
                    "unchanged; an empty list is a no-op (assigns nothing and "
                    "removes nothing). To clear an area's labels use "
                    "ha_set_area_or_floor(kind='area', labels=[])."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant label.

        Creates a new label if label_id is not provided, or updates an existing label if label_id is provided.

        Labels are a flexible tagging system that can be applied to entities,
        devices, and areas for organization and automation purposes.

        EXAMPLES:
        - Create simple label: ha_config_set_label("Critical")
        - Create colored label: ha_config_set_label("Outdoor", color="green")
        - Create label with icon: ha_config_set_label("Battery Powered", icon="mdi:battery")
        - Create full label: ha_config_set_label("Security", color="red", icon="mdi:shield", description="Security-related devices")
        - Update label: ha_config_set_label("Updated Name", label_id="my_label_id", color="blue")
        - Create and apply to areas: ha_config_set_label("Site Home", areas=["kitchen", "living_room"])

        After creating a label, use ha_set_entity(labels=["label_id"]) to assign it to entities,
        ha_set_device(labels=["label_id"]) for devices, or
        ha_set_area_or_floor(kind="area", labels=["label_id"]) for areas (replaces the area's set).
        Pass areas=["kitchen"] here to add the label onto those areas without replacing others.
        """
        try:
            parsed_areas = await self._prepare_label_write(name, label_id, areas)
            action, message = self._build_label_set_message(
                name, label_id, color, icon, description
            )
            result = await self._client.send_websocket_message(message)
            if result.get("success"):
                return await self._label_set_success_response(
                    result, action, name, label_id, parsed_areas
                )
            self._raise_label_set_failure(result, action, name, label_id)

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error setting label {name!r}: {e}")
            exception_to_structured_error(
                e,
                context={"name": name, "label_id": label_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify the label name is valid",
                    "For updates, verify the label_id exists using ha_config_get_label()",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    @tool(
        name="ha_config_remove_label",
        tags={"Labels & Categories"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Label",
        },
    )
    @with_auto_backup(domain="label", id_param="label_id")
    @log_tool_usage
    async def ha_config_remove_label(
        self,
        label_id: Annotated[
            str,
            Field(description="ID of the label to delete"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant label.

        Removes the label from the label registry. This will also remove the label
        from all entities, devices, and areas that have it assigned.

        EXAMPLES:
        - Delete label: ha_config_remove_label("my_label_id")

        Use ha_config_get_label() to find label IDs.

        **WARNING:** Deleting a label will remove it from all assigned entities.
        This action cannot be undone.
        """
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                label_id,
                "label_id",
                suggestions=[
                    "Pass a valid label_id (use ha_config_get_label() to list)",
                ],
                context={"action": "remove"},
            )
            message: dict[str, Any] = {
                "type": "config/label_registry/delete",
                "label_id": label_id,
            }

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    "label_id": label_id,
                    "message": f"Successfully deleted label: {label_id}",
                }
            else:
                error_str = str(result.get("error", "")).lower()
                if "not found" in error_str or "doesn't exist" in error_str:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.RESOURCE_NOT_FOUND,
                            f"Label not found: {label_id}",
                            context={"label_id": label_id},
                            suggestions=[
                                "Use ha_config_get_label() without label_id to see all labels",
                            ],
                        )
                    )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to delete label: {result.get('error', 'Unknown error')}",
                        context={"label_id": label_id},
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error removing label {label_id!r}: {e}")
            exception_to_structured_error(
                e,
                context={"label_id": label_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify the label_id exists using ha_config_get_label()",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


def register_label_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant label management tools."""
    register_tool_methods(mcp, LabelTools(client))
