"""Regression tests for deterministic structural bulk selection."""

from __future__ import annotations

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
    monkeypatch.setattr(
        bulk_selector,
        "load_hidden_set",
        AsyncMock(return_value=({"light.hidden"}, [])),
    )
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
async def test_all_matches_excluded_names_exclusion_not_area() -> None:
    """Candidates existed in-area but all were excluded: a distinct message
    from "domain not in area" above -- this is the caller's exclusion (or
    visibility) eating the whole result, not a wrong area/domain."""
    client = SelectorClient(
        states=[_state("light.only")],
        entities=[{"entity_id": "light.only", "area_id": "salon"}],
    )

    with pytest.raises(BulkSelectorValidationError, match="excluded or hidden"):
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

    with pytest.raises(BulkSelectorInfrastructureError, match="device registry"):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


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
