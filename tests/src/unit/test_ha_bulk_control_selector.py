"""Public-tool tests for deterministic ha_bulk_control selector mode."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.bulk_selector import (
    MAX_SELECTOR_ENTITIES,
    BulkSelectorInfrastructureError,
    BulkSelectorResolution,
    InfrastructureErrorCause,
)
from ha_mcp.tools.tools_service import ServiceTools


@pytest.fixture
def resolution() -> BulkSelectorResolution:
    """Return one frozen, visibility-safe selector resolution."""
    return BulkSelectorResolution(
        resolved_entity_ids=("light.sofa",),
        excluded_entity_ids=("light.vitrine",),
        selected_area_ids=("salon",),
        expanded_group_ids=("light.salon_group",),
        hidden_entity_count=0,
        _operation_common={"action": "off", "validate_first": True},
    )


@pytest.mark.asyncio
async def test_selector_dry_run_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
    resolution: BulkSelectorResolution,
) -> None:
    """Dry-run returns the frozen plan without calling bulk control."""
    resolver = AsyncMock(return_value=resolution)
    monkeypatch.setattr("ha_mcp.tools.tools_service.resolve_bulk_selector", resolver)
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock()
    tools = ServiceTools(MagicMock(), device_tools)

    result = await tools.ha_bulk_control(
        selector={
            "domain": "light",
            "area_ids": ["salon"],
            "exclude_entity_ids": ["light.vitrine"],
        },
        action="off",
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["dispatched"] is False
    assert result["resolution"]["resolved_entity_ids"] == ["light.sofa"]
    device_tools.bulk_device_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_surfaces_resolution_warnings_at_top_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per AGENTS.md, `warnings` must be top-level, never nested under
    `resolution` -- a consumer that only reads the top-level key must still
    see a hidden-entity degradation."""
    resolution_with_warning = BulkSelectorResolution(
        resolved_entity_ids=("light.sofa",),
        excluded_entity_ids=(),
        selected_area_ids=("salon",),
        expanded_group_ids=(),
        hidden_entity_count=1,
        warnings=("1 matching entity was hidden by the entity visibility filter.",),
        _operation_common={"action": "off", "validate_first": True},
    )
    monkeypatch.setattr(
        "ha_mcp.tools.tools_service.resolve_bulk_selector",
        AsyncMock(return_value=resolution_with_warning),
    )
    tools = ServiceTools(MagicMock(), MagicMock())

    result = await tools.ha_bulk_control(
        selector={"domain": "light", "area_ids": ["salon"]},
        action="off",
        dry_run=True,
    )

    assert result["warnings"] == [
        "1 matching entity was hidden by the entity visibility filter."
    ]
    assert "warnings" not in result["resolution"]


@pytest.mark.asyncio
async def test_dispatch_merges_resolution_warnings_with_dispatch_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatch-time warnings from bulk_device_control (per-operation
    degradations) must be extended with, not overwritten by, the
    resolution's own warnings -- both belong in the same top-level list."""
    resolution_with_warning = BulkSelectorResolution(
        resolved_entity_ids=("light.sofa",),
        excluded_entity_ids=(),
        selected_area_ids=("salon",),
        expanded_group_ids=(),
        hidden_entity_count=1,
        warnings=("1 matching entity was hidden by the entity visibility filter.",),
        _operation_common={"action": "off", "validate_first": True},
    )
    monkeypatch.setattr(
        "ha_mcp.tools.tools_service.resolve_bulk_selector",
        AsyncMock(return_value=resolution_with_warning),
    )
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(
        return_value={
            "success": True,
            "successful": 1,
            "failed": 0,
            "warnings": ["light.sofa took longer than expected to confirm"],
        }
    )
    tools = ServiceTools(MagicMock(), device_tools)

    result = await tools.ha_bulk_control(
        selector={"domain": "light", "area_ids": ["salon"]},
        action="off",
    )

    assert result["warnings"] == [
        "light.sofa took longer than expected to confirm",
        "1 matching entity was hidden by the entity visibility filter.",
    ]
    assert "warnings" not in result["resolution"]


