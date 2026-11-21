"""Helper type constants and the helper response shape."""

from contextvars import ContextVar
from typing import Any, TypedDict

from ...storage_helper_types import SIMPLE_HELPER_TYPES, StorageHelperType
from ..config_write_helpers import attach_skill_content

__all__ = [
    "SIMPLE_HELPER_TYPES",
    "StorageHelperType",
    "_HELPER_SKILL_FILES",
    "SIMPLE_CONFIG_KEYS_DESCRIPTION",
    "HelperResponse",
    "_attach_helper_skill",
    "_helper_response",
    "_simple_helper_error_context",
]

# helper-selection.md guides which helper type fits the agent's use case
# (input_*, counter, timer, template, group, utility_meter, etc.).
_HELPER_SKILL_FILES: tuple[str, ...] = ("references/helper-selection.md",)


def _attach_helper_skill(response: dict[str, Any], MandatoryBPS: bool) -> None:
    """In-place attach skill_content to a helper response when applicable.

    Helper tool has no best-practice checker integration, so
    ``referenced_files`` is always None — embedding is driven purely by
    the ``MandatoryBPS`` flag. Delegates to the shared
    :func:`attach_skill_content` so the missing-vendor-warning path is
    consistent across every write tool.
    """
    attach_skill_content(
        response,
        MandatoryBPS=MandatoryBPS,
        canonical_files=_HELPER_SKILL_FILES,
        referenced_files=None,
    )


def _simple_helper_error_context(
    helper_type: str,
    **extra: Any,
) -> dict[str, Any]:
    """Build a validation-error `context` dict carrying the helper's schema.

    Returns `helper_type`, Core's field list as `data_schema` when the
    component served it for this call, and any caller-supplied extra fields.
    Without the component Core's own error text names the field.
    """
    context: dict[str, Any] = {"helper_type": helper_type}
    schema = _core_helper_fields(helper_type)
    if schema is not None:
        context["data_schema"] = schema
    context.update(extra)
    return context


class HelperResponse(TypedDict, total=False):
    """Uniform response contract for ``ha_config_set_helper`` (issue #1293).

    Documents the legal key set across all three branches (create, update,
    flow). ``total=False`` because per-branch fields (entity_id, flow extras,
    warnings) are conditional. Consumed by ``_helper_response`` below — all
    return literals in this module funnel through that builder so the shape
    has a single point of construction.
    """

    success: bool
    action: str  # "create" | "update"
    helper_type: str
    data: dict[str, Any]
    entity_id: str  # absent on flow branch (use entity_ids[] for multi-entity)
    message: str | None
    warnings: list[str]  # omitted when empty
    # Flow-helper convenience accessors (only set on the flow branch).
    method: str
    entry_id: str | None
    title: str | None
    updated: bool
    entity_ids: list[str]
    area_id: str | None
    labels: list[str]
    category: str
    applied: list[dict[str, Any]]


def _helper_response(
    action: str,
    helper_type: str,
    *,
    data: dict[str, Any],
    entity_id: str | None = None,
    message: str | None = None,
    warnings: list[str] | None = None,
    **extras: Any,
) -> dict[str, Any]:
    """Single construction point for the ``ha_config_set_helper`` response.

    Enforces the uniform shape from issue #1293: ``success`` → ``action`` →
    ``helper_type`` → ``data`` → ``entity_id`` (when present) → ``message`` →
    flow-helper extras → ``warnings`` (only when non-empty). Returning
    ``dict[str, Any]`` rather than ``HelperResponse`` keeps the call sites
    free of mypy gymnastics around the dynamic ``**extras`` keys; the
    TypedDict serves as the readable contract anchor instead.
    """
    resp: dict[str, Any] = {
        "success": True,
        "action": action,
        "helper_type": helper_type,
        "data": data,
    }
    if entity_id is not None:
        resp["entity_id"] = entity_id
    resp["message"] = message
    resp.update(extras)
    if warnings:
        resp["warnings"] = warnings
    return resp


# What the `config` description says about SIMPLE helpers' keys until Core's
# own field list replaces it (``ComponentHelperSchemaTransform``, when the
# component serves it). No field is named here: Core's schema is the list.
SIMPLE_CONFIG_KEYS_DESCRIPTION = (
    "the fields of the type's own Home Assistant schema, under its names; "
    "Home Assistant validates them and its error names any mistake. With the "
    "ha_mcp_tools component installed, ha_config_list_helpers(helper_type, "
    "describe=True) lists a type's fields."
)


# Core's simple-helper field lists and the call's action, fetched from the
# component once per ha_config_set_helper call; None when it does not serve
# them.
_CORE_HELPER_SCHEMAS: ContextVar[tuple[dict[str, Any], str] | None] = ContextVar(
    "_CORE_HELPER_SCHEMAS", default=None
)


def _core_helper_fields(helper_type: str) -> list[dict[str, Any]] | None:
    current = _CORE_HELPER_SCHEMAS.get()
    if current is None:
        return None
    schemas, action = current
    fields = (schemas.get(helper_type) or {}).get(action)
    return fields or None
