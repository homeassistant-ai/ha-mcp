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

    device_tools.bulk_device_control.assert_awaited_once_with(
        operations=resolution.operations,
        parallel=False,
        ctx=None,
    )
    assert result["resolution"]["excluded_entity_ids"] == ["light.vitrine"]


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
    tools = ServiceTools(MagicMock(), device_tools)
    operations = [{"entity_id": "light.one", "action": "off"}]

    result = await tools.ha_bulk_control(operations, False)

    assert result == {"success": True}
    device_tools.bulk_device_control.assert_awaited_once_with(
        operations=operations,
        parallel=False,
        ctx=None,
    )


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