@pytest.mark.asyncio
async def test_selector_dispatches_only_frozen_leaf_operations(
    monkeypatch: pytest.MonkeyPatch,
    resolution: BulkSelectorResolution,
) -> None:
    """Execution delegates exact leaves to the existing verified bulk path."""
    monkeypatch.setattr(
        "ha_mcp.tools.tools_service.resolve_bulk_selector",
        AsyncMock(return_value=resolution),
    )
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(
        return_value={"success": True, "successful": 1, "failed": 0}
    )
    tools = ServiceTools(MagicMock(), device_tools)

    result = await tools.ha_bulk_control(
        selector={"domain": "light", "area_ids": ["salon"]},
        action="off",
        parallel=False,
    )

    # A literal, hand-built expected payload -- not `resolution.operations`
    # called again -- so this independently pins the actual row shape.
    # Comparing against a second call to the same property would still pass
    # even if `.operations` itself computed the wrong shape, since both
    # sides of the assertion would share the identical bug.
    device_tools.bulk_device_control.assert_awaited_once_with(
        operations=[
            {"entity_id": "light.sofa", "action": "off", "validate_first": True}
        ],
        parallel=False,
        ctx=None,
    )
    assert result["resolution"]["excluded_entity_ids"] == ["light.vitrine"]


@pytest.mark.asyncio
async def test_dispatch_tool_error_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    resolution: BulkSelectorResolution,
) -> None:
    """A ToolError raised by bulk_device_control itself (e.g. "every
    operation failed validation") must pass through unchanged, not get
    re-classified by exception_to_structured_error into a generic error
    that discards its real code/message/suggestions.
    """
    original = ToolError(
        '{"success": false, "error": {"code": "VALIDATION_FAILED", '
        '"message": "All operations failed validation"}}'
    )
    monkeypatch.setattr(
        "ha_mcp.tools.tools_service.resolve_bulk_selector",
        AsyncMock(return_value=resolution),
    )
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(side_effect=original)
    tools = ServiceTools(MagicMock(), device_tools)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_bulk_control(
            selector={"domain": "light", "area_ids": ["salon"]},
            action="off",
        )

    # `is`, not a text comparison: the code path under test is a bare
    # `except ToolError: raise`, which re-raises the SAME object -- `is`
    # pins that exact contract, where a text-equal-but-reconstructed
    # ToolError would also satisfy a str() comparison without proving the
    # guard actually took the re-raise branch instead of rebuilding an
    # equivalent one some other way.
    assert exc_info.value is original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {
            "operations": [{"entity_id": "light.one", "action": "off"}],
            "selector": {"domain": "light", "area_ids": ["salon"]},
            "action": "off",
        },
    ],
)
async def test_exactly_one_target_mode_is_required(arguments: dict) -> None:
    """Ambiguous or absent targeting fails before any dispatch."""
    tools = ServiceTools(MagicMock(), MagicMock())

    with pytest.raises(ToolError, match="exactly one"):
        await tools.ha_bulk_control(**arguments)


@pytest.mark.asyncio
async def test_existing_operations_call_shape_remains_supported() -> None:
    """Legacy operations and positional parallel arguments remain unchanged."""
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(return_value=[])
    tools = ServiceTools(client, device_tools)
    operations = [{"entity_id": "light.one", "action": "off"}]

    result = await tools.ha_bulk_control(operations, False)

    assert result == {"success": True}
    device_tools.bulk_device_control.assert_awaited_once_with(
        operations=operations,
        parallel=False,
        ctx=None,
    )


@pytest.mark.asyncio
async def test_operations_mode_single_entity_skips_states_fetch() -> None:
    """A single distinct entity_id can never contain a group/member
    conflict -- there's no second row to overlap with -- so the
    group-safety check must not fetch states for it at all. The
    overwhelming majority of operations-mode calls target one entity (or
    several rows for the SAME entity) and never touch a group, and
    shouldn't pay for a states round-trip this check can never use.
    """
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(return_value=[])
    tools = ServiceTools(client, device_tools)

    result = await tools.ha_bulk_control(
        [{"entity_id": "light.one", "action": "off"}], False
    )

    assert result == {"success": True}
    client.get_states.assert_not_awaited()


