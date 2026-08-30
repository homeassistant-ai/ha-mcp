"""Resolve deterministic structural selectors into exact bulk-control leaves."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, NamedTuple, NotRequired, TypedDict

from pydantic import ConfigDict, Field

from ..client.rest_client import HomeAssistantClient
from ..utils.domain_handlers import get_domain_handler
from ..utils.entity_membership import normalize_member_entity_ids
from ..visibility.resolver import VisibilityDataUnavailable, load_hidden_set

logger = logging.getLogger(__name__)

MAX_SELECTOR_ENTITIES = 100

# HA's native scene platform sets its `entity_id` attribute to the entities
# the scene CONFIGURES (homeassistant/components/homeassistant/scene.py:
# `{ATTR_ENTITY_ID: list(self.scene_config.states)}`), not entities it is
# structurally composed of -- those routinely live in other areas or none at
# all. `normalize_member_entity_ids` cannot tell that shape apart from a real
# aggregate's member list (both are just an `entity_id` collection), so a
# scene assigned to a selected area must never be admitted as an expansion
# root; see `resolve_bulk_selector`'s `matching_roots`. This is a domain
# whose entity_id-attribute has a fundamentally different HA-core meaning,
# not a manufacturer/integration heuristic -- it does not reintroduce the
# per-integration checks (is_hue_group, hue_type, ...) this feature
# deliberately avoids.
_NON_AGGREGATE_ROOT_DOMAINS = frozenset({"scene"})


class BulkControlSelector(TypedDict):
    """Exact structural scope for one deterministic bulk action.

    Lives here (not in ``tools_service.py``) so ``_SELECTOR_KEYS`` below can
    derive from this single field set instead of duplicating it as an
    independent literal -- the import direction (``tools_service`` already
    imports from this module) makes that safe without a cycle.
    """

    domain: Annotated[
        str,
        Field(description="Exact Home Assistant domain, e.g. 'light'."),
    ]
    area_ids: NotRequired[
        Annotated[
            list[str],
            Field(description="Exact Home Assistant area IDs to include."),
        ]
    ]
    floor_ids: NotRequired[
        Annotated[
            list[str],
            Field(description="Exact Home Assistant floor IDs to include."),
        ]
    ]
    exclude_entity_ids: NotRequired[
        Annotated[
            list[str],
            Field(
                description=(
                    "Exact entity or aggregate IDs to exclude after recursive "
                    "membership expansion."
                )
            ),
        ]
    ]


BulkControlSelector.__pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[attr-defined]
_SELECTOR_KEYS = frozenset(BulkControlSelector.__annotations__)


class _ExpansionEntry(NamedTuple):
    """One memoized membership-expansion result. Named to keep the two
    ``frozenset[str]`` slots (leaves vs. groups) from being interchangeable
    at the write sites -- a positional bare tuple lets a swapped write
    type-check silently."""

    leaves: frozenset[str]
    groups: frozenset[str]


class _ValidatedSelector(NamedTuple):
    """Normalized selector fields, so ``_validate_selector`` doesn't return a
    bare 5-tuple with three ``list[str]`` slots that a reordering at the call
    site could swap silently."""

    domain: str
    action: str
    area_ids: list[str]
    floor_ids: list[str]
    excluded_roots: list[str]


class _Topology(NamedTuple):
    """The five HA registry/state views one resolution reads, named instead
    of positionally unpacked -- a reordered ``gather()`` call would otherwise
    mislabel a registry in every downstream error message without mypy or a
    test catching it."""

    states: Any
    entities: Any
    devices: Any
    areas: Any
    floors: Any


@dataclass(frozen=True)
class BulkSelectorResolution:
    """One frozen selector resolution ready for preview or dispatch."""

    resolved_entity_ids: tuple[str, ...]
    excluded_entity_ids: tuple[str, ...]
    selected_area_ids: tuple[str, ...]
    expanded_group_ids: tuple[str, ...]
    hidden_entity_count: int
    warnings: tuple[str, ...] = field(default_factory=tuple)
    # repr=False: `_operation_common` can carry a lock or alarm code
    # (selector mode's `parameters` argument, e.g. `lock.open` with a
    # keypad code), so the default dataclass __repr__ would otherwise write
    # it to logs/tracebacks anywhere a resolution is printed or logged
    # unredacted. It stays compare=True (the default): it holds `action`,
    # so two resolutions over the identical entity set but OPPOSITE actions
    # must not compare equal, and `compare=False` would make them -- there
    # is no dispatch-irrelevant reading of this field despite its name.
    # hash=False, though: `@dataclass(frozen=True)` generates a real
    # __hash__ from every compare=True field, and a plain `dict` has none
    # -- left at its default, `hash(resolution)` would type-check (mypy
    # sees a real __hash__, isinstance(r, Hashable) is True) and then raise
    # `TypeError: unhashable type: 'dict'` at runtime the first time
    # anything actually hashes one. hash=False excludes just this field
    # from __hash__ while keeping it in __eq__, so the class is genuinely
    # hashable (hash/eq stay consistent) instead of merely claiming to be.
    _operation_common: dict[str, Any] = field(
        default_factory=dict, repr=False, hash=False
    )

    @property
    def operations(self) -> list[dict[str, Any]]:
        """Derive the dispatch rows from ``resolved_entity_ids`` on demand.

        Computed rather than stored so ``operations`` and
        ``resolved_entity_ids`` cannot drift apart -- a stored, separately
        constructed ``operations`` list could desync from the entity list
        the dry-run preview reports, silently making the preview disagree
        with what actually dispatches.
        """
        operations: list[dict[str, Any]] = []
        for entity_id in self.resolved_entity_ids:
            row = {"entity_id": entity_id, **self._operation_common}
            if "parameters" in row:
                # `**self._operation_common` only shallow-copies: every row
                # would otherwise share the SAME "parameters" dict object
                # (and the same nested values inside it, e.g. an
                # `rgb_color` list), so an in-place mutation of one row's
                # parameters -- even a nested one, like
                # `params["rgb_color"][0] = 0` -- would otherwise leak into
                # every other row. Deep, not shallow: a `dict(...)` copy
                # would only stop top-level key rebinding.
                row["parameters"] = copy.deepcopy(row["parameters"])
            operations.append(row)
        return operations

    def summary(self) -> dict[str, Any]:
        """Return the stable, visibility-safe resolution report."""
        result: dict[str, Any] = {
            "resolved_entity_ids": list(self.resolved_entity_ids),
            "excluded_entity_ids": list(self.excluded_entity_ids),
            "selected_area_ids": list(self.selected_area_ids),
            "expanded_group_ids": list(self.expanded_group_ids),
            "counts": {
                "resolved": len(self.resolved_entity_ids),
                "excluded": len(self.excluded_entity_ids),
                "groups_expanded": len(self.expanded_group_ids),
                "hidden": self.hidden_entity_count,
            },
        }
        if self.warnings:
            result["warnings"] = list(self.warnings)
        return result


class BulkSelectorValidationError(ValueError):
    """Report a caller-fixable selector error that must prevent every dispatch."""

    def __init__(self, message: str, *, parameter: str = "selector") -> None:
        super().__init__(message)
        self.parameter = parameter


class InfrastructureErrorCause(StrEnum):
    """Why a ``BulkSelectorInfrastructureError`` was raised, for routing the
    response's ``suggestions`` to an actionable next step -- not every
    infrastructure failure is a network problem. A closed set (StrEnum,
    matching ``ErrorCode``/``Verdict``/the rest of this codebase's error
    taxonomy) rather than an open ``str``: a typo in a raise site's
    ``cause=`` used to type-check as any other string and silently fall
    back to ``CONNECTIVITY``'s boilerplate at the lookup in
    ``tools_service.py``, keeping the suite green while quietly serving
    the wrong suggestions. mypy now catches that at the raise site instead.
    """

    CONNECTIVITY = "connectivity"
    """A genuinely unavailable/malformed HA registry or state response --
    "check HA is running" is the right advice. The default."""

    MALFORMED_DEVICE_REGISTRY = "malformed_device_registry"
    """A local, user-fixable data problem: a device-registry row missing
    ``id``. Network suggestions are actively unhelpful here."""

    VISIBILITY_CONFIG = "visibility_config"
    """A local, user-fixable data problem: a corrupt/unloadable
    ``entity_visibility.json``. Network suggestions are actively unhelpful
    here."""


class BulkSelectorInfrastructureError(RuntimeError):
    """Report a Home Assistant infrastructure failure hit while resolving a
    selector -- NOT the caller's fault, and never the caller's to fix by
    editing the selector. Kept a distinct type (not a
    ``BulkSelectorValidationError`` subclass) so ``tools_service.py`` routes
    it through the connection/infrastructure error path instead of
    ``VALIDATION_FAILED``: an agent that sees "selector is invalid" will
    rewrite a selector that was fine and retry against an HA outage no
    selector can fix.

    ``cause`` (see ``InfrastructureErrorCause``) distinguishes actionable
    next steps for the response's ``suggestions`` -- see
    ``tools_service.py``'s ``_INFRASTRUCTURE_ERROR_SUGGESTIONS``.
    """

    def __init__(
        self,
        message: str,
        *,
        cause: InfrastructureErrorCause = InfrastructureErrorCause.CONNECTIVITY,
    ) -> None:
        super().__init__(message)
        self.cause = cause


def _registry_rows(result: Any, label: str) -> list[dict[str, Any]]:
    """Unwrap one required Home Assistant registry response."""
    if not isinstance(result, Mapping) or result.get("success") is not True:
        raise BulkSelectorInfrastructureError(f"Home Assistant {label} is unavailable")
    rows = result.get("result")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise BulkSelectorInfrastructureError(
            f"Home Assistant {label} returned an unexpected response"
        )
    return rows


def _validate_device_registry_rows(device_rows: list[dict[str, Any]]) -> None:
    """Fail closed on a device row missing ``id``.

    ``visibility.resolver._parse_device_registry`` silently skips any device
    entry without an ``id`` even when the hidden-set resolver runs
    ``strict=True`` (see ``_load_hidden_entities``), so a malformed row would
    otherwise let a device-derived exclusion (area/label inherited by its
    entities) drop out of the hidden set without warning. Mirrors the
    per-entry validation ``_refresh_hidden_set`` in
    ``visibility/enforcement.py`` applies before calling the same strict
    resolver. Infrastructure-class (HA's own registry data is malformed),
    not the caller's selector to fix.
    """
    if any(not row.get("id") for row in device_rows):
        raise BulkSelectorInfrastructureError(
            "Home Assistant device registry returned a malformed entry",
            cause=InfrastructureErrorCause.MALFORMED_DEVICE_REGISTRY,
        )


def _string_list(selector: Mapping[str, Any], key: str) -> list[str]:
    """Read one optional exact-ID list without accepting scalar coercion."""
    raw = selector.get(key, [])
    if not isinstance(raw, list) or not all(
        isinstance(value, str) and value.strip() for value in raw
    ):
        raise BulkSelectorValidationError(
            f"selector.{key} must be a list of non-empty exact identifiers",
            parameter=f"selector.{key}",
        )
    # Strip so a padded id ("light.kitchen " passed the non-empty check
    # above but would otherwise be used verbatim -- entity/area/floor IDs
    # are never legitimately whitespace-padded, so this can only fix a typo,
    # never mask a real identifier.
    return sorted({value.strip() for value in raw})


def _entity_area_id(
    entity_id: str,
    entity_registry: Mapping[str, Mapping[str, Any]],
    device_areas: Mapping[str, str | None],
) -> str | None:
    """Resolve HA's entity-area override followed by device-area inheritance."""
    entry = entity_registry.get(entity_id)
    if not entry:
        return None
    if area_id := entry.get("area_id"):
        return str(area_id)
    device_id = entry.get("device_id")
    return device_areas.get(str(device_id)) if device_id else None


def _expand_entity(
    entity_id: str,
    states: Mapping[str, Mapping[str, Any]],
    *,
    path: tuple[str, ...],
    expanded_groups: set[str],
    cache: dict[str, _ExpansionEntry],
    parameter: str,
) -> set[str]:
    """Expand generic membership recursively and reject incomplete graph walks."""
    if entity_id in path:
        cycle_start = path.index(entity_id)
        cycle = " -> ".join((*path[cycle_start:], entity_id))
        raise BulkSelectorValidationError(
            f"Aggregate membership cycle detected: {cycle}",
            parameter=parameter,
        )
    if cached := cache.get(entity_id):
        expanded_groups.update(cached.groups)
        return set(cached.leaves)
    state = states.get(entity_id)
    if state is None:
        # `path[-1]`, when present, is the aggregate whose member list named
        # this entity_id -- without it, "light.gone does not exist" gives no
        # way to find which entity's member list is stale.
        referenced_by = f", referenced by '{path[-1]}'" if path else ""
        raise BulkSelectorValidationError(
            f"Entity '{entity_id}' does not exist or has no live state{referenced_by}",
            parameter=parameter,
        )
    members = normalize_member_entity_ids(state.get("attributes"))
    if members is None:
        cache[entity_id] = _ExpansionEntry(frozenset({entity_id}), frozenset())
        return {entity_id}
    expanded_groups.add(entity_id)
    if not members:
        cache[entity_id] = _ExpansionEntry(frozenset(), frozenset({entity_id}))
        return set()
    leaves: set[str] = set()
    nested_groups: set[str] = {entity_id}
    for member_id in members:
        member_groups: set[str] = set()
        leaves.update(
            _expand_entity(
                member_id,
                states,
                path=(*path, entity_id),
                expanded_groups=member_groups,
                cache=cache,
                parameter=parameter,
            )
        )
        nested_groups.update(member_groups)
    expanded_groups.update(nested_groups)
    cache[entity_id] = _ExpansionEntry(frozenset(leaves), frozenset(nested_groups))
    return leaves


def _validate_selector(selector: Mapping[str, Any], action: str) -> _ValidatedSelector:
    """Validate the public selector and return normalized exact identifiers."""
    if not isinstance(selector, Mapping):
        raise BulkSelectorValidationError("selector must be a JSON object")
    extra_keys = sorted(set(selector) - _SELECTOR_KEYS)
    if extra_keys:
        raise BulkSelectorValidationError(
            f"selector contains unsupported fields: {', '.join(extra_keys)}"
        )
    domain = selector.get("domain")
    if not isinstance(domain, str) or not domain.strip() or "." in domain:
        raise BulkSelectorValidationError(
            "selector.domain must be one exact Home Assistant domain",
            parameter="selector.domain",
        )
    domain = domain.strip().lower()
    normalized_action = action.strip().lower() if isinstance(action, str) else ""
    valid_actions = get_domain_handler(domain).get(
        "valid_actions", ["on", "off", "toggle"]
    )
    if not normalized_action or normalized_action not in valid_actions:
        raise BulkSelectorValidationError(
            f"Invalid action '{normalized_action}' for domain '{domain}'; "
            f"valid actions: {', '.join(valid_actions)}",
            parameter="action",
        )
    area_ids = _string_list(selector, "area_ids")
    floor_ids = _string_list(selector, "floor_ids")
    if not area_ids and not floor_ids:
        raise BulkSelectorValidationError(
            "selector requires at least one exact area_id or floor_id"
        )
    return _ValidatedSelector(
        domain=domain,
        action=normalized_action,
        area_ids=area_ids,
        floor_ids=floor_ids,
        excluded_roots=_string_list(selector, "exclude_entity_ids"),
    )


# Matches the positional order of the five awaitables in _load_topology's
# gather() call -- zipped in below so a failure's own log line names the
# ACTUAL registry that failed ("the device registry fetch failed") instead
# of a generic "a topology fetch failed" that gives an operator nothing to
# search HA's own logs for.
async def _load_topology(client: HomeAssistantClient) -> _Topology:
    """Load the HA state and registry views used by one resolution.

    ``return_exceptions=True`` plus an explicit re-raise guard, mirroring
    ``tools_areas.py``'s registry fetch: a bare ``asyncio.gather`` lets the
    first exception propagate while the other four awaitables are neither
    cancelled nor retrieved, so their eventual exceptions surface only as an
    unattributed "Task exception was never retrieved" with no tool or call
    context once the event loop garbage-collects them.

    Logs every failed fetch here (not just the one that gets raised, and
    not left to the caller's own log line, which only ever sees the single
    exception that propagates): with two+ concurrent fetches failing (e.g.
    both the entity and device registry reads), every failure after the
    first would otherwise vanish without a trace. ``%r`` plus
    ``exc_info=`` (not ``%s``): ``str()`` on the exception types these
    fetches actually raise -- ``asyncio.TimeoutError``, ``ConnectionResetError``
    -- is frequently empty, which would otherwise produce a content-free
    WARNING line for exactly the outage this logging exists to diagnose.
    """
    results: tuple[Any, Any, Any, Any, Any] = await asyncio.gather(
        client.get_states(),
        client.send_websocket_message({"type": "config/entity_registry/list"}),
        client.send_websocket_message({"type": "config/device_registry/list"}),
        client.send_websocket_message({"type": "config/area_registry/list"}),
        client.send_websocket_message({"type": "config/floor_registry/list"}),
        return_exceptions=True,
    )
    states, entities, devices, areas, floors = results
    # Labels paired with their named local, not zipped against `results` by
    # position: a `zip(strict=True)` against a separate label tuple only
    # catches a LENGTH change on reorder, not the reorder itself -- if a
    # future edit swaps two `gather()` lines and correctly updates the
    # unpack above to match, a position-based label tuple would silently
    # keep naming the OLD order, confidently blaming the wrong registry in
    # the WARNING below during exactly the outage this logging exists to
    # diagnose. Binding each label directly to its already-correct local
    # makes a mislabel impossible without an edit that is visibly wrong on
    # this line itself.
    labeled_results = (
        ("states", states),
        ("entity registry", entities),
        ("device registry", devices),
        ("area registry", areas),
        ("floor registry", floors),
    )
    failures = [
        (label, result)
        for label, result in labeled_results
        if isinstance(result, BaseException)
    ]
    if failures:
        for label, failure in failures:
            logger.warning(
                "bulk selector: %s fetch failed: %r", label, failure, exc_info=failure
            )
        raise failures[0][1]
    # Keyword construction, not `_Topology(*results)`: the NamedTuple exists
    # specifically so a reordered gather() call can't mislabel a registry
    # silently -- unpacking the gather() result and re-splatting it
    # positionally here would carry the exact risk it was written to avoid.
    return _Topology(
        states=states, entities=entities, devices=devices, areas=areas, floors=floors
    )


def _require_domain_known(domain: str, states: Mapping[str, Any]) -> None:
    """Fail fast when no entity of this domain exists anywhere in the
    instance -- catches a domain typo (e.g. "lights") independently of
    area selection. Distinct from the later matching_roots-empty check,
    which reports "not in the selected area(s)" for a domain that IS real."""
    if not any(entity_id.startswith(f"{domain}.") for entity_id in states):
        raise BulkSelectorValidationError(
            f"No entities of domain '{domain}' exist in this Home Assistant instance",
            parameter="selector.domain",
        )


def _all_excluded_or_hidden_message(*, excluded_count: int, hidden_count: int) -> str:
    """Build the final empty-result message with a visibility-safe count
    breakdown (never entity IDs) when available.

    Only ever called once ``resolve_bulk_selector``'s own empty-aggregate
    and wrong-domain gates have both already ruled themselves out (see the
    ``not directly_hidden`` guards above this function's one call site), so
    by construction at least one count here is non-zero -- refuse the
    zero/zero case outright rather than let a future regression upstream
    silently render a confident "excluded or hidden" claim with no
    evidence behind it.
    """
    if not excluded_count and not hidden_count:
        raise AssertionError(
            "_all_excluded_or_hidden_message called with both counts zero -- "
            "resolve_bulk_selector's empty-result gates should have already "
            "raised a more specific error for this case"
        )
    counts = []
    if excluded_count:
        counts.append(f"{excluded_count} excluded")
    if hidden_count:
        counts.append(f"{hidden_count} hidden")
    detail = f" ({', '.join(counts)})" if counts else ""
    return f"All matching entities were excluded or hidden{detail}"


def _select_area_ids(
    area_rows: list[dict[str, Any]],
    floor_rows: list[dict[str, Any]],
    area_ids: list[str],
    floor_ids: list[str],
) -> set[str]:
    """Validate exact topology IDs and expand floors to their areas."""
    areas = {str(row["area_id"]): row for row in area_rows if row.get("area_id")}
    floors = {str(row["floor_id"]) for row in floor_rows if row.get("floor_id")}
    unknown_areas = sorted(set(area_ids) - set(areas))
    unknown_floors = sorted(set(floor_ids) - floors)
    if unknown_areas or unknown_floors:
        # A caller that only has display names (e.g. from ha_search's
        # friendly area_names, or a floor's display name) will otherwise
        # retry with the same wrong value indefinitely -- area_ids/floor_ids
        # are exact HA registry IDs (usually a lowercase, underscored slug
        # like "living_room"), not the names shown in the UI, and nothing
        # else in this error says where to find the real ones.
        details = []
        if unknown_areas:
            details.append(f"unknown area_ids: {', '.join(unknown_areas)}")
        if unknown_floors:
            details.append(f"unknown floor_ids: {', '.join(unknown_floors)}")
        raise BulkSelectorValidationError(
            "; ".join(details) + ". area_ids/floor_ids must be exact Home "
            "Assistant registry IDs, not display names shown in the UI or "
            "returned as ha_search's 'area_names' -- call ha_list_floors_areas "
            "to look up the exact area_id/floor_id for each area or floor."
        )
    selected = set(area_ids)
    selected.update(
        area_id for area_id, row in areas.items() if row.get("floor_id") in floor_ids
    )
    return selected


def _expand_roots(
    roots: list[str],
    states: Mapping[str, Mapping[str, Any]],
    cache: dict[str, _ExpansionEntry],
    parameter: str,
) -> _ExpansionEntry:
    """Expand a list of aggregate or leaf roots into a deduplicated leaf set
    and the aggregates discovered while expanding them.

    Returns an ``_ExpansionEntry`` rather than mutating a caller-supplied
    out-parameter: ``resolve_bulk_selector`` calls this once for the
    selection side and once for the exclusion side, and the exclusion
    side's groups were previously collected into an accumulator that was
    allocated, passed in, and never read again -- write-only. Returning
    both halves makes "does this call site actually use the discovered
    groups" an explicit choice at each call site instead of a set that
    might or might not get inspected later.
    """
    leaves: set[str] = set()
    groups: set[str] = set()
    for entity_id in roots:
        leaves.update(
            _expand_entity(
                entity_id,
                states,
                path=(),
                expanded_groups=groups,
                cache=cache,
                parameter=parameter,
            )
        )
    return _ExpansionEntry(frozenset(leaves), frozenset(groups))


async def _load_hidden_entities(
    client: Any,
    entity_result: Any,
    states_result: list[dict[str, Any]],
    device_result: Any,
    entity_registry: Mapping[str, Mapping[str, Any]],
) -> tuple[set[str], list[str]]:
    """Load visibility policy strictly and include registry-hidden entities.

    Returns ``(hidden_ids, warnings)``. The warnings are operator-facing
    visibility-config notes (e.g. an unknown ``exclude_categories`` entry)
    that ``load_hidden_set`` still returns even under ``strict=True`` --
    per its own docstring, "benign notes ... still warn, never raise" is
    orthogonal to the strict/fail-closed behavior, which only covers
    degradations that would otherwise leak entities. Threaded into the
    resolution's own ``warnings`` by the caller rather than discarded, so
    whoever owns ``entity_visibility.json`` learns about a config problem
    this resolution surfaced on their behalf.
    """
    try:
        visibility_hidden, warnings = await load_hidden_set(
            entity_result,
            states_result,
            client,
            device_result,
            strict=True,
        )
    except VisibilityDataUnavailable as exc:
        # Preserve and log the underlying cause: VisibilityDataUnavailable
        # covers four distinct degradations (registry unusable, allowlist
        # registry empty, Assist data unavailable, config load failure) --
        # collapsing them to one fixed sentence here would erase which one
        # actually happened, and nothing else records it server-side.
        logger.warning("bulk selector: visibility resolution failed: %s", exc)
        raise BulkSelectorInfrastructureError(
            f"Entity visibility could not be resolved safely: {exc}",
            # "visibility_config", not the "connectivity" default: one of
            # the four VisibilityDataUnavailable causes this wraps is a
            # corrupt/unloadable entity_visibility.json in the user's own
            # data dir -- not distinguished from the other three (HA
            # registry/Assist-data unavailable) here, so the routed
            # suggestions below hedge across both rather than confidently
            # pointing at the wrong one.
            cause=InfrastructureErrorCause.VISIBILITY_CONFIG,
        ) from exc
    registry_hidden = {
        entity_id
        for entity_id, row in entity_registry.items()
        if row.get("hidden_by") is not None
    }
    return visibility_hidden | registry_hidden, warnings


async def resolve_bulk_selector(
    client: HomeAssistantClient,
    selector: Mapping[str, Any],
    *,
    action: str,
    parameters: dict[str, Any] | None,
    timeout_seconds: float | None,
    validate_first: bool,
) -> BulkSelectorResolution:
    """Resolve exact HA topology to visible, non-excluded leaf operations.

    All registry/state reads and validation finish before the caller receives any
    operation row, so a malformed selector can never produce a partial dispatch.
    Membership is integration-neutral and uses the shared normalized attributes
    supplied by Home Assistant; no manufacturer, model, or integration heuristics
    apply. The one domain-level exception is ``scene`` (see
    ``_NON_AGGREGATE_ROOT_DOMAINS``): HA core gives that domain's ``entity_id``
    attribute a different meaning (controlled targets, not structural members),
    so it is excluded from aggregate-root admission on architectural grounds,
    not integration identity.
    """
    validated = _validate_selector(selector, action)
    # Attribute access, not a positional unpack: `_ValidatedSelector` exists
    # specifically so a reordering of its fields can't swap two `list[str]`
    # slots silently -- unpacking it positionally here would defeat that.
    domain = validated.domain
    action = validated.action
    area_ids = validated.area_ids
    floor_ids = validated.floor_ids
    excluded_roots = validated.excluded_roots
    # No try/except here: _load_topology already logs every failed fetch
    # (with its own registry label, %r, and exc_info=) before raising, so
    # a second, generic log line here would only duplicate it with less
    # information.
    topology = await _load_topology(client)
    if not isinstance(topology.states, list) or not all(
        isinstance(state, dict) for state in topology.states
    ):
        raise BulkSelectorInfrastructureError(
            "Home Assistant states returned an unexpected response"
        )
    entity_rows = _registry_rows(topology.entities, "entity registry")
    device_rows = _registry_rows(topology.devices, "device registry")
    _validate_device_registry_rows(device_rows)
    selected_areas = _select_area_ids(
        _registry_rows(topology.areas, "area registry"),
        _registry_rows(topology.floors, "floor registry"),
        area_ids,
        floor_ids,
    )
    states = {
        str(state["entity_id"]): state
        for state in topology.states
        if isinstance(state.get("entity_id"), str)
    }
    _require_domain_known(domain, states)
    entity_registry = {
        str(row["entity_id"]): row
        for row in entity_rows
        if isinstance(row.get("entity_id"), str)
    }
    device_areas = {
        str(row["id"]): row.get("area_id") for row in device_rows if row.get("id")
    }
    hidden, visibility_warnings = await _load_hidden_entities(
        client, topology.entities, topology.states, topology.devices, entity_registry
    )
    # A root only needs to LIVE in the selected area; it does not need to be
    # of the target domain itself. A `group`/other-domain aggregate assigned
    # to the area (e.g. `group.living_room_lights`) is included here too, so
    # its membership expansion below can surface `light.*` leaves that have
    # no individual area assignment of their own. `scene.*` is excluded (see
    # module docstring note / `_NON_AGGREGATE_ROOT_DOMAINS`): its entity_id
    # attribute lists controlled targets, not structural members, and
    # admitting it here would let a scene assigned to the area pull entities
    # from anywhere in the house into the dispatch. The target-domain filter
    # is re-applied to the expanded leaves further down.
    matching_roots = {
        entity_id
        for entity_id, state in states.items()
        if _entity_area_id(entity_id, entity_registry, device_areas) in selected_areas
        and (
            entity_id.startswith(f"{domain}.")
            or (
                not any(
                    entity_id.startswith(f"{d}.") for d in _NON_AGGREGATE_ROOT_DOMAINS
                )
                and normalize_member_entity_ids(state.get("attributes")) is not None
            )
        )
    }
    if not matching_roots:
        raise BulkSelectorValidationError(
            f"No entities of domain '{domain}' exist in the selected area(s)"
        )
    directly_hidden = matching_roots & hidden
    candidate_roots = sorted(matching_roots - hidden)
    # Two independent calls, not one shared out-param: expanded_group_ids in
    # the final resolution must report only aggregates discovered while
    # expanding the SELECTION side. An excluded aggregate (and any of its
    # own nested sub-groups) is not something that fed the dispatch --
    # reporting it in the same set would misleadingly suggest it did, so the
    # exclusion side's own `.groups` is deliberately never read below. The
    # memoization cache stays shared: an entity referenced by both sides
    # should still only be expanded once.
    expansion_cache: dict[str, _ExpansionEntry] = {}
    selection_expansion = _expand_roots(
        candidate_roots, states, expansion_cache, "selector"
    )
    exclusion_expansion = _expand_roots(
        excluded_roots,
        states,
        expansion_cache,
        "selector.exclude_entity_ids",
    )
    expanded_leaves = selection_expansion.leaves
    excluded_leaves = exclusion_expansion.leaves
    expanded_groups = selection_expansion.groups
    selected_leaves = {
        entity_id for entity_id in expanded_leaves if entity_id.startswith(f"{domain}.")
    }
    # Both branches below are gated on `not directly_hidden`: `candidate_roots`
    # already subtracts `hidden`, so if ANY matching root is hidden, some of
    # the "wrong domain" or "empty aggregate" evidence below could really be
    # explained by that hidden entity having been the one that mattered --
    # e.g. one visible root expands to a different domain while a SEPARATE,
    # hidden root would have matched directly. `directly_hidden` is the more
    # actionable fact in that case, so neither branch fires and both counts
    # fall through together into `_all_excluded_or_hidden_message` below.
    # `directly_hidden` empty additionally guarantees `candidate_roots` is
    # non-empty here (matching_roots was already confirmed non-empty above,
    # and nothing in it is hidden), so neither branch needs its own
    # `candidate_roots` check.
    if not directly_hidden and not expanded_leaves:
        # A matched aggregate (or every one of several) has literally no
        # members -- `normalize_member_entity_ids` returned `[]`, not
        # `None`, so it WAS admitted as a root, but its own expansion
        # contributed nothing. Distinct from "wrong domain" below: no leaf
        # of ANY domain resulted, so there is nothing to blame on a
        # different domain either.
        raise BulkSelectorValidationError(
            f"No entities of domain '{domain}' exist in the selected area(s) -- "
            "the matched aggregate(s) have no members"
        )
    if not directly_hidden and expanded_leaves and not selected_leaves:
        # A fourth, distinct empty-result cause: matching_roots was
        # non-empty (a non-scene aggregate of a DIFFERENT domain qualified
        # as a root, e.g. a `group.living_room` whose members are all
        # `switch.*`), so expansion ran and produced leaves, but none of
        # them were the target domain -- neither exclusion nor visibility
        # ever entered into it. Caught here, before either subtraction
        # below, so it can never be misreported as "excluded or hidden".
        raise BulkSelectorValidationError(
            f"No entities of domain '{domain}' exist in the selected area(s) -- "
            "a matched aggregate's members are all a different domain"
        )
    effective_excluded = selected_leaves & excluded_leaves
    selected_leaves.difference_update(excluded_leaves)
    hidden_selected = selected_leaves & hidden
    selected_leaves.difference_update(hidden)
    resolved_entity_ids = sorted(selected_leaves)
    if not resolved_entity_ids:
        raise BulkSelectorValidationError(
            _all_excluded_or_hidden_message(
                excluded_count=len(effective_excluded),
                hidden_count=len(directly_hidden | hidden_selected),
            )
        )
    if len(resolved_entity_ids) > MAX_SELECTOR_ENTITIES:
        raise BulkSelectorValidationError(
            f"The selector resolved to {len(resolved_entity_ids)} entities; "
            f"the maximum is {MAX_SELECTOR_ENTITIES}"
        )
    operation_common: dict[str, Any] = {
        "action": action,
        "validate_first": validate_first,
    }
    if parameters is not None:
        # Deep copy, not the caller's own object by reference (and not a
        # shallow `dict(...)`, which stops only top-level rebinding and
        # still shares nested values like an `rgb_color` list): the
        # per-row copy in BulkSelectorResolution.operations protects each
        # DISPATCH row from cross-row mutation, but does nothing about the
        # resolution's own stored copy -- without this, a caller that
        # still holds `parameters` and mutates it (at any depth) after
        # this call returns would silently rewrite the "frozen"
        # resolution's own payload too.
        operation_common["parameters"] = copy.deepcopy(parameters)
    if timeout_seconds is not None:
        operation_common["timeout_seconds"] = timeout_seconds
    excluded_and_hidden = effective_excluded & hidden
    hidden_count = len(directly_hidden | hidden_selected | excluded_and_hidden)
    warnings: list[str] = list(visibility_warnings)
    if hidden_count:
        # .gemini/styleguide.md "Tool Tags and Return Values": a degraded
        # result belongs in top-level warnings, not a bare count the caller has no
        # actionable text for. `counts.hidden` alone cannot tell a user
        # "three entities you wanted controlled were skipped" from "your
        # exclusion was redundant" -- this at least names that something
        # was hidden without leaking which entity (visibility-safe).
        warnings.append(
            f"{hidden_count} matching entit{'y was' if hidden_count == 1 else 'ies were'} "
            "hidden by the entity visibility filter and excluded from this result."
        )
    return BulkSelectorResolution(
        resolved_entity_ids=tuple(resolved_entity_ids),
        excluded_entity_ids=tuple(sorted(effective_excluded - hidden)),
        selected_area_ids=tuple(sorted(selected_areas)),
        expanded_group_ids=tuple(sorted(expanded_groups - hidden)),
        hidden_entity_count=hidden_count,
        warnings=tuple(warnings),
        _operation_common=operation_common,
    )
