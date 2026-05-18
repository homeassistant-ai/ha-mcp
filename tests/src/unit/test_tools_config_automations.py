"""Unit tests for Automation configuration tools.

Validates the `automation_id` typed-id key across the automation config
tool lifecycle:

- `ha_config_get_automation` (issues #1299 + #1334, PR #1329)
- `ha_config_set_automation` python_transform branch (issue #1333)
- `ha_config_remove_automation` (issue #1333 Boy-Scout)

Gives sibling parity with `ha_config_get_script` (`script_id`),
`ha_config_get_scene` (`scene_id`), and `ha_config_get_dashboard`
(`url_path`). The value is the resolved entity_id when registry lookup
succeeds, falling back to the raw input otherwise — same
canonical-with-fallback semantic as scenes/dashboards.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_config_automations import AutomationConfigTools


@pytest.fixture
def mock_client():
    """Mock client satisfying read, transform, and delete paths."""
    client = MagicMock()
    client.get_automation_config = AsyncMock(
        return_value={
            "id": "abc123unique",
            "alias": "Morning Routine",
            "trigger": [{"platform": "time", "at": "07:00:00"}],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.bedroom"}}
            ],
        }
    )
    # Default: registry returns the canonical entity_id for unique_id "abc123unique".
    # Individual tests override .get_states for the no-match case.
    client.get_states = AsyncMock(
        return_value=[
            {
                "entity_id": "automation.morning_routine",
                "attributes": {"id": "abc123unique"},
            }
        ]
    )
    # fetch_entity_category path — empty success result keeps category injection off.
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {"categories": {}}}
    )
    # Transform path: upsert returns canonical entity_id by default; tests
    # override to exercise the None-entity_id fallback branches.
    client.upsert_automation_config = AsyncMock(
        return_value={
            "unique_id": "abc123unique",
            "entity_id": "automation.morning_routine",
            "result": "ok",
            "operation": "updated",
        }
    )
    # Delete path: HA returns identifier echo + resolved unique_id.
    client.delete_automation_config = AsyncMock(
        return_value={
            "identifier": "abc123unique",
            "unique_id": "abc123unique",
            "result": "ok",
            "operation": "deleted",
        }
    )
    return client


@pytest.fixture
def tools(mock_client):
    return AutomationConfigTools(mock_client)


class TestGetAutomationIdKey:
    """`automation_id` canonical key on `ha_config_get_automation` (issues #1299, #1334)."""

    async def test_returns_resolved_entity_id_when_input_is_unique_id(self, tools):
        """unique_id input → automation_id = resolved entity_id."""
        result = await tools.ha_config_get_automation(identifier="abc123unique")

        assert result["success"] is True
        assert result["automation_id"] == "automation.morning_routine"

    async def test_returns_input_when_input_is_entity_id(self, tools):
        """entity_id input → automation_id == caller input (resolver short-circuits)."""
        result = await tools.ha_config_get_automation(
            identifier="automation.morning_routine"
        )

        assert result["success"] is True
        assert result["automation_id"] == "automation.morning_routine"

    async def test_falls_back_to_identifier_when_registry_lookup_misses(
        self, tools, mock_client
    ):
        """unique_id with no registry match → automation_id falls back to raw input."""
        mock_client.get_states = AsyncMock(return_value=[])

        result = await tools.ha_config_get_automation(identifier="orphaned_unique_id")

        assert result["success"] is True
        assert result["automation_id"] == "orphaned_unique_id"

    async def test_identifier_key_no_longer_in_response(self, tools):
        """Issue #1334: redundant `identifier` echo key dropped from response.

        Family-level convergence — every `ha_config_get_*` tool surfaces one
        canonical-with-fallback typed-id key. For automations that's
        `automation_id`; the legacy `identifier` echo was the last
        leftover of the pre-#1329 shape.
        """
        result = await tools.ha_config_get_automation(identifier="abc123unique")

        assert "identifier" not in result, (
            f"`identifier` key must not be present in response, got: {result}"
        )


@pytest.fixture
def transform_tools(tools):
    """Tools instance with hash/refetch internals stubbed for python_transform tests.

    `_fetch_and_verify_hash` and `_get_automation_config_internal` are
    bypassed so tests don't have to compute matching `compute_config_hash`
    values — the parity key under test depends only on the post-upsert
    `entity_id` resolution, not on the hash arithmetic.
    """
    canonical_config = {
        "alias": "Morning Routine",
        "trigger": [{"platform": "time", "at": "07:00:00"}],
        "action": [
            {"service": "light.turn_on", "target": {"entity_id": "light.bedroom"}}
        ],
    }
    tools._fetch_and_verify_hash = AsyncMock(return_value=dict(canonical_config))
    tools._get_automation_config_internal = AsyncMock(
        return_value=(dict(canonical_config), "post_transform_hash")
    )
    return tools


class TestPythonTransformAutomationIdKey:
    """`automation_id` parity on `ha_config_set_automation` python_transform (issue #1333)."""

    async def test_uses_upsert_entity_id_when_returned(self, transform_tools):
        """upsert returns canonical entity_id → automation_id mirrors it."""
        result = await transform_tools.ha_config_set_automation(
            identifier="abc123unique",
            python_transform="config['mode'] = 'single'",
            config_hash="prior_hash",
        )

        assert result["success"] is True
        assert result["action"] == "python_transform"
        assert "identifier" not in result
        assert "unique_id" not in result
        assert result["automation_id"] == "automation.morning_routine"

    async def test_falls_back_to_entity_id_input_when_upsert_omits_it(
        self, transform_tools, mock_client
    ):
        """upsert returns no entity_id but identifier is entity_id → fallback to identifier."""
        mock_client.upsert_automation_config = AsyncMock(
            return_value={
                "unique_id": "abc123unique",
                "entity_id": None,
                "result": "ok",
                "operation": "updated",
            }
        )

        result = await transform_tools.ha_config_set_automation(
            identifier="automation.morning_routine",
            python_transform="config['mode'] = 'single'",
            config_hash="prior_hash",
        )

        assert result["success"] is True
        assert result["automation_id"] == "automation.morning_routine"

    async def test_falls_back_to_unique_id_input_when_unresolvable(
        self, transform_tools, mock_client
    ):
        """upsert returns no entity_id AND identifier is unique_id → fallback to raw input."""
        mock_client.upsert_automation_config = AsyncMock(
            return_value={
                "unique_id": "orphaned_unique_id",
                "entity_id": None,
                "result": "ok",
                "operation": "updated",
            }
        )

        result = await transform_tools.ha_config_set_automation(
            identifier="orphaned_unique_id",
            python_transform="config['mode'] = 'single'",
            config_hash="prior_hash",
        )

        assert result["success"] is True
        assert result["automation_id"] == "orphaned_unique_id"


class TestDeleteAutomationIdKey:
    """`automation_id` parity on `ha_config_remove_automation` (issue #1333 Boy-Scout).

    `wait=False` skips `wait_for_entity_removed` so the tests don't have to
    mock the polling helper — `automation_id` is set from
    `_resolve_automation_entity_id` regardless of the wait branch.
    """

    async def test_returns_resolved_entity_id_when_input_is_unique_id(self, tools):
        """unique_id input → automation_id = resolved entity_id from registry.

        Also pins the "single canonical typed key" shape — the spread of the
        underlying ``delete_automation_config`` result must not leak the
        legacy ``identifier`` / ``unique_id`` echo keys into the response.
        """
        result = await tools.ha_config_remove_automation(
            identifier="abc123unique", wait=False
        )

        assert result["success"] is True
        assert result["action"] == "delete"
        assert result["automation_id"] == "automation.morning_routine"
        assert "identifier" not in result
        assert "unique_id" not in result

    async def test_returns_input_when_input_is_entity_id(self, tools):
        """entity_id input → automation_id == identifier (resolver short-circuit)."""
        result = await tools.ha_config_remove_automation(
            identifier="automation.morning_routine", wait=False
        )

        assert result["success"] is True
        assert result["automation_id"] == "automation.morning_routine"

    async def test_falls_back_to_identifier_when_registry_lookup_misses(
        self, tools, mock_client
    ):
        """unique_id with no registry match → automation_id falls back to raw input."""
        mock_client.get_states = AsyncMock(return_value=[])

        result = await tools.ha_config_remove_automation(
            identifier="orphaned_unique_id", wait=False
        )

        assert result["success"] is True
        assert result["automation_id"] == "orphaned_unique_id"
