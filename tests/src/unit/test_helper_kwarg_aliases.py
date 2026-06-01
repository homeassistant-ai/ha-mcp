"""Bug 2: ha_config_set_helper must accept shorthand kwargs (`min`, `max`, `unit`).

The function signature uses `min_value`/`max_value`/`unit_of_measurement`, but
agents (and HA's own JSON shape) commonly pass `min`/`max`/`unit`. Without
``validation_alias``, pydantic raises ``unexpected_keyword_argument`` inside
FastMCP's tool wrapper before the function body runs.

These tests exercise the fix through FastMCP's actual tool dispatch path
(``Tool.run`` → ``TypeAdapter.validate_python``), because the bug is in
schema-level validation — calling the bare Python function directly would
bypass it.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    """Mock client that records every WS message sent."""
    client = MagicMock()

    async def ws_handler(msg: dict) -> dict:
        msg_type = msg.get("type", "")

        if msg_type == "config/entity_registry/get":
            return {
                "success": True,
                "result": {
                    "entity_id": msg.get("entity_id"),
                    "unique_id": "abc123",
                    "platform": "input_number",
                },
            }
        if msg_type.endswith("/list"):
            # Existing helper for the update path.
            return {
                "success": True,
                "result": [{
                    "id": "abc123",
                    "name": "Existing",
                    "min": 0,
                    "max": 100,
                    "step": 1,
                    "mode": "slider",
                }],
            }
        if msg_type.endswith("/create") or msg_type.endswith("/update"):
            return {
                "success": True,
                "result": {
                    "id": "abc123",
                    **{k: v for k, v in msg.items() if k != "type"},
                },
            }
        if msg_type == "config/entity_registry/update":
            return {
                "success": True,
                "result": {"entity_entry": {"entity_id": msg.get("entity_id")}},
            }
        return {"success": True, "result": {}}

    client.send_websocket_message = AsyncMock(side_effect=ws_handler)
    return client


@pytest.fixture
async def helper_tool(mock_client):
    """Register helper tools on a real FastMCP server and return the tool object."""
    from ha_mcp.tools.tools_config_helpers import register_config_helper_tools

    server = FastMCP("test-aliases")
    register_config_helper_tools(server, mock_client)
    return await server.get_tool("ha_config_set_helper")


def _find_msg(client: Any, msg_type: str) -> dict | None:
    for call in client.send_websocket_message.call_args_list:
        msg = call[0][0]
        if msg.get("type") == msg_type:
            return msg
    return None


# ---------------------------------------------------------------------------
# Tests — must pass through FastMCP's TypeAdapter (Tool.run), not the bare fn.
# ---------------------------------------------------------------------------


class TestSchemaSurfacesCanonicalNames:
    """The MCP-exposed schema should advertise the canonical names only.

    The aliases are accepted at validation time but should NOT pollute the
    tool-discovery surface — agents see the long-form names per the existing
    contract.
    """

    def test_schema_exposes_min_value_not_min(self, helper_tool):
        props = helper_tool.parameters.get("properties", {})
        assert "min_value" in props
        assert "max_value" in props
        assert "unit_of_measurement" in props
        # Aliases are not advertised separately (they'd duplicate the schema).
        assert "min" not in props
        assert "max" not in props
        assert "unit" not in props


class TestShorthandAliasesAccepted:
    """Bug 2: passing the shorthand alias must not raise validation errors."""

    async def test_min_alias_accepted_on_create(self, helper_tool, mock_client):
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await helper_tool.run({
                "helper_type": "input_number",
                "name": "Min Alias",
                "min": 5,
                "max_value": 50,  # mix canonical + alias to prove both paths
            })

        create_msg = _find_msg(mock_client, "input_number/create")
        assert create_msg is not None, "create message should be sent"
        assert create_msg.get("min") == 5, (
            f"Bug 2: shorthand 'min' kwarg was not accepted/forwarded. "
            f"Create message: {create_msg!r}"
        )
        assert create_msg.get("max") == 50

    async def test_max_alias_accepted_on_create(self, helper_tool, mock_client):
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await helper_tool.run({
                "helper_type": "input_number",
                "name": "Max Alias",
                "min_value": 0,
                "max": 99,
            })

        create_msg = _find_msg(mock_client, "input_number/create")
        assert create_msg is not None
        assert create_msg.get("max") == 99, (
            f"Bug 2: shorthand 'max' kwarg was not accepted/forwarded. "
            f"Create message: {create_msg!r}"
        )

    async def test_unit_alias_accepted_on_create(self, helper_tool, mock_client):
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await helper_tool.run({
                "helper_type": "input_number",
                "name": "Unit Alias",
                "min_value": 0,
                "max_value": 100,
                "unit": "C",
            })

        create_msg = _find_msg(mock_client, "input_number/create")
        assert create_msg is not None
        assert create_msg.get("unit_of_measurement") == "C", (
            f"Bug 2: shorthand 'unit' kwarg was not accepted/forwarded. "
            f"Create message: {create_msg!r}"
        )

    async def test_all_aliases_accepted_together(self, helper_tool, mock_client):
        """The realistic case from the bug report: min/max/unit all shorthand."""
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await helper_tool.run({
                "helper_type": "input_number",
                "name": "Thermostat",
                "min": 60,
                "max": 85,
                "unit": "F",
            })

        # No exception means Bug 2 is fixed at the validation layer.
        assert result is not None
        create_msg = _find_msg(mock_client, "input_number/create")
        assert create_msg is not None
        assert create_msg["min"] == 60
        assert create_msg["max"] == 85
        assert create_msg["unit_of_measurement"] == "F"

    async def test_aliases_accepted_on_update(self, helper_tool, mock_client):
        """Update path must also honour the aliases (same schema, same code)."""
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await helper_tool.run({
                "helper_type": "input_number",
                "helper_id": "abc123",
                "min": 10,
                "max": 90,
                "unit": "%",
            })

        update_msg = _find_msg(mock_client, "input_number/update")
        assert update_msg is not None, "update message should be sent"
        assert update_msg.get("min") == 10
        assert update_msg.get("max") == 90
        assert update_msg.get("unit_of_measurement") == "%"


class TestCanonicalStillWorks:
    """Regression guard: the long-form names must keep working unchanged."""

    async def test_canonical_names_still_accepted(self, helper_tool, mock_client):
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await helper_tool.run({
                "helper_type": "input_number",
                "name": "Canonical",
                "min_value": 1,
                "max_value": 10,
                "unit_of_measurement": "kW",
            })

        create_msg = _find_msg(mock_client, "input_number/create")
        assert create_msg is not None
        assert create_msg.get("min") == 1
        assert create_msg.get("max") == 10
        assert create_msg.get("unit_of_measurement") == "kW"