def _light_state(entity_id: str, members: list[str] | None = None) -> dict:
    """A minimal HA state dict; ``members`` sets the group-membership
    attribute Home Assistant (and ha_mcp.utils.entity_membership) reads as
    ``entity_id`` -- the same shape a Hue Room/Zone group exposes."""
    attributes: dict = {"friendly_name": entity_id}
    if members is not None:
        attributes["entity_id"] = members
    return {"entity_id": entity_id, "state": "on", "attributes": attributes}


@pytest.mark.asyncio
async def test_operations_mode_rejects_group_and_member_in_same_batch() -> None:
    """Confirmed live (2026-08-23): a Hue Room group entity plus 4 of its 5
    members, with the 5th deliberately omitted to exclude it, still turned
    the 5th member off too -- purely from the group row's own cascade.
    Operations mode has no ``exclude_entity_ids`` to express that intent,
    so the omission alone never protected it; the only safe response is to
    reject the whole batch rather than silently dispatch it.

    Also confirmed live: the rejection message alone was not enough. The
    calling model retried four times with a broken selector call --
    ``exclude_entity_ids`` at the top level (it belongs inside ``selector``)
    and area *display names* where exact ``area_id`` registry values were
    required -- and never recovered. The message must show a concrete,
    correctly-shaped example, not just name selector mode.
    """
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(
        return_value=[
            _light_state(
                "light.couloir_sous_sol_hue",
                [
                    "light.couloir_sous_sol_spot_01",
                    "light.couloir_sous_sol_spot_02",
                    "light.couloir_sous_sol_spot_03",
                    "light.couloir_sous_sol_spot_04",
                    "light.couloir_sous_sol_escalier",
                ],
            ),
            _light_state("light.couloir_sous_sol_spot_01"),
            _light_state("light.couloir_sous_sol_spot_02"),
            _light_state("light.couloir_sous_sol_spot_03"),
            _light_state("light.couloir_sous_sol_spot_04"),
            _light_state("light.couloir_sous_sol_escalier"),
        ]
    )
    tools = ServiceTools(client, device_tools)
    operations = [
        {"entity_id": "light.couloir_sous_sol_hue", "action": "off"},
        {"entity_id": "light.couloir_sous_sol_spot_01", "action": "off"},
        {"entity_id": "light.couloir_sous_sol_spot_02", "action": "off"},
        {"entity_id": "light.couloir_sous_sol_spot_03", "action": "off"},
        {"entity_id": "light.couloir_sous_sol_spot_04", "action": "off"},
        # light.couloir_sous_sol_escalier deliberately omitted here -- the
        # caller's (unenforceable, in this mode) attempt to exclude it.
    ]

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_bulk_control(operations, False)

    message = json.loads(str(exc_info.value))["error"]["message"]
    assert "group/aggregate entity" in message
    # The worked example must show exclude_entity_ids nested INSIDE
    # selector -- not as a sibling argument, which is the exact mistake
    # observed live.
    assert '"selector": {"domain": "light"' in message
    assert '"exclude_entity_ids"' in message
    assert "ha_list_floors_areas" in message
    device_tools.bulk_device_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_operations_mode_allows_group_targeted_alone() -> None:
    """Targeting only the group entity, with none of its members separately
    listed, is unambiguous (equivalent to controlling the group as a whole)
    and must still dispatch normally."""
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(
        return_value=[_light_state("light.group", ["light.a", "light.b"])]
    )
    tools = ServiceTools(client, device_tools)
    operations = [{"entity_id": "light.group", "action": "off"}]

    result = await tools.ha_bulk_control(operations, False)

    assert result == {"success": True}
    device_tools.bulk_device_control.assert_awaited_once_with(
        operations=operations, parallel=False, ctx=None
    )


