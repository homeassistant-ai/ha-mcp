"""Record shaping and pagination for ha_config_list_helpers."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from ...errors import ErrorCode, create_error_response
from ..helpers import raise_tool_error
from ..response_helpers import build_pagination_metadata
from .schemas import SIMPLE_HELPER_TYPES

logger = logging.getLogger(__name__)

# The component reports ``secret_scrub_degraded`` when secrets.yaml exists but
# cannot be read (its flow-helper options then went out unscrubbed).
_SCRUB_DEGRADED_WARNING = (
    "secrets.yaml could not be read, so flow-helper options in this listing were "
    "not scrubbed of resolved !secret values."
)


def _component_warnings(result: dict[str, Any]) -> list[str]:
    """The warnings for a component result whose flow-helper read degraded."""
    if result.get("secret_scrub_degraded") is True:
        return [_SCRUB_DEGRADED_WARNING]
    return []


def raise_if_helper_flows_degraded(
    result: dict[str, Any], helper_types: list[str]
) -> None:
    """Name the cause when the component left out helper flows Core lists.

    With ``helper_flows_degraded`` the component's loader read failed and it
    listed only Core's built-in helper flows, so custom ones are missing from
    an installed, current component; "update the component" would mislead.
    """
    if result.get("helper_flows_degraded") is not True:
        return
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Home Assistant's loader could not list its helper flows for the "
            f"ha_mcp_tools component, so it cannot list {', '.join(helper_types)}.",
            context={"helper_types": helper_types},
            suggestions=[
                "Check the Home Assistant log for the loader error, then retry",
            ],
        )
    )


def listed_items(listed: Any) -> list[Any]:
    """The editable items of a ``<type>/list`` result.

    person/list nests them under "storage" (its "config" persons come from
    YAML); every other storage collection returns the list itself.
    """
    if isinstance(listed, dict):
        listed = listed.get("storage")
    return listed if isinstance(listed, list) else []


def _shape_collection_helper_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Map one component collection-helper record onto the legacy list record.

    The legacy ``{helper_type}/list`` record is the storage body itself
    (``id`` = storage id, ``name`` = creation-time name, plus type-specific
    keys). The component supplies that same body as ``config`` and, from the
    real storage collection + entity registry, the authoritative
    ``storage_id`` plus the current ``entity_id`` and display ``name``. Keep the
    legacy keys with their legacy meanings and layer the current ``entity_id`` +
    ``name`` on top — the additive form of the #1794 stale-id fix that the
    legacy-path PR converges to.
    """
    config = rec.get("config")
    out: dict[str, Any] = dict(config) if isinstance(config, dict) else {}
    # Prefer the record-level ``storage_id`` (the component reads it from the
    # real storage collection). Not every collection body carries its own
    # ``id`` — person/zone are stored keyed by id rather than embedding it — so
    # trusting the body's ``id`` drifts for those types. Fall back to
    # ``object_id`` only when ``storage_id`` is absent (older component).
    storage_id = rec.get("storage_id")
    if storage_id is None:
        storage_id = rec.get("object_id")
    if storage_id is not None:
        out["id"] = storage_id
    entity_id = rec.get("entity_id")
    if entity_id is not None:
        out["entity_id"] = entity_id
    name = rec.get("name")
    if name is not None:
        # The storage body's ``name`` is the creation-time name (a rename
        # updates the registry, not the body — #1794); preserve it as
        # ``original_name`` before the current display name overrides it, so a
        # component-served record carries the same additive shape as the legacy
        # join (both paths promise entity_id/original_name in the docstring).
        original_name = out.get("name")
        if original_name is not None:
            out["original_name"] = original_name
        out["name"] = name
    return out


