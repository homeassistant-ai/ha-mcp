"""Parametrized negative-input tests for ha_get_state (A3 archetype).

Covers three distinct failure paths: plain-text 404, standard JSON 404,
and pre-flight validation before any network I/O.
"""

import pytest

from ..utilities.assertions import safe_call_tool


@pytest.mark.error_handling
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_id,description",
    [
        ("", "empty string — plain-text 404, distinct client parse path"),
        ("sensor.", "malformed format — standard JSON 404"),
        ([], "empty list — pre-flight validation, no network I/O"),
    ],
)
async def test_ha_get_state_invalid_input_returns_error(
    mcp_client, entity_id: str | list, description: str
) -> None:
    """ha_get_state must return success=False for any invalid input.

    Each parametrized case hits a distinct failure path in the tool or client.
    """
    result = await safe_call_tool(
        mcp_client, "ha_get_state", {"entity_id": entity_id}
    )

    assert result["success"] is False, (
        f"Expected success=False for entity_id={entity_id!r} ({description}), "
        f"got: {result}"
    )
    assert "error" in result, (
        f"Expected structured error dict for {entity_id!r}, got: {result}"
    )