@pytest.mark.asyncio
async def test_operations_mode_allows_members_targeted_alone() -> None:
    """Targeting only specific members, with the group entity itself never
    listed, is exactly the safe pattern this check exists to steer callers
    toward -- must not be flagged."""
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(
        return_value=[
            _light_state("light.group", ["light.a", "light.b", "light.c"]),
            _light_state("light.a"),
            _light_state("light.b"),
        ]
    )
    tools = ServiceTools(client, device_tools)
    operations = [
        {"entity_id": "light.a", "action": "off"},
        {"entity_id": "light.b", "action": "off"},
    ]

    result = await tools.ha_bulk_control(operations, False)

    assert result == {"success": True}
    device_tools.bulk_device_control.assert_awaited_once_with(
        operations=operations, parallel=False, ctx=None
    )


@pytest.mark.asyncio
async def test_operations_mode_allows_scene_alongside_its_configured_entities() -> None:
    """A scene targeted together with an entity IT CONFIGURES must NOT be
    flagged -- HA core's scene platform sets a scene's ``entity_id``
    attribute to the entities it configures (not entities it is
    structurally composed of), which is the identical shape a real
    aggregate's member list has. Without excluding scenes here the same
    way ``bulk_selector._NON_AGGREGATE_ROOT_DOMAINS`` does, this would be
    flagged as a group conflict whose only offered remedy (selector mode's
    ``exclude_entity_ids``) selector mode itself refuses for scenes --
    sending the caller to a fix that doesn't exist for the case that
    triggered the error.
    """
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(
        return_value=[
            _light_state("scene.movie_night", ["light.living_room"]),
            _light_state("light.living_room"),
        ]
    )
    tools = ServiceTools(client, device_tools)
    operations = [
        {"entity_id": "scene.movie_night", "action": "on"},
        {"entity_id": "light.living_room", "action": "off"},
    ]

    result = await tools.ha_bulk_control(operations, False)

    assert result == {"success": True}
    device_tools.bulk_device_control.assert_awaited_once_with(
        operations=operations, parallel=False, ctx=None
    )


@pytest.mark.asyncio
async def test_operations_mode_rejects_opposing_action_group_member_conflict() -> None:
    """A conflict is flagged regardless of whether the group and member rows
    request the SAME action or opposing ones. The group's own fan-out races
    the member's explicit row either way, so an opposing action (e.g. group
    "on" alongside an explicit member "off") is not a safe override -- it's
    just a differently-shaped version of the same race.
    """
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(
        return_value=[
            _light_state("light.group", ["light.a", "light.b"]),
            _light_state("light.a"),
            _light_state("light.b"),
        ]
    )
    tools = ServiceTools(client, device_tools)
    operations = [
        {"entity_id": "light.group", "action": "on"},
        {"entity_id": "light.a", "action": "off"},
    ]

    with pytest.raises(ToolError, match="group/aggregate entity"):
        await tools.ha_bulk_control(operations, False)

    device_tools.bulk_device_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_operations_mode_rejects_nested_group_conflict() -> None:
    """A conflict must be caught through a NESTED aggregate, not just a
    direct member -- an outer Zone group containing an inner Room group,
    with the batch targeting the outer group and a leaf that only belongs
    to the inner one. Checking only ``light.outer``'s own direct
    ``entity_id`` attribute (``["light.inner"]``) would miss this: the
    outer group's own service call still cascades through the inner group
    down to the leaf, exactly as unsafe as targeting the inner group and
    that leaf directly.
    """
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(
        return_value=[
            _light_state("light.outer", ["light.inner"]),
            _light_state("light.inner", ["light.leaf_a", "light.leaf_b"]),
            _light_state("light.leaf_a"),
            _light_state("light.leaf_b"),
        ]
    )
    tools = ServiceTools(client, device_tools)
    operations = [
        {"entity_id": "light.outer", "action": "off"},
        {"entity_id": "light.leaf_a", "action": "off"},
    ]

    with pytest.raises(ToolError, match="group/aggregate entity"):
        await tools.ha_bulk_control(operations, False)

    device_tools.bulk_device_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_operations_mode_allows_unrelated_nested_groups() -> None:
    """Two independent group hierarchies with no shared membership must
    not cross-contaminate -- the transitive walk for one group must not
    leak into flagging an entity that only belongs to a completely
    different group's tree.
    """
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(
        return_value=[
            _light_state("light.outer_a", ["light.inner_a"]),
            _light_state("light.inner_a", ["light.leaf_a"]),
            _light_state("light.leaf_a"),
            _light_state("light.outer_b", ["light.inner_b"]),
            _light_state("light.inner_b", ["light.leaf_b"]),
            _light_state("light.leaf_b"),
        ]
    )
    tools = ServiceTools(client, device_tools)
    operations = [
        {"entity_id": "light.outer_a", "action": "off"},
        {"entity_id": "light.leaf_b", "action": "off"},
    ]

    result = await tools.ha_bulk_control(operations, False)

    assert result == {"success": True}
    device_tools.bulk_device_control.assert_awaited_once_with(
        operations=operations, parallel=False, ctx=None
    )


