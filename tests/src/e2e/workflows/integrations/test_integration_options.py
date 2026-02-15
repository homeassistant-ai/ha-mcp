"""
E2E tests for integration options tools.
"""

import pytest

from tests.src.e2e.utilities.assertions import (
    assert_mcp_success,
    safe_call_tool,
)


@pytest.mark.asyncio
@pytest.mark.integrations
class TestIntegrationOptions:
    """Test VT integration options read/write tooling."""

    async def _get_vt_entries(self, mcp_client):
        result = await mcp_client.call_tool(
            "ha_get_integration",
            {"domain": "versatile_thermostat", "include_options": True},
        )
        data = assert_mcp_success(result, "list VT integrations")
        return data.get("entries", [])

    async def test_get_integration_options_success(self, mcp_client):
        entries = await self._get_vt_entries(mcp_client)
        if not entries:
            pytest.skip("No versatile_thermostat entries found")

        entry_id = entries[0]["entry_id"]
        result = await mcp_client.call_tool(
            "ha_get_integration_options",
            {"entry_id": entry_id, "include_validation_hints": True},
        )
        data = assert_mcp_success(result, "get integration options")
        assert data["entry_id"] == entry_id
        assert "options" in data

    async def test_set_integration_options_dry_run(self, mcp_client):
        entries = await self._get_vt_entries(mcp_client)
        if not entries:
            pytest.skip("No versatile_thermostat entries found")

        central = next(
            (e for e in entries if e.get("title", "").lower() == "central configuration"),
            None,
        )
        if not central:
            pytest.skip("No central VT entry found")

        result = await mcp_client.call_tool(
            "ha_set_integration_options",
            {
                "entry_id": central["entry_id"],
                "options_patch": {"presence_sensor_entity_id": "person.knuth"},
                "dry_run": True,
                "strict_keys": True,
            },
        )
        data = assert_mcp_success(result, "VT dry-run options patch")
        assert data["applied"] is False
        assert "diff" in data

    async def test_set_integration_options_requires_confirm(self, mcp_client):
        entries = await self._get_vt_entries(mcp_client)
        if not entries:
            pytest.skip("No versatile_thermostat entries found")

        central = next(
            (e for e in entries if e.get("title", "").lower() == "central configuration"),
            None,
        )
        if not central:
            pytest.skip("No central VT entry found")

        data = await safe_call_tool(
            mcp_client,
            "ha_set_integration_options",
            {
                "entry_id": central["entry_id"],
                "options_patch": {"presence_sensor_entity_id": "person.knuth"},
                "dry_run": False,
                "confirm": False,
            },
        )
        assert not data.get("success", False)
        error = data.get("error", {})
        if isinstance(error, dict):
            assert error.get("code") == "VALIDATION_MISSING_PARAMETER"

    async def test_set_integration_options_unknown_key(self, mcp_client):
        entries = await self._get_vt_entries(mcp_client)
        if not entries:
            pytest.skip("No versatile_thermostat entries found")

        data = await safe_call_tool(
            mcp_client,
            "ha_set_integration_options",
            {
                "entry_id": entries[0]["entry_id"],
                "options_patch": {"unknown_vt_key": True},
                "dry_run": True,
                "strict_keys": True,
            },
        )
        assert not data.get("success", False)
        error = data.get("error", {})
        if isinstance(error, dict):
            assert error.get("code") == "CONFIG_VALIDATION_FAILED"
