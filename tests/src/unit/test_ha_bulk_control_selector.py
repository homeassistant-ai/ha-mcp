"""Public-tool tests for deterministic ha_bulk_control selector mode."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.bulk_selector import BulkSelectorResolution
from ha_mcp.tools.tools_service import ServiceTools


@pytest.fixture
def resolution() -> BulkSelectorResolution:
    """Return one frozen, visibility-safe selector resolution."""
    return BulkSelectorResolution(
        operations=[
            {
                "entity_id": "light.sofa",
                "action": "off",
                "validate_first": True,
            }
        ],
        resolved_entity_ids=["light.sofa"],
        excluded_entity_ids=["light.vitrine"],
        selected_area_ids=["salon"],
        expanded_group_ids=["light.salon_group"],
        hidden_entity_count=0,
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
