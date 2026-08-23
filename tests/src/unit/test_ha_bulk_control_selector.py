"""Public-tool tests for deterministic ha_bulk_control selector mode."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.bulk_selector import (
    BulkSelectorInfrastructureError,
    BulkSelectorResolution,
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

    assert str(exc_info.value) == str(original)


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

    with pytest.raises(ToolError, match="group/aggregate entity"):
        await tools.ha_bulk_control(operations, False)

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
async def test_operations_mode_group_safety_check_fails_closed_on_states_error() -> (
    None
):
    """A states-fetch failure during the group-safety check must not
    silently skip the check and dispatch anyway: an unverifiable batch is
    not a verified-safe one, matching the fail-closed stance
    bulk_selector.py takes for the analogous selector-mode read."""
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(side_effect=RuntimeError("websocket dropped"))
    tools = ServiceTools(client, device_tools)

    with pytest.raises(ToolError):
        await tools.ha_bulk_control(
            [{"entity_id": "light.one", "action": "off"}], False
        )

    device_tools.bulk_device_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_operations_mode_group_safety_check_fails_closed_on_malformed_states() -> (
    None
):
    """A non-list states response is just as unverifiable as a transport
    failure and must fail closed the same way, not be treated as "no
    states, so no conflicts possible"."""
    device_tools = MagicMock()
    device_tools.bulk_device_control = AsyncMock(return_value={"success": True})
    client = MagicMock()
    client.get_states = AsyncMock(return_value={"success": False})
    tools = ServiceTools(client, device_tools)

    with pytest.raises(ToolError, match="Home Assistant"):
        await tools.ha_bulk_control(
            [{"entity_id": "light.one", "action": "off"}], False
        )

    device_tools.bulk_device_control.assert_not_awaited()


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
            [{"entity_id": "light.one", "action": "off"}], False
        )

    assert str(exc_info.value) == str(original)
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
    """
    tools = ServiceTools(MagicMock(), MagicMock())

    with pytest.raises(ToolError, match=f"'{offender}' is a selector-only parameter"):
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
                cause="malformed_device_registry",
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
