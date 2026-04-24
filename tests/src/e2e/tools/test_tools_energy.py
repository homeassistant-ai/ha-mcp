"""
E2E smoke tests for ha_manage_energy_prefs.

Scope: read-only mode="get" against the freshly-initialised test container.
The container starts with no ``.storage/energy`` file, so HA returns the
empty default — confirming tool registration, WebSocket connectivity, and
response shape without mutating state.

Write paths (mode="set", including dry_run) are covered by the unit tests
under tests/src/unit/test_tools_energy.py. They are deliberately not
exercised here to avoid coupling CI runs to container-persisted energy
prefs state.
"""

import logging

import pytest

from ..utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_energy_prefs_get_returns_expected_shape(mcp_client):
    """mode='get' returns the three top-level keys and a config_hash.

    A fresh HA install has no ``.storage/energy``; the endpoint still
    succeeds and returns empty lists for the top-level keys. The tool
    must echo that structure and compute a deterministic hash.
    """
    result = await mcp_client.call_tool(
        "ha_manage_energy_prefs",
        {"mode": "get"},
    )
    raw = assert_mcp_success(result, "energy prefs get")
    data = raw.get("data", raw)

    assert data.get("success") is True
    assert data.get("mode") == "get"
    assert "config" in data
    assert "config_hash" in data

    config = data["config"]
    assert isinstance(config, dict)
    # All three top-level keys must be present in the response, even on a
    # fresh install.
    for key in ("energy_sources", "device_consumption", "device_consumption_water"):
        assert key in config, (
            f"top-level key '{key}' missing from energy prefs response — "
            f"got keys: {sorted(config.keys())}"
        )
        assert isinstance(config[key], list), (
            f"top-level key '{key}' must be a list, got {type(config[key]).__name__}"
        )

    # Hash must be a non-empty hex string.
    config_hash = data["config_hash"]
    assert isinstance(config_hash, str) and len(config_hash) > 0
    # compute_config_hash truncates SHA256 to 16 hex chars.
    assert len(config_hash) == 16
    int(config_hash, 16)  # raises if not hex

    logger.info(
        "energy prefs get returned %d sources, %d devices, %d water devices; hash=%s",
        len(config["energy_sources"]),
        len(config["device_consumption"]),
        len(config["device_consumption_water"]),
        config_hash,
    )


@pytest.mark.asyncio
async def test_energy_prefs_get_hash_is_deterministic(mcp_client):
    """Repeated mode='get' on an unchanged state returns the same hash."""
    first = await mcp_client.call_tool(
        "ha_manage_energy_prefs",
        {"mode": "get"},
    )
    second = await mcp_client.call_tool(
        "ha_manage_energy_prefs",
        {"mode": "get"},
    )

    first_data = assert_mcp_success(first, "first get").get("data", {})
    second_data = assert_mcp_success(second, "second get").get("data", {})

    assert first_data.get("config_hash") == second_data.get("config_hash"), (
        "config_hash must be deterministic across repeated reads of an "
        "unchanged state"
    )