def _shape_flow_helper_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Map one component flow-helper record onto a list record.

    Flow helpers have no storage ``id`` and no ``{type}/list`` command; the
    component sources them from the config entry. The record carries the
    ``entry_id`` (config-entry id), the current ``entity_id`` + display
    ``name``, the ``helper_type``, and the data-minimized ``options`` body
    (``ConfigEntry.options`` only, never ``entry.data``). Mirrors the
    collection shaper's current-fields layering.
    """
    out: dict[str, Any] = {"helper_type": rec.get("helper_type")}
    entry_id = rec.get("entry_id")
    if entry_id is not None:
        out["entry_id"] = entry_id
    entity_id = rec.get("entity_id")
    if entity_id is not None:
        out["entity_id"] = entity_id
    name = rec.get("name")
    if name is not None:
        out["name"] = name
    options = rec.get("options")
    if isinstance(options, dict):
        out["options"] = options
    withheld = rec.get("options_withheld")
    if withheld is not None:
        out["options_withheld"] = withheld
    return out


def _paginate_helpers_response(
    response: dict[str, Any], offset: int, limit: int
) -> dict[str, Any]:
    """Slice a helper listing envelope down to one page.

    The single normalization point for every ``ha_config_list_helpers`` route
    (all-types, component, legacy): each builds the same
    ``success``/``helper_type``/``count``/``helpers``/``message`` envelope, so
    the slice is applied once here instead of in each builder. ``count`` becomes
    the page size and ``total_count`` carries the full size, matching
    ``ha_list_services``.
    """
    helpers = response.get("helpers")
    if not isinstance(helpers, list):
        # Every builder owes this function a flat list; anything else is a bug in
        # the caller (a {type}/list shape that escaped _flatten_helper_list_result).
        # The records are still usable, so return them unpaginated rather than
        # raising -- but say so in warnings[], not just the log: the caller is an
        # agent that never sees server logs, and absent metadata otherwise reads
        # as "the collection fits on one page".
        shape = type(helpers).__name__
        logger.warning(
            "Cannot paginate %r listing: expected a list of helpers, got %s; "
            "returning the envelope unpaginated",
            response.get("helper_type"),
            shape,
        )
        warning = (
            f"Listing could not be paginated (expected a list of helpers, got "
            f"{shape}); returned in full, without pagination metadata."
        )
        existing = response.get("warnings")
        return {
            **response,
            "warnings": [*existing, warning]
            if isinstance(existing, list)
            else [warning],
        }
    total_count = len(helpers)
    page = helpers[offset : offset + limit]
    return {
        **response,
        "helpers": page,
        **build_pagination_metadata(total_count, offset, limit, len(page)),
    }


def _shape_component_helpers_response(
    helper_type: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Map an ``ha_mcp_tools/helpers_list`` result into the legacy envelope.

    Emits the exact legacy top-level keys (``success``/``helper_type``/
    ``count``/``helpers``/``message``). Records are shaped to the requested
    universe: a flow ``helper_type`` yields flow records (``entry_id`` +
    current ``entity_id``/``name`` + ``options``, or ``options_withheld`` for a
    custom-only domain); a storage type yields the
    storage-body records. A record of the other kind is dropped defensively.
    ``count`` is the length of the emitted list, mirroring the legacy
    ``count == len(helpers)`` guarantee.
    """
    raw = result.get("helpers")
    records = raw if isinstance(raw, list) else []
    want_flow = helper_type not in SIMPLE_HELPER_TYPES
    helpers: list[dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if (rec.get("kind") == "flow") != want_flow:
            continue
        helpers.append(
            _shape_flow_helper_record(rec)
            if want_flow
            else _shape_collection_helper_record(rec)
        )
    response: dict[str, Any] = {
        "success": True,
        "helper_type": helper_type,
        "count": len(helpers),
        "helpers": helpers,
        "message": f"Found {len(helpers)} {helper_type} helper(s)",
    }
    if want_flow and (warnings := _component_warnings(result)):
        response["warnings"] = warnings
    return response


def _component_covers(result: dict[str, Any], helper_type: str) -> bool:
    """Whether a helpers_list response authoritatively enumerated ``helper_type``.

    The component reports ``covered_types``: the helper types its response could
    actually see. A type outside that list (e.g. ``tag`` — tags have no state
    entity, so the component's from-states scan can't enumerate them) must NOT
    be trusted as "none exist". A response with no ``covered_types`` at all (an
    older component) is treated conservatively as covering nothing, so the
    caller falls back rather than trusting a possibly-partial list.
    """
    covered = result.get("covered_types")
    return isinstance(covered, list) and helper_type in covered


def _raise_flow_requires_component(helper_type: str) -> NoReturn:
    """Raise the structured error for a flow helper with no component path.

    Flow-based helper types have no ``{type}/list`` WS command, so the legacy
    listing path cannot enumerate them — only the ha_mcp_tools component's
    ``helpers_list`` can. When the component is absent, downlevel, or its call
    fails, this is a hard error: never a silent empty list, never a legacy
    fallback (there is none for flow types).
    """
    raise_tool_error(
        create_error_response(
            ErrorCode.COMPONENT_NOT_INSTALLED,
            f"Listing '{helper_type}' (a flow-based helper) requires the "
            "ha_mcp_tools custom component (>= 1.1.0); the built-in listing "
            "path cannot enumerate flow helpers.",
            context={"helper_type": helper_type},
        )
    )


def _raise_all_requires_component() -> NoReturn:
    """Raise the structured error for all-types listing with no component path.

    ``helper_type="all"`` has no legacy equivalent — there is no single WS
    command that enumerates every helper type — so, like a flow-based type, it
    is served exclusively through the ha_mcp_tools component. When the component
    is absent, downlevel, or its call fails, this is a hard error: never a
    silent empty list, never a partial legacy fallback.
    """
    raise_tool_error(
        create_error_response(
            ErrorCode.COMPONENT_NOT_INSTALLED,
            "Listing all helper types in one call (helper_type='all') requires "
            "the ha_mcp_tools custom component (>= 1.1.0); without it, list a "
            "specific helper_type instead.",
            context={"helper_type": "all"},
        )
    )


async def shape_all_helpers_response(
    result: dict[str, Any],
    legacy_list: Callable[[str], Awaitable[dict[str, Any]]],
    *,
    flow_types: frozenset[str],
) -> dict[str, Any]:
    """Map an all-types ``helpers_list`` result into the merged listing envelope.

    Each record is shaped by kind (flow → ``_shape_flow_helper_record``,
    collection → ``_shape_collection_helper_record``) and stamped with its
    own ``helper_type`` so records of different types stay distinguishable in
    the flat list. Respecting ``covered_types`` (mirroring the single-type
    path): a simple type the component could not enumerate from the state
    machine — ``tag`` has no state entity — is fetched per-type via its
    legacy ``{type}/list`` (``legacy_list``) and merged, so ``all`` never silently drops it.
    """
    raw = result.get("helpers")
    records = raw if isinstance(raw, list) else []
    helpers: list[dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("kind") == "flow":
            helpers.append(_shape_flow_helper_record(rec))
        else:
            shaped = _shape_collection_helper_record(rec)
            # All-types records span many types, so each self-describes its
            # type (single-type mode carries it at the envelope top instead).
            shaped["helper_type"] = rec.get("helper_type")
            helpers.append(shaped)

    covered = result.get("covered_types")
    covered_set = set(covered) if isinstance(covered, list) else set()
    # Flow helper types have no legacy ``{type}/list`` fallback — if the
    # component did not authoritatively cover one, a "successful" merged
    # listing would silently omit it. Mirror the single-type taxonomy:
    # hard error, never a partial inventory reported as complete.
    uncovered_flow = sorted(flow_types - covered_set)
    if uncovered_flow:
        raise_if_helper_flows_degraded(result, uncovered_flow)
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "The ha_mcp_tools component response did not cover flow "
                f"helper type(s): {', '.join(uncovered_flow)} — cannot "
                "return a complete all-types listing.",
                context={"helper_type": "all", "uncovered": uncovered_flow},
                suggestions=[
                    "Update the ha_mcp_tools custom component",
                    "List helper types individually instead of 'all'",
                ],
            )
        )
    merge_warnings = _component_warnings(result)
    for helper_type in sorted(SIMPLE_HELPER_TYPES - covered_set):
        legacy = await legacy_list(helper_type)
        # legacy_list joins the registry (issue #1945) and, degrade-
        # open, flags a failed registry read in warnings[]; surface those here
        # instead of dropping them, else an uncovered type is served stale and
        # silent during an all-types listing.
        merge_warnings.extend(legacy.get("warnings", []))
        skipped = 0
        for item in legacy.get("helpers", []):
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("helper_type", helper_type)
                helpers.append(row)
            else:
                skipped += 1
        if skipped:
            # This is how the unflattened person/list dict used to vanish:
            # iterating it yielded its keys, and each failed the check here.
            logger.warning(
                "Dropped %d unrecognised item(s) from the %s listing while "
                "merging all types; the merged listing is incomplete",
                skipped,
                helper_type,
            )

    response: dict[str, Any] = {
        "success": True,
        "helper_type": "all",
        "count": len(helpers),
        "helpers": helpers,
        "message": f"Found {len(helpers)} helper(s)",
    }
    if merge_warnings:
        response["warnings"] = merge_warnings
    return response