@pytest.mark.asyncio
async def test_operations_mode_cyclic_membership_does_not_hang() -> None:
    """A membership cycle (A lists B as a member, B lists A back) must not
    hang or crash the group-safety check -- unlike ``bulk_selector``'s own
    selector-mode expansion (which is resolving an authoritative dispatch
    set and is right to raise on a cycle), this is a best-effort safety
    scan over one batch and should degrade gracefully instead.
    """
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(
        return_value=[
            _light_state("light.a", ["light.b"]),
            _light_state("light.b", ["light.a"]),
            _light_state("light.c"),
        ]
    )
    tools = ServiceTools(client, device_tools)
    operations = [
        {"entity_id": "light.a", "action": "off"},
        {"entity_id": "light.c", "action": "off"},
    ]

    result = await tools.ha_bulk_control(operations, False)

    assert result == {"success": True}
    device_tools.bulk_device_control.assert_awaited_once_with(
        operations=operations, parallel=False, ctx=None
    )


@pytest.mark.asyncio
async def test_operations_mode_group_safety_check_fails_closed_on_states_error() -> (
    None
):
    """A states-fetch failure during the group-safety check must not
    silently skip the check and dispatch anyway: an unverifiable batch is
    not a verified-safe one, matching the fail-closed stance
    bulk_selector.py takes for the analogous selector-mode read.

    Two distinct entities: a single-entity batch short-circuits before the
    states fetch (see test_operations_mode_single_entity_skips_states_fetch),
    so it would never reach this code path.
    """
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(side_effect=RuntimeError("websocket dropped"))
    tools = ServiceTools(client, device_tools)

    with pytest.raises(ToolError):
        await tools.ha_bulk_control(
            [
                {"entity_id": "light.one", "action": "off"},
                {"entity_id": "light.two", "action": "off"},
            ],
            False,
        )

    device_tools.bulk_device_control.assert_not_awaited()

    # The malformed-response-shape counterpart to this test used to live
    # here, mocking client.get_states() to return a non-list. Removed: the
    # real client now enforces list-or-raise at the source
    # (HomeAssistantClient.get_states, see tests/src/unit/test_rest_client_states.py)
    # instead of this call site defending against a shape production
    # cannot actually produce.


