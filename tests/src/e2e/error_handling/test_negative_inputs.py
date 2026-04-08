"""
Parametrized negative-input tests for A3 (required entity_id) tools.

These tests assert that malformed, empty, or non-existent entity inputs
produce structured error responses rather than silent false-successes.

Background: Discussion #914 identified a gap in the existing E2E suite.
test_network_errors.py covers similar scenarios but with soft assertions
(logging only). This file adds hard-asserting @pytest.mark.parametrize
tests so CI catches regressions automatically.

Design notes:
- Only read-only / no-side-effect tools — no cleanup needed.
- All calls go through safe_call_tool() which normalises ToolError
  exceptions into dicts with success=False, keeping assertions uniform.
- Fixture: mcp_client (session-scoped, Docker HA container via conftest).
- asyncio_mode = auto is set project-wide; @pytest.mark.asyncio is
  therefore redundant here but kept for readability and grep-ability.

Scope: A3 archetype only (ha_get_state, ha_get_entity).
       A4 and higher archetypes are left for follow-up PRs.
"""

import logging

import pytest

from ..utilities.assertions import safe_call_tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ha_get_state — A3 archetype: required entity_id (str or list[str])
# ---------------------------------------------------------------------------

@pytest.mark.error_handling
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_id,description",
    [
        (
            "sensor.",
            "malformed: missing object_id after dot",
        ),
        (
            "light.xyz_nonexistent_e2e_abc_123",
            "valid format but entity does not exist in HA",
        ),
        (
            [],
            "empty list — ToolError raised pre-flight, no network I/O",
        ),
    ],
)
async def test_ha_get_state_invalid_input_returns_error(
    mcp_client, entity_id: str | list, description: str
) -> None:
    """ha_get_state with invalid input must return success=False.

    Failure paths differ by input type:

    String inputs ("sensor.", "light.xyz_nonexistent_e2e_abc_123"):
      Reach the HA REST endpoint (GET /api/states/{entity_id}) → HTTP 404 →
      HomeAssistantAPIError(404) → exception_to_structured_error →
      raise_tool_error() → ToolError → safe_call_tool() → success=False.
      Evidence (live-verified against HA 2026.4.1):
      - GET /api/states/sensor.       → HTTP 404 "Entity not found."
      - GET /api/states/light.xyz_... → HTTP 404 "Entity not found."

    Empty list []:
      Pre-flight validation in the multi-entity path raises ToolError
      before any network I/O (source: tools_search.py line 891):
        if not isinstance(entity_ids, list) or not entity_ids:
            raise_tool_error(create_validation_error(...))
    """
    logger.info(
        "Testing ha_get_state invalid input: %r (%s)", entity_id, description
    )

    result = await safe_call_tool(
        mcp_client, "ha_get_state", {"entity_id": entity_id}
    )

    assert result.get("success") is False, (
        f"Expected success=False for entity_id={entity_id!r} ({description}), "
        f"got: {result}"
    )
    assert "error" in result, (
        f"Expected structured error dict for {entity_id!r}, got: {result}"
    )
    logger.info(
        "✅ ha_get_state(%r) correctly returned success=False", entity_id
    )


# ---------------------------------------------------------------------------
# ha_get_entity — A3 archetype: entity registry lookup (str or list[str])
# ---------------------------------------------------------------------------

@pytest.mark.error_handling
@pytest.mark.asyncio
async def test_ha_get_entity_nonexistent_returns_error(mcp_client) -> None:
    """ha_get_entity with a valid-format but non-existent entity must return success=False.

    The HA WebSocket command (config/entity_registry/get) returns
    success=false for entities not in the registry. The MCP layer converts
    that to a ValueError, which is caught and re-raised as a ToolError.

    Evidence:
    - Source trace (tools_entities.py, _fetch_entity):
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": eid}
        )
        if not result.get("success"):          # ← HA returns success=false
            raise ValueError(error_msg)        # ← MCP converts to ToolError
    - The ha_remove_entity unit tests (test_tools_entities.py) include a
      mock of send_websocket_message returning {success: False, error: "..."}
      for nonexistent entities, confirming the WebSocket error shape.
      Note: ha_remove_entity uses config/entity_registry/remove, not /get;
      the mock is cited only to show the general HA WebSocket error pattern,
      not as direct evidence for /get specifically.
    - No direct WebSocket inspection was possible (Nabu Casa proxy blocks
      external WebSocket from this environment). This is the one test with
      reduced confidence (~85%); if HA behaviour differs, CI will surface it.
    """
    entity_id = "sensor.xyz_nonexistent_e2e_abc_123"
    logger.info("Testing ha_get_entity with nonexistent entity: %s", entity_id)

    result = await safe_call_tool(
        mcp_client, "ha_get_entity", {"entity_id": entity_id}
    )

    assert result.get("success") is False, (
        f"Expected success=False for nonexistent entity {entity_id!r}, "
        f"got: {result}"
    )
    assert "error" in result, (
        f"Expected structured error dict for {entity_id!r}, got: {result}"
    )
    logger.info(
        "✅ ha_get_entity(%r) correctly returned success=False", entity_id
    )


@pytest.mark.error_handling
@pytest.mark.asyncio
async def test_ha_get_entity_empty_list_returns_empty_success(mcp_client) -> None:
    """ha_get_entity([]) returns success=True with an empty entity list.

    This is intentional by design: an empty input list is treated as a
    no-op and returns an empty result rather than an error. This test
    documents the behaviour and the asymmetry with ha_get_state([]),
    which raises a ToolError for the same empty-list input.

    Evidence:
    - Source (tools_entities.py lines 953-959):
        elif isinstance(entity_id, list):
            if not entity_id:
                return {
                    "success": True,
                    "entity_entries": [],
                    "count": 0,
                    "message": "No entities requested",
                }
    - parse_mcp_result() does a plain json.loads() with no added fields,
      so result == expected_result is a safe exact-equality check here.
    """
    logger.info(
        "Testing ha_get_entity([]) — expects success=True, count=0"
    )

    result = await safe_call_tool(
        mcp_client, "ha_get_entity", {"entity_id": []}
    )

    expected_result = {
        "success": True,
        "entity_entries": [],
        "count": 0,
        "message": "No entities requested",
    }
    assert result == expected_result, (
        f"Expected {expected_result} for ha_get_entity([]), got: {result}"
    )
    logger.info(
        "✅ ha_get_entity([]) → success=True, count=0  "
        "(contrast: ha_get_state([]) raises ToolError — by design)"
    )
