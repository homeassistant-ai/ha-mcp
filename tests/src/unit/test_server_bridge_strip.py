"""Locks down the `server.HomeAssistantSmartMCPServer.get_entities_by_area`
bridge's invocation of `strip_internal_fields` on its return value.

The bridge is a public method that wraps `smart_tools.get_entities_by_area`
(which enriches per-entity dicts with `_hidden_by` so downstream search
branches can apply the score penalty without a second registry lookup).
That helper output crosses the public-API boundary here — if a future
refactor drops the strip call, internal fields leak directly to MCP
clients with no signal in CI. This test pins the contract.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_get_entities_by_area_bridge_strips_internal_fields():
    # Stub the minimal HomeAssistantSmartMCPServer surface needed for the bridge:
    # the bridge just delegates to `self.smart_tools.get_entities_by_area`
    # and then strips internals.
    fake_smart_tools = SimpleNamespace()
    fake_smart_tools.get_entities_by_area = AsyncMock(
        return_value={
            "areas": {
                "kitchen": {
                    "area_name": "Kitchen",
                    "entities": {
                        "light": [
                            {
                                "entity_id": "light.kitchen_main",
                                "_hidden_by": None,
                                "_aliases": [],
                                "state": "on",
                            },
                            {
                                "entity_id": "light.kitchen_diag",
                                "_hidden_by": "integration",
                                "_aliases": [],
                                "state": "off",
                            },
                        ]
                    },
                }
            },
            "total_entities": 2,
        }
    )

    # Import after monkeypatch path setup — server.py pulls in fastmcp
    # which is heavy but available in the test env.
    from ha_mcp.server import HomeAssistantSmartMCPServer

    # Construct without going through __init__ (heavy startup). The
    # bridge only touches self.smart_tools.
    instance = HomeAssistantSmartMCPServer.__new__(HomeAssistantSmartMCPServer)
    instance.smart_tools = fake_smart_tools

    result = await instance.get_entities_by_area("kitchen")

    # No leading-underscore keys anywhere in the tree.
    def _no_internals(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(k, str) and k.startswith("_"):
                    return False
                if not _no_internals(v):
                    return False
        elif isinstance(obj, list):
            for item in obj:
                if not _no_internals(item):
                    return False
        return True

    assert _no_internals(result), (
        f"bridge leaked internal fields to public output: {result}"
    )

    # And the public data is still there.
    light_entries = result["areas"]["kitchen"]["entities"]["light"]
    assert {e["entity_id"] for e in light_entries} == {
        "light.kitchen_main",
        "light.kitchen_diag",
    }