@pytest.mark.asyncio
async def test_operations_mode_group_safety_check_propagates_tool_error_unchanged() -> (
    None
):
    """A ToolError raised by ``client.get_states()`` itself must pass
    through unchanged, not get re-classified by
    ``exception_to_structured_error`` into a generic error that discards
    its real code/message/suggestions -- the same guard (and the same
    regression this pins) that ``_run_bulk_selector``'s dispatch block
    needs for the analogous ``ToolError``-from-``bulk_device_control`` case
    (see ``test_dispatch_tool_error_propagates_unchanged``).
    """
    original = ToolError(
        '{"success": false, "error": {"code": "AUTH_EXPIRED", '
        '"message": "The access token has expired"}}'
    )
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(side_effect=original)
    tools = ServiceTools(client, device_tools)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_bulk_control(
            [
                {"entity_id": "light.one", "action": "off"},
                {"entity_id": "light.two", "action": "off"},
            ],
            False,
        )

    # `is`, not a text comparison -- see the identical note on the
    # selector-mode sibling of this test (test_dispatch_tool_error_propagates_unchanged).
    assert exc_info.value is original
    device_tools.bulk_device_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_operations_mode_rejects_dry_run() -> None:
    """Selector-only dry-run cannot silently alter legacy operations mode.

    The rejection must name the actual offending parameter (dry_run), not a
    generic collapsed message — five distinct selector-only-parameter
    mistakes (action, parameters, timeout_seconds, validate_first, dry_run)
    used to share one message that always blamed "selector", the one
    parameter the caller demonstrably did not pass.
    """
    tools = ServiceTools(MagicMock(), MagicMock())

    with pytest.raises(ToolError, match="'dry_run' is a selector-only parameter"):
        await tools.ha_bulk_control(
            operations=[{"entity_id": "light.one", "action": "off"}],
            dry_run=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "offender"),
    [
        ({"action": "off"}, "action"),
        ({"parameters": {"brightness_pct": 30}}, "parameters"),
        ({"timeout_seconds": 5.0}, "timeout_seconds"),
        ({"validate_first": False}, "validate_first"),
    ],
)
async def test_operations_mode_rejects_every_selector_only_parameter(
    kwargs: dict[str, object], offender: str
) -> None:
    """Every selector-only parameter must name itself as the offender, not
    just ``dry_run`` (see ``test_operations_mode_rejects_dry_run``). These
    five used to share one message that always blamed "selector" -- the one
    parameter the caller demonstrably did not pass, since this branch is
    only reached when ``selector is None``.

    action/parameters/timeout_seconds/validate_first are all declared
    ``BulkControlOperation`` row fields, so the message must send the
    caller to move the value onto each row -- telling them to delete it
    (the old, still-correct-for-``dry_run`` remedy) discards intent that
    was almost certainly real: ``operations=[...], parameters={...}``
    means "apply these to my operations", not "forget I said this".
    """
    tools = ServiceTools(MagicMock(), MagicMock())

    with pytest.raises(
        ToolError,
        match=f"'{offender}' is a per-operation field in operations mode",
    ):
        await tools.ha_bulk_control(
            operations=[{"entity_id": "light.one", "action": "off"}],
            **kwargs,
        )


@pytest.mark.asyncio
async def test_selector_mode_requires_action() -> None:
    """Selector mode with no ``action`` must fail before any registry read,
    naming ``action`` as the missing parameter -- not fall through to
    ``resolve_bulk_selector`` with ``action=None``, which would instead
    surface a confusing "Invalid action ''" message from deep inside
    selector validation.
    """
    tools = ServiceTools(MagicMock(), MagicMock())

    with pytest.raises(ToolError, match="Selector mode requires action"):
        await tools.ha_bulk_control(
            selector={"domain": "light", "area_ids": ["salon"]},
        )


def test_docstring_entity_limit_matches_the_constant() -> None:
    """The tool docstring hardcodes the entity-limit number in prose (a
    docstring can't be an f-string -- Python only recognizes a plain
    string literal immediately after ``def`` as ``__doc__``, so
    interpolating ``MAX_SELECTOR_ENTITIES`` there would silently make the
    tool's real description ``None``), so nothing at import time keeps the
    two in sync if ``MAX_SELECTOR_ENTITIES`` ever changes. This test is
    that sync check instead.
    """
    assert ServiceTools.ha_bulk_control.__doc__ is not None
    assert (
        f"more than {MAX_SELECTOR_ENTITIES} entities"
        in ServiceTools.ha_bulk_control.__doc__
    )


