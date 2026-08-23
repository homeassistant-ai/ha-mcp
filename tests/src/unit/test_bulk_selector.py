"""Regression tests for deterministic structural bulk selection."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools import bulk_selector
from ha_mcp.tools.bulk_selector import (
    BulkSelectorInfrastructureError,
    BulkSelectorValidationError,
    resolve_bulk_selector,
)


class SelectorClient:
    """Minimal HA client exposing one consistent topology snapshot."""

    def __init__(
        self,
        *,
        states: list[dict[str, Any]],
        entities: list[dict[str, Any]],
        devices: list[dict[str, Any]] | None = None,
        areas: list[dict[str, Any]] | None = None,
        floors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.states = states
        self.registries = {
            "config/entity_registry/list": entities,
            "config/device_registry/list": devices or [],
            "config/area_registry/list": areas
            or [{"area_id": "salon", "name": "Salon", "floor_id": None}],
            "config/floor_registry/list": floors or [],
        }

    async def get_states(self) -> list[dict[str, Any]]:
        return self.states

    async def send_websocket_message(self, message: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "result": self.registries[message["type"]]}


def _state(entity_id: str, members: list[str] | None = None) -> dict[str, Any]:
    attributes: dict[str, Any] = {"friendly_name": entity_id}
    if members is not None:
        attributes["entity_id"] = members
    return {"entity_id": entity_id, "state": "on", "attributes": attributes}


@pytest.fixture(autouse=True)
def _visible_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bulk_selector, "load_hidden_set", AsyncMock(return_value=(set(), []))
    )


@pytest.mark.asyncio
async def test_excluded_leaf_inside_nested_aggregate_is_never_an_operation() -> None:
    """Exclusion subtraction happens after recursive membership expansion."""
    client = SelectorClient(
        states=[
            _state("light.salon_group", ["light.nested", "light.table"]),
            _state("light.nested", ["light.vitrine", "light.sofa"]),
            _state("light.vitrine"),
            _state("light.sofa"),
            _state("light.table"),
        ],
        entities=[
            {"entity_id": "light.salon_group", "area_id": "salon"},
            {"entity_id": "light.nested", "area_id": None},
            {"entity_id": "light.vitrine", "area_id": None},
            {"entity_id": "light.sofa", "area_id": None},
            {"entity_id": "light.table", "area_id": None},
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {
            "domain": "light",
            "area_ids": ["salon"],
            "exclude_entity_ids": ["light.vitrine"],
        },
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.resolved_entity_ids == ("light.sofa", "light.table")
    assert result.excluded_entity_ids == ("light.vitrine",)
    assert {row["entity_id"] for row in result.operations} == {
        "light.sofa",
        "light.table",
    }


@pytest.mark.asyncio
async def test_floor_and_device_area_inheritance_resolve_exactly() -> None:
    """Floor expansion includes entities inheriting an area from their device."""
    client = SelectorClient(
        states=[_state("light.inherited"), _state("light.other")],
        entities=[
            {"entity_id": "light.inherited", "area_id": None, "device_id": "dev1"},
            {"entity_id": "light.other", "area_id": "other", "device_id": None},
        ],
        devices=[{"id": "dev1", "area_id": "salon"}],
        areas=[
            {"area_id": "salon", "floor_id": "ground"},
            {"area_id": "other", "floor_id": "upper"},
        ],
        floors=[{"floor_id": "ground"}, {"floor_id": "upper"}],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "floor_ids": ["ground"]},
        action="off",
        parameters=None,
        timeout_seconds=5,
        validate_first=True,
    )

    assert result.selected_area_ids == ("salon",)
    assert result.operations == [
        {
            "entity_id": "light.inherited",
            "action": "off",
            "validate_first": True,
            "timeout_seconds": 5,
        }
    ]


@pytest.mark.asyncio
async def test_membership_cycle_fails_before_returning_operations() -> None:
    """A cyclic aggregate graph is an all-or-nothing preflight failure."""
    client = SelectorClient(
        states=[
            _state("light.one", ["light.two"]),
            _state("light.two", ["light.one"]),
        ],
        entities=[
            {"entity_id": "light.one", "area_id": "salon"},
            {"entity_id": "light.two", "area_id": None},
        ],
    )

    with pytest.raises(BulkSelectorValidationError, match="cycle"):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_dangling_member_error_names_the_referencing_aggregate() -> None:
    """A stale member ID must name WHICH aggregate's member list is stale.

    ``_expand_entity``'s ``referenced_by`` attribution (``path[-1]``) is the
    only thing that lets an operator find the actual misconfigured
    aggregate -- "light.gone does not exist" alone gives no way to locate
    which entity's member list needs fixing.
    """
    client = SelectorClient(
        states=[_state("light.group", ["light.gone"])],
        entities=[{"entity_id": "light.group", "area_id": "salon"}],
    )

    with pytest.raises(
        BulkSelectorValidationError,
        match=r"'light\.gone' does not exist.*referenced by 'light\.group'",
    ):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_visibility_filters_positive_leaf_but_keeps_exclusion_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden leaves affect safety without leaking their IDs in the report."""
    monkeypatch.setattr(
        bulk_selector,
        "load_hidden_set",
        AsyncMock(return_value=({"light.hidden", "light.secret"}, [])),
    )
    client = SelectorClient(
        states=[
            _state("light.group", ["light.visible", "light.hidden", "light.secret"]),
            _state("light.visible"),
            _state("light.hidden"),
            _state("light.secret"),
        ],
        entities=[
            {"entity_id": "light.group", "area_id": "salon"},
            {"entity_id": "light.visible", "area_id": None},
            {"entity_id": "light.hidden", "area_id": None},
            {"entity_id": "light.secret", "area_id": None},
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {
            "domain": "light",
            "area_ids": ["salon"],
            "exclude_entity_ids": ["light.secret"],
        },
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.resolved_entity_ids == ("light.visible",)
    assert result.excluded_entity_ids == ()
    assert result.hidden_entity_count == 2


@pytest.mark.asyncio
async def test_directly_hidden_matching_root_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hidden entity assigned directly to the area contributes to the count."""
    mock_load_hidden_set = AsyncMock(return_value=({"light.hidden"}, []))
    monkeypatch.setattr(bulk_selector, "load_hidden_set", mock_load_hidden_set)
    client = SelectorClient(
        states=[_state("light.visible"), _state("light.hidden")],
        entities=[
            {"entity_id": "light.visible", "area_id": "salon"},
            {"entity_id": "light.hidden", "area_id": "salon"},
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "area_ids": ["salon"]},
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.resolved_entity_ids == ("light.visible",)
    assert result.hidden_entity_count == 1
    # The resolver must ask the real resolver for the FAIL-CLOSED behavior
    # (strict=True): the mock accepts and discards kwargs, so nothing else
    # in this file would notice if `_load_hidden_entities` stopped passing
    # it -- silently falling back to strict's default (False, fail-OPEN)
    # would dispatch to hidden entities whenever the visibility config
    # fails to load, with this whole suite still green.
    assert mock_load_hidden_set.await_args.kwargs["strict"] is True
    # The resolver itself (not just the tool layer, which some tests
    # exercise with a hand-constructed BulkSelectorResolution(warnings=...))
    # must be the one producing this warning.
    assert result.warnings != ()


@pytest.mark.asyncio
async def test_load_hidden_set_warnings_are_threaded_into_the_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``load_hidden_set``'s own operator-facing warnings (e.g. an unknown
    ``exclude_categories`` entry -- benign, so returned as a warning even
    under ``strict=True``, per its own docstring) must reach
    ``resolution.warnings`` even when nothing is hidden.

    Distinct from ``test_directly_hidden_matching_root_is_counted`` above:
    that test's own ``result.warnings != ()`` assertion is satisfied by
    the SEPARATE hidden-count warning this resolver builds itself, since
    its mock returns an empty warnings list -- so replacing
    ``list(visibility_warnings)`` with ``[]`` at the call site would leave
    that test (and the whole suite) green. Zero hidden entities here means
    the hidden-count warning never fires, isolating this one specifically.
    """
    monkeypatch.setattr(
        bulk_selector,
        "load_hidden_set",
        AsyncMock(
            return_value=(
                set(),
                ["Unknown exclude_categories dropped: typo_category"],
            )
        ),
    )
    client = SelectorClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "area_ids": ["salon"]},
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.hidden_entity_count == 0
    assert result.warnings == ("Unknown exclude_categories dropped: typo_category",)


@pytest.mark.asyncio
async def test_registry_hidden_by_excludes_even_with_visibility_filter_disabled() -> (
    None
):
    """An entity hidden via HA's own registry (`hidden_by`, e.g. the user
    hid it in the HA UI) must be excluded even when ha-mcp's own opt-in
    visibility filter is off/unconfigured.

    This is NOT covered by the module's autouse mock (`load_hidden_set` ->
    `(set(), [])`): registry_hidden in `_load_hidden_entities` is computed
    directly from the entity registry's own `hidden_by` field, independent
    of whatever `load_hidden_set` returns. `config.exclude_hidden` (the
    opt-in filter's own hidden-respecting dimension) defaults to False, so
    on a default ha-mcp install with the visibility filter never
    configured, this is the ONLY thing honoring HA's native "hide this
    entity" toggle for bulk selectors -- deleting it would let a selector
    dispatch to an entity the user explicitly hid in the HA UI, with the
    rest of this suite (all of which uses entities with no `hidden_by`)
    staying green.
    """
    client = SelectorClient(
        states=[_state("light.visible"), _state("light.hidden_by_user")],
        entities=[
            {"entity_id": "light.visible", "area_id": "salon"},
            {
                "entity_id": "light.hidden_by_user",
                "area_id": "salon",
                "hidden_by": "user",
            },
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "area_ids": ["salon"]},
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.resolved_entity_ids == ("light.visible",)
    assert result.hidden_entity_count == 1


@pytest.mark.asyncio
async def test_cross_domain_aggregate_root_expands_into_target_domain_leaves() -> None:
    """An area-assigned aggregate of a different domain still expands.

    ``group.living_room_lights`` is not itself a ``light.*`` entity, so it
    must not be excluded from the root candidates just because its own
    domain doesn't match the selector. Its ungrouped ``light.*`` members
    (no individual area assignment) must still be discovered via membership
    expansion, then filtered to the requested domain.
    """
    client = SelectorClient(
        states=[
            _state("group.living_room_lights", ["light.ceiling", "light.lamp"]),
            _state("light.ceiling"),
            _state("light.lamp"),
        ],
        entities=[
            {"entity_id": "group.living_room_lights", "area_id": "salon"},
            {"entity_id": "light.ceiling", "area_id": None},
            {"entity_id": "light.lamp", "area_id": None},
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "area_ids": ["salon"]},
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.resolved_entity_ids == ("light.ceiling", "light.lamp")


@pytest.mark.asyncio
async def test_scene_in_selected_area_does_not_contribute_leaves() -> None:
    """A scene's entity_id attribute names controlled targets, not members.

    HA's native scene platform sets its own `entity_id` state attribute to
    the entities the scene CONFIGURES (homeassistant/components/
    homeassistant/scene.py), which routinely live elsewhere in the house.
    A scene assigned to a selected area must never be treated as an
    aggregate root -- doing so would pull an out-of-area (or no-area)
    entity into the dispatch, defeating the exclusion invariant this
    feature exists to guarantee.
    """
    client = SelectorClient(
        states=[
            _state("scene.movie_night", ["light.bedroom_lamp"]),
            _state("light.salon_ceiling"),
            _state("light.bedroom_lamp"),
        ],
        entities=[
            {"entity_id": "scene.movie_night", "area_id": "salon"},
            {"entity_id": "light.salon_ceiling", "area_id": "salon"},
            {"entity_id": "light.bedroom_lamp", "area_id": "bedroom"},
        ],
        areas=[
            {"area_id": "salon", "name": "Salon", "floor_id": None},
            {"area_id": "bedroom", "name": "Bedroom", "floor_id": None},
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "area_ids": ["salon"]},
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.resolved_entity_ids == ("light.salon_ceiling",)


@pytest.mark.asyncio
async def test_unknown_domain_fails_closed_with_a_clear_message() -> None:
    """A domain typo (e.g. 'lights') must not silently resolve to nothing.

    `get_domain_handler` returns the generic default handler for any
    unrecognized domain, so a typo'd domain can pass both the domain-shape
    check and the action-validity check (the default handler's
    valid_actions happens to include "off") and previously fell through to
    the generic "no visible leaf entities" message -- which wrongly points
    the caller at their (correct) exclusions/areas instead of the actual
    typo.
    """
    client = SelectorClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )

    with pytest.raises(
        BulkSelectorValidationError, match="No entities of domain 'lights'"
    ):
        await resolve_bulk_selector(
            client,
            {"domain": "lights", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_invalid_action_for_a_real_domain_names_valid_actions() -> None:
    """An action typo against an otherwise-real, present domain is rejected
    by ``_validate_selector`` itself, before any registry/state fetch --
    distinct from the domain-typo case above (a bad *domain*), this
    exercises the action/domain cross-check with a domain that IS real.
    """
    client = SelectorClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )

    with pytest.raises(
        BulkSelectorValidationError,
        match="Invalid action 'explode' for domain 'light'",
    ):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="explode",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_valid_domain_absent_from_selected_area_names_the_area_not_exclusions() -> (
    None
):
    """A real domain with zero entities in the selected area gets its own message.

    Distinct from both the domain-typo case above (no entities of that
    domain ANYWHERE) and the all-excluded-or-hidden case below (candidates
    existed but none survived) -- this is "the domain is real, but not in
    this area", which used to share the same generic "no visible leaf
    entities after exclusions" wording as the all-excluded-or-hidden case.
    """
    client = SelectorClient(
        states=[_state("light.elsewhere")],
        entities=[{"entity_id": "light.elsewhere", "area_id": "bedroom"}],
        areas=[
            {"area_id": "salon", "name": "Salon", "floor_id": None},
            {"area_id": "bedroom", "name": "Bedroom", "floor_id": None},
        ],
    )

    with pytest.raises(
        BulkSelectorValidationError,
        match="No entities of domain 'light' exist in the selected area",
    ):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_cross_domain_aggregate_with_no_matching_leaves_names_the_domain() -> (
    None
):
    """A fourth, distinct empty-result cause: a non-scene aggregate of a
    DIFFERENT domain qualifies as a root (matching_roots is non-empty), but
    none of its expanded leaves are the target domain -- neither exclusion
    nor visibility ever entered into it. Must not be misreported as "all
    matching entities were excluded or hidden", which would send the user
    to audit an empty exclude_entity_ids list and a visibility config
    doing nothing.
    """
    client = SelectorClient(
        states=[
            _state("group.living_room", ["switch.a", "switch.b"]),
            _state("switch.a"),
            _state("switch.b"),
            # Elsewhere in the house, so 'light' is a real domain (passes
            # the earlier domain-known-at-all check) but never a candidate
            # here: different area, not part of this aggregate.
            _state("light.elsewhere"),
        ],
        entities=[
            {"entity_id": "group.living_room", "area_id": "salon"},
            {"entity_id": "switch.a", "area_id": None},
            {"entity_id": "switch.b", "area_id": None},
            {"entity_id": "light.elsewhere", "area_id": "bedroom"},
        ],
        areas=[
            {"area_id": "salon", "name": "Salon", "floor_id": None},
            {"area_id": "bedroom", "name": "Bedroom", "floor_id": None},
        ],
    )

    with pytest.raises(
        BulkSelectorValidationError,
        match="No entities of domain 'light' exist in the selected area",
    ):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_all_roots_hidden_reports_hidden_not_domain_missing() -> None:
    """Regression: when EVERY matching root in the area is hidden,
    ``candidate_roots`` (which subtracts ``hidden`` before expansion) is
    itself empty, so ``expanded_leaves`` is empty too -- a narrower
    "expanded_leaves and not selected_leaves" guard would misfire on this
    exact input the same way an unconditional "not selected_leaves" guard
    did, reporting "No entities of domain 'light' exist in the selected
    area(s)" for an entity that DOES exist and IS in the area; it is
    simply hidden. Must report the hidden cause instead, matching the
    pre-existing behavior for a partially-hidden match set.
    """
    client = SelectorClient(
        states=[_state("light.hidden_by_user")],
        entities=[
            {
                "entity_id": "light.hidden_by_user",
                "area_id": "salon",
                "hidden_by": "user",
            }
        ],
    )

    with pytest.raises(
        BulkSelectorValidationError,
        match=r"excluded or hidden \(1 hidden\)",
    ):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_all_matches_excluded_names_exclusion_not_area() -> None:
    """Candidates existed in-area but all were excluded: a distinct message
    from "domain not in area" above -- this is the caller's exclusion (or
    visibility) eating the whole result, not a wrong area/domain. The
    message also carries a visibility-safe count breakdown (never entity
    IDs) so the user knows there was exactly one exclusion, not a wrong
    guess at how many."""
    client = SelectorClient(
        states=[_state("light.only")],
        entities=[{"entity_id": "light.only", "area_id": "salon"}],
    )

    with pytest.raises(
        BulkSelectorValidationError, match=r"excluded or hidden \(1 excluded\)"
    ):
        await resolve_bulk_selector(
            client,
            {
                "domain": "light",
                "area_ids": ["salon"],
                "exclude_entity_ids": ["light.only"],
            },
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_all_matches_excluded_and_hidden_reports_both_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both counts together, not just excluded-only (above) or hidden-only
    (test_all_roots_hidden_reports_hidden_not_domain_missing).
    ``_all_excluded_or_hidden_message``'s "both non-zero" branch has no
    test pinning its exact formatting -- a swapped or dropped count there
    would pass every existing test, since the excluded-only and
    hidden-only tests each exercise only ONE of its two conditionals.
    """
    monkeypatch.setattr(
        bulk_selector,
        "load_hidden_set",
        AsyncMock(return_value=({"light.hidden_one"}, [])),
    )
    client = SelectorClient(
        states=[_state("light.excluded_one"), _state("light.hidden_one")],
        entities=[
            {"entity_id": "light.excluded_one", "area_id": "salon"},
            {"entity_id": "light.hidden_one", "area_id": "salon"},
        ],
    )

    with pytest.raises(
        BulkSelectorValidationError,
        match=r"excluded or hidden \(1 excluded, 1 hidden\)",
    ):
        await resolve_bulk_selector(
            client,
            {
                "domain": "light",
                "area_ids": ["salon"],
                "exclude_entity_ids": ["light.excluded_one"],
            },
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_area_ids_strips_whitespace_padding() -> None:
    """A padded area_id (e.g. from copy-paste) is used verbatim, not silently
    ignored -- `_string_list` strips before returning, so it can only fix a
    typo, never mask a real (deliberately whitespace-containing) value,
    since HA IDs are never legitimately whitespace-padded."""
    client = SelectorClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "area_ids": [" salon "]},
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.resolved_entity_ids == ("light.one",)


@pytest.mark.asyncio
async def test_topology_fetch_failure_propagates_without_orphaning_tasks() -> None:
    """One failed registry fetch must surface, not vanish behind a bare gather.

    Mirrors ``tools_areas.py``'s registry-fetch pattern: ``return_exceptions=True``
    plus an explicit re-raise means a failure on any of the five concurrent
    fetches is reported, instead of a bare ``asyncio.gather`` where the first
    exception propagates while the other in-flight awaitables are neither
    cancelled nor retrieved (surfacing later as an unattributed "Task
    exception was never retrieved").
    """

    class FailingClient(SelectorClient):
        async def send_websocket_message(
            self, message: dict[str, Any]
        ) -> dict[str, Any]:
            if message["type"] == "config/device_registry/list":
                raise ConnectionError("websocket dropped")
            return await super().send_websocket_message(message)

    client = FailingClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )

    with pytest.raises(ConnectionError, match="websocket dropped"):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_topology_multi_failure_logs_every_fetch_not_just_the_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two simultaneous fetch failures must BOTH be logged, by name.

    The single-failure test above never exercises the "more than one
    failure" branch. Also pins ``%r``/``exc_info=`` over ``%s``:
    ``str(asyncio.TimeoutError())``/``str(ConnectionResetError())`` are
    both empty, so a ``%s``-formatted WARNING for exactly this kind of
    outage would read as content-free noise, defeating the point of
    logging every failure instead of just the one that gets raised.
    """

    class DoubleFailingClient(SelectorClient):
        async def send_websocket_message(
            self, message: dict[str, Any]
        ) -> dict[str, Any]:
            if message["type"] == "config/device_registry/list":
                raise ConnectionResetError()
            if message["type"] == "config/area_registry/list":
                raise TimeoutError()
            return await super().send_websocket_message(message)

    client = DoubleFailingClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises((ConnectionResetError, TimeoutError)),
    ):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("device registry" in message for message in warnings)
    assert any("area registry" in message for message in warnings)


@pytest.mark.asyncio
async def test_visibility_data_unavailable_is_infrastructure_not_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strict-resolver visibility failure is HA's fault, not the selector's.

    Preserves and surfaces the underlying VisibilityDataUnavailable message
    (one of four distinct causes in visibility/resolver.py) rather than a
    fixed generic sentence, and raises the infrastructure-class exception so
    the caller is routed to check HA connectivity, not rewrite a fine
    selector.
    """
    monkeypatch.setattr(
        bulk_selector,
        "load_hidden_set",
        AsyncMock(
            side_effect=bulk_selector.VisibilityDataUnavailable(
                "entity visibility config could not be loaded"
            )
        ),
    )
    client = SelectorClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )

    with pytest.raises(
        BulkSelectorInfrastructureError,
        match="entity visibility config could not be loaded",
    ) as exc_info:
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )
    # "visibility_config", not the connectivity default: this cause routes
    # to settings-UI/config-file guidance, not "check your HOMEASSISTANT_URL".
    assert exc_info.value.cause == "visibility_config"


@pytest.mark.asyncio
async def test_registry_unavailable_defaults_to_connectivity_cause() -> None:
    """A genuinely unavailable HA registry keeps the default "connectivity"
    cause -- distinct from the malformed-device-registry and
    visibility-config causes, which route to different (non-network)
    suggestions in tools_service.py."""

    class UnavailableClient(SelectorClient):
        async def send_websocket_message(
            self, message: dict[str, Any]
        ) -> dict[str, Any]:
            if message["type"] == "config/entity_registry/list":
                return {"success": False}
            return await super().send_websocket_message(message)

    client = UnavailableClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )

    with pytest.raises(BulkSelectorInfrastructureError) as exc_info:
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )
    assert exc_info.value.cause == "connectivity"


@pytest.mark.asyncio
async def test_device_registry_entry_missing_id_fails_closed() -> None:
    """A malformed device row must fail closed, not silently drop a device.

    ``visibility.resolver._parse_device_registry`` skips a device entry
    without an ``id`` even under the strict resolver, so a well-formed
    device registry is what keeps a device-derived hidden dimension from
    silently dropping out. This mirrors the per-entry validation
    ``_refresh_hidden_set`` in ``visibility/enforcement.py`` applies before
    calling the same strict resolver.
    """
    client = SelectorClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
        devices=[{"area_id": "salon"}],
    )

    with pytest.raises(
        BulkSelectorInfrastructureError, match="device registry"
    ) as exc_info:
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )
    # "malformed_device_registry", not the connectivity default: this is
    # HA's own registry data being malformed, not a network problem.
    assert exc_info.value.cause == "malformed_device_registry"


@pytest.mark.asyncio
async def test_excluding_an_aggregate_excludes_every_expanded_member() -> None:
    """``exclude_entity_ids`` naming a GROUP, not just a leaf, must expand too.

    A narrowed implementation that subtracts ``set(excluded_roots)`` directly
    (skipping membership expansion on the excluded side) passes every other
    test in this file, since none of them exclude an aggregate -- only
    already-a-leaf entities. This is the case from the PR's own Problem
    section: excluding a group must turn off none of its members.
    """
    client = SelectorClient(
        states=[
            _state("light.spare_group", ["light.member_one", "light.member_two"]),
            _state("light.member_one"),
            _state("light.member_two"),
            _state("light.unrelated"),
        ],
        entities=[
            {"entity_id": "light.spare_group", "area_id": "salon"},
            {"entity_id": "light.member_one", "area_id": None},
            {"entity_id": "light.member_two", "area_id": None},
            {"entity_id": "light.unrelated", "area_id": "salon"},
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {
            "domain": "light",
            "area_ids": ["salon"],
            "exclude_entity_ids": ["light.spare_group"],
        },
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.resolved_entity_ids == ("light.unrelated",)
    assert "light.member_one" not in result.resolved_entity_ids
    assert "light.member_two" not in result.resolved_entity_ids


@pytest.mark.asyncio
async def test_hue_room_group_excludes_one_member_leaf_not_the_group() -> None:
    """Selector-mode regression test for the confirmed live basement-
    exclusion bug (handoff, 2026-08-23): a real Hue Room group
    (``light.couloir_sous_sol_hue``, 5 member lights) with one leaf member
    excluded via ``exclude_entity_ids``.

    Unlike ``test_excluding_an_aggregate_excludes_every_expanded_member``
    (which excludes the GROUP entity itself), this excludes one of the
    group's own LEAF members while still targeting the group's area --
    exactly the "turn off the basement except the staircase light" shape
    that ``operations`` mode could not express safely. Asserts the group
    entity itself never appears in ``resolved_entity_ids`` or as a dispatch
    operation (selector mode only ever dispatches to expanded leaves), and
    that the excluded leaf is the only member missing from the result.
    """
    members = [
        "light.couloir_sous_sol_spot_01",
        "light.couloir_sous_sol_spot_02",
        "light.couloir_sous_sol_spot_03",
        "light.couloir_sous_sol_spot_04",
        "light.couloir_sous_sol_escalier",
    ]
    client = SelectorClient(
        states=[
            _state("light.couloir_sous_sol_hue", members),
            *(_state(member) for member in members),
        ],
        entities=[
            {"entity_id": "light.couloir_sous_sol_hue", "area_id": "sous_sol"},
            *({"entity_id": member, "area_id": None} for member in members),
        ],
        areas=[{"area_id": "sous_sol", "name": "Sous-sol", "floor_id": None}],
    )

    result = await resolve_bulk_selector(
        client,
        {
            "domain": "light",
            "area_ids": ["sous_sol"],
            "exclude_entity_ids": ["light.couloir_sous_sol_escalier"],
        },
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.resolved_entity_ids == (
        "light.couloir_sous_sol_spot_01",
        "light.couloir_sous_sol_spot_02",
        "light.couloir_sous_sol_spot_03",
        "light.couloir_sous_sol_spot_04",
    )
    assert "light.couloir_sous_sol_escalier" not in result.resolved_entity_ids
    assert "light.couloir_sous_sol_hue" not in result.resolved_entity_ids
    assert all(
        row["entity_id"] != "light.couloir_sous_sol_hue" for row in result.operations
    )
    assert result.excluded_entity_ids == ("light.couloir_sous_sol_escalier",)


@pytest.mark.asyncio
async def test_operations_parameters_are_independent_per_row() -> None:
    """Each operation row's `parameters` dict must be its own object.

    `operations` builds every row from the same `_operation_common` dict via
    `**` unpacking, which only shallow-copies -- without a per-row copy of
    the nested `parameters` mapping, every row would share the SAME dict
    object, so mutating one row's parameters (e.g. a dispatcher normalizing
    a value in place) would silently leak into every other row.
    """
    client = SelectorClient(
        states=[_state("light.one"), _state("light.two")],
        entities=[
            {"entity_id": "light.one", "area_id": "salon"},
            {"entity_id": "light.two", "area_id": "salon"},
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "area_ids": ["salon"]},
        action="on",
        parameters={"brightness_pct": 50},
        timeout_seconds=None,
        validate_first=True,
    )

    operations = result.operations
    assert len(operations) == 2
    assert operations[0]["parameters"] is not operations[1]["parameters"]
    operations[0]["parameters"]["brightness_pct"] = 10
    assert operations[1]["parameters"]["brightness_pct"] == 50


@pytest.mark.asyncio
async def test_resolution_parameters_survive_caller_mutating_its_own_dict() -> None:
    """The resolution's OWN stored ``parameters`` must not alias the
    caller's dict. The per-row copy in ``.operations`` protects each
    dispatch row from cross-row mutation, but does nothing about the
    resolution's own copy -- without a copy at the point ``parameters`` is
    stored, a caller that still holds the dict it passed in and mutates it
    after this call returns would silently rewrite the "frozen" resolution's
    payload too, breaking dry_run's preview/dispatch consistency guarantee.
    """
    client = SelectorClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )
    caller_parameters = {"brightness_pct": 50}

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "area_ids": ["salon"]},
        action="on",
        parameters=caller_parameters,
        timeout_seconds=None,
        validate_first=True,
    )
    caller_parameters["brightness_pct"] = 999

    assert result.operations[0]["parameters"]["brightness_pct"] == 50


@pytest.mark.asyncio
async def test_resolution_equality_is_action_sensitive() -> None:
    """Two resolutions over the identical entity set but OPPOSITE actions
    must not compare equal.

    ``_operation_common`` (which holds ``action``) is excluded from
    ``__repr__`` for a real reason (it can carry a lock/alarm code via
    ``parameters``) but must stay IN the dataclass's equality comparison:
    it is not dispatch-irrelevant plumbing despite its name, and
    "on" vs "off" over the same entities is about as different as two
    resolutions can be while still comparing equal on every other field.
    """
    client = SelectorClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )
    selector = {"domain": "light", "area_ids": ["salon"]}
    kwargs = {"parameters": None, "timeout_seconds": None, "validate_first": True}

    on_result = await resolve_bulk_selector(client, selector, action="on", **kwargs)
    off_result = await resolve_bulk_selector(client, selector, action="off", **kwargs)

    assert on_result.resolved_entity_ids == off_result.resolved_entity_ids
    assert on_result != off_result
    assert on_result.operations != off_result.operations


@pytest.mark.asyncio
async def test_resolution_repr_never_contains_a_lock_or_alarm_code() -> None:
    """``repr(resolution)`` must never leak a code carried in ``parameters``
    -- a future dataclass regeneration (e.g. a field reordering that drops
    the explicit ``field(..., repr=False)``) would silently restore it to
    every log line and traceback that prints a resolution unredacted.
    """
    client = SelectorClient(
        states=[_state("lock.one")],
        entities=[{"entity_id": "lock.one", "area_id": "salon"}],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "lock", "area_ids": ["salon"]},
        action="open",
        parameters={"code": "SENTINEL-SECRET-1234"},
        timeout_seconds=None,
        validate_first=True,
    )

    assert "SENTINEL-SECRET-1234" not in repr(result)


@pytest.mark.asyncio
async def test_excluded_aggregate_not_reported_in_expanded_group_ids() -> None:
    """An aggregate referenced only by exclude_entity_ids must not appear in
    expanded_group_ids -- that field reports groups that fed the SELECTION,
    and an excluded group (or its nested sub-groups) never did.

    ``light.excluded_group`` is deliberately NOT assigned to the selected
    area: exclude_entity_ids roots are expanded directly by ID regardless
    of area, but if it WERE in the selected area it would also be a
    legitimate selection-side root in its own right (matching_roots doesn't
    consult exclude_entity_ids), which would confound this test with a
    second, unrelated reason for it to appear in expanded_group_ids.
    """
    client = SelectorClient(
        states=[
            _state("light.selected_group", ["light.a"]),
            _state("light.a"),
            _state("light.excluded_group", ["light.b"]),
            _state("light.b"),
        ],
        entities=[
            {"entity_id": "light.selected_group", "area_id": "salon"},
            {"entity_id": "light.a", "area_id": None},
            {"entity_id": "light.excluded_group", "area_id": None},
            {"entity_id": "light.b", "area_id": None},
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {
            "domain": "light",
            "area_ids": ["salon"],
            "exclude_entity_ids": ["light.excluded_group"],
        },
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.expanded_group_ids == ("light.selected_group",)
    assert "light.excluded_group" not in result.expanded_group_ids


@pytest.mark.asyncio
async def test_action_is_normalized_case_insensitively() -> None:
    """Action normalization accepts surrounding whitespace and mixed case."""
    client = SelectorClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "area_ids": ["salon"]},
        action=" OFF ",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.operations[0]["action"] == "off"


@pytest.mark.asyncio
async def test_selector_entity_limit_fails_closed() -> None:
    """An oversized resolved target set is rejected before dispatch."""
    entity_ids = [f"light.entity_{index}" for index in range(101)]
    client = SelectorClient(
        states=[_state(entity_id) for entity_id in entity_ids],
        entities=[
            {"entity_id": entity_id, "area_id": "salon"} for entity_id in entity_ids
        ],
    )

    with pytest.raises(BulkSelectorValidationError, match="maximum is 100"):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_selector_entity_limit_boundary_allows_exactly_max() -> None:
    """Exactly ``MAX_SELECTOR_ENTITIES`` (100) must dispatch, not fail closed.

    The check is ``len(resolved_entity_ids) > MAX_SELECTOR_ENTITIES``; 101
    (one over) is covered by ``test_selector_entity_limit_fails_closed``
    above, but nothing pinned the boundary itself -- an off-by-one flip to
    ``>=`` would reject exactly 100 and still pass every other test here.
    """
    entity_ids = [f"light.entity_{index}" for index in range(100)]
    client = SelectorClient(
        states=[_state(entity_id) for entity_id in entity_ids],
        entities=[
            {"entity_id": entity_id, "area_id": "salon"} for entity_id in entity_ids
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "area_ids": ["salon"]},
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert len(result.resolved_entity_ids) == 100


@pytest.mark.asyncio
async def test_malformed_registry_response_shape_fails_closed() -> None:
    """``_registry_rows`` has two failure branches: unavailable/no-success
    (covered by ``test_registry_unavailable_defaults_to_connectivity_cause``)
    and a ``success: True`` reply whose ``result`` isn't a list-of-dicts --
    a genuinely malformed shape rather than an absent one. Only the first
    was exercised anywhere in this file.
    """

    class MalformedShapeClient(SelectorClient):
        async def send_websocket_message(
            self, message: dict[str, Any]
        ) -> dict[str, Any]:
            if message["type"] == "config/area_registry/list":
                return {"success": True, "result": "not-a-list"}
            return await super().send_websocket_message(message)

    client = MalformedShapeClient(
        states=[_state("light.one")],
        entities=[{"entity_id": "light.one", "area_id": "salon"}],
    )

    with pytest.raises(
        BulkSelectorInfrastructureError, match="unexpected response"
    ) as exc_info:
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )
    assert exc_info.value.cause == "connectivity"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "message"),
    [
        ({"domain": "light", "area_ids": ["unknown"]}, "unknown area_ids"),
        ({"domain": "light", "floor_ids": ["unknown"]}, "unknown floor_ids"),
        ({"domain": "light"}, "at least one"),
        (
            {"domain": "light", "area_ids": ["salon"], "name": "Salon"},
            "unsupported fields: name",
        ),
    ],
)
async def test_invalid_structural_ids_fail_closed(
    selector: dict[str, Any], message: str
) -> None:
    """Invalid exact topology never degrades to a broader match."""
    client = SelectorClient(states=[_state("light.one")], entities=[])

    with pytest.raises(BulkSelectorValidationError, match=message):
        await resolve_bulk_selector(
            client,
            selector,
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_unknown_area_id_message_points_to_the_lookup_tool() -> None:
    """A caller with only a display name (e.g. ha_search's friendly
    ``area_names``, or a floor's display name) retries with the same wrong
    value forever unless told area_ids/floor_ids are a different, exact
    registry ID and where to find it -- confirmed live: a caller that
    passed display names ("Cave", "Couloir Sous-Sol") as ``area_ids`` hit
    this exact error four times in a row with no path to recovery.
    """
    client = SelectorClient(states=[_state("light.one")], entities=[])

    with pytest.raises(
        BulkSelectorValidationError,
        match=r"exact Home Assistant registry IDs.*ha_list_floors_areas",
    ):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["Couloir Sous-Sol"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )
