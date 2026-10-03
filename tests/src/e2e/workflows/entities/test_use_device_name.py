"""Home Assistant 2026.10 "Use device name" through ha_set_entity (issue #2611).

HA stores the switch as an entity-registry ``name`` of ``""`` and then shows the
device name as the entity's friendly name. ``ha_set_entity(name="")`` keeps its
revert-to-default meaning; ``use_device_name`` carries the new state. Cores
before 2026.10 store ``""`` but render the default name, so the tool refuses the
switch there instead of silently doing the opposite of what was asked.
"""

import json
import os
from typing import Any

import httpx
import pytest
from packaging.version import Version
from test_constants import TEST_TOKEN

from ha_mcp._vendor.fastmcp import Client

from ...utilities.assertions import MCPAssertions, parse_mcp_result, safe_call_tool

# default_config's sun integration: has_entity_name with a "Sun" device, so the
# friendly name visibly changes between "Sun Next dawn" and "Sun".
ENTITY = "sensor.sun_next_dawn"


def _ha_supports_use_device_name() -> bool:
    url = os.environ["HOMEASSISTANT_URL"].rstrip("/")
    response = httpx.get(
        f"{url}/api/config",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        timeout=30,
    )
    response.raise_for_status()
    return Version(response.json()["version"]).release[:2] >= (2026, 10)


async def _entry(mcp_client: Client) -> dict[str, Any]:
    data = parse_mcp_result(
        await mcp_client.call_tool("ha_get_entity", {"entity_id": ENTITY})
    )
    assert data.get("success"), data
    return data["entity_entry"]


async def _friendly_name(mcp_client: Client) -> str | None:
    data = parse_mcp_result(
        await mcp_client.call_tool("ha_get_state", {"entity_id": ENTITY})
    )
    assert data.get("success"), data
    return data["data"]["attributes"].get("friendly_name")


@pytest.mark.registry
class TestUseDeviceName:
    async def test_switch_on_and_off(self, mcp_client: Client) -> None:
        """On a 2026.10 Core the switch stores ``""`` and the friendly name
        becomes the device name; off restores the integration default."""
        if not _ha_supports_use_device_name():
            pytest.skip("Use device name needs Home Assistant 2026.10")
        before = await _entry(mcp_client)
        default_friendly = await _friendly_name(mcp_client)
        try:
            on = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_set_entity", {"entity_id": ENTITY, "use_device_name": True}
                )
            )
            assert on.get("success"), on
            entry = await _entry(mcp_client)
            assert entry["name"] == "", entry
            assert entry["uses_device_name"] is True, entry
            assert await _friendly_name(mcp_client) == "Sun"

            off = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_set_entity", {"entity_id": ENTITY, "use_device_name": False}
                )
            )
            assert off.get("success"), off
            entry = await _entry(mcp_client)
            assert entry["name"] is None, entry
            assert entry["uses_device_name"] is False, entry
            assert await _friendly_name(mcp_client) == default_friendly
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_set_entity",
                {"entity_id": ENTITY, "name": before.get("name") or ""},
            )

    async def test_refused_on_older_core(self, mcp_client: Client) -> None:
        if _ha_supports_use_device_name():
            pytest.skip(
                "Core supports Use device name; the refusal path is for older Cores"
            )
        mcp = MCPAssertions(mcp_client)
        data = await mcp.call_tool_failure(
            "ha_set_entity", {"entity_id": ENTITY, "use_device_name": True}
        )
        assert data["error"]["code"] == "VALIDATION_INVALID_PARAMETER", data
        assert "2026.10" in json.dumps(data), data
        assert (await _entry(mcp_client))["name"] is None

    async def test_rejected_together_with_name(self, mcp_client: Client) -> None:
        mcp = MCPAssertions(mcp_client)
        data = await mcp.call_tool_failure(
            "ha_set_entity",
            {"entity_id": ENTITY, "name": "Dawn", "use_device_name": True},
        )
        assert data["error"]["code"] == "VALIDATION_INVALID_PARAMETER", data
        assert (await _entry(mcp_client))["name"] is None