@pytest.mark.asyncio
async def test_selector_transport_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real HA transport exception from resolution becomes a structured error.

    Raises the actual client exception type transport failures use
    (``HomeAssistantConnectionError``), not a generic ``RuntimeError`` with a
    message crafted to hit the classifier's message-substring fallback —
    that would only prove the fallback heuristic works, not that this code
    path classifies a genuine transport failure correctly.
    """
    monkeypatch.setattr(
        "ha_mcp.tools.tools_service.resolve_bulk_selector",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection refused")),
    )
    tools = ServiceTools(MagicMock(), MagicMock())

    with pytest.raises(ToolError, match="CONNECTION_FAILED"):
        await tools.ha_bulk_control(
            selector={"domain": "light", "area_ids": ["salon"]},
            action="off",
        )


@pytest.mark.asyncio
async def test_selector_infrastructure_failure_is_connection_error_not_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Home Assistant infrastructure failure must not blame the selector.

    ``BulkSelectorInfrastructureError`` (unavailable/malformed HA registry or
    visibility data) is not the caller's fault and is not fixable by editing
    the selector, so it must route through the connection-error path
    (CONNECTION_FAILED) rather than VALIDATION_FAILED — otherwise an agent
    reads "selector is invalid", rewrites a selector that was fine, and
    retries against an HA outage no selector change can fix.
    """
    monkeypatch.setattr(
        "ha_mcp.tools.tools_service.resolve_bulk_selector",
        AsyncMock(
            side_effect=BulkSelectorInfrastructureError(
                "Home Assistant entity registry is unavailable"
            )
        ),
    )
    tools = ServiceTools(MagicMock(), MagicMock())

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_bulk_control(
            selector={"domain": "light", "area_ids": ["salon"]},
            action="off",
        )

    body = str(exc_info.value)
    assert "CONNECTION_FAILED" in body
    assert "VALIDATION_FAILED" not in body


@pytest.mark.asyncio
async def test_infrastructure_error_suggestions_match_the_actual_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONNECTION_FAILED's default suggestions (check HA is running / verify
    HOMEASSISTANT_URL / check network) are actively unhelpful for a
    malformed local device-registry row or a corrupt local visibility
    config -- neither is a network problem. The routed suggestions must
    reflect the actual cause, not the connectivity boilerplate.
    """
    monkeypatch.setattr(
        "ha_mcp.tools.tools_service.resolve_bulk_selector",
        AsyncMock(
            side_effect=BulkSelectorInfrastructureError(
                "Home Assistant device registry returned a malformed entry",
                cause=InfrastructureErrorCause.MALFORMED_DEVICE_REGISTRY,
            )
        ),
    )
    tools = ServiceTools(MagicMock(), MagicMock())

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_bulk_control(
            selector={"domain": "light", "area_ids": ["salon"]},
            action="off",
        )

    body = str(exc_info.value)
    assert "device registry" in body.lower()
    assert "HOMEASSISTANT_URL" not in body
    assert "network connectivity" not in body.lower()


@pytest.mark.asyncio
async def test_visibility_config_cause_routes_to_its_own_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The THIRD cause, not just malformed_device_registry: a corrupt or
    unloadable entity_visibility.json is also not a network problem, and
    must route to the settings-UI-focused suggestion, not the connectivity
    boilerplate.
    """
    monkeypatch.setattr(
        "ha_mcp.tools.tools_service.resolve_bulk_selector",
        AsyncMock(
            side_effect=BulkSelectorInfrastructureError(
                "Entity visibility could not be resolved safely: config "
                "could not be loaded",
                cause=InfrastructureErrorCause.VISIBILITY_CONFIG,
            )
        ),
    )
    tools = ServiceTools(MagicMock(), MagicMock())

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_bulk_control(
            selector={"domain": "light", "area_ids": ["salon"]},
            action="off",
        )

    body = str(exc_info.value)
    assert "entity visibility" in body.lower()
    assert "HOMEASSISTANT_URL" not in body
    assert "network connectivity" not in body.lower()


def test_infrastructure_error_suggestions_cover_every_cause() -> None:
    """A parity test over the closed cause set, not another parametrized
    string case: the previous test above constructs
    ``cause=InfrastructureErrorCause.MALFORMED_DEVICE_REGISTRY`` itself,
    so it only ever proves the table against a value the test supplies --
    never catches the table itself silently falling out of sync with the
    enum (a member added to ``InfrastructureErrorCause`` with no matching
    ``_INFRASTRUCTURE_ERROR_SUGGESTIONS`` entry would fall back to the
    connectivity boilerplate at runtime with no test noticing). This
    fails the moment the two drift, regardless of which cause a future
    raise site happens to use.
    """
    from ha_mcp.tools.tools_service import _INFRASTRUCTURE_ERROR_SUGGESTIONS

    assert set(_INFRASTRUCTURE_ERROR_SUGGESTIONS) == set(InfrastructureErrorCause)
