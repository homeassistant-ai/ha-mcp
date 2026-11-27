"""
E2E smoke tests for ha_manage_energy_prefs.

Scope: mode="get" and a minimal mode="set" roundtrip against the
freshly-initialised test container. Native proposal validation is exercised in test_native_energy_contract.py;
the E2E suite exercises the real WebSocket plumbing to catch command
renames (energy/get_prefs, energy/save_prefs) that mocks cannot.
"""

import logging

import pytest

from ..utilities.assertions import MCPAssertions, assert_mcp_success
from ..utilities.cleanup import CleanupSteps

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_energy_prefs_get_returns_expected_shape(mcp_client):
    """mode='get' returns Core's top-level lists and a config_hash.

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
    # Native defaults are available through the component; without it an
    # unconfigured Core returns no prefs and the tool reports an empty config.
    if config:
        assert all(isinstance(value, list) for value in config.values())
    else:
        assert "unavailable" in data["note"]

    # Hash must be a non-empty hex string.
    config_hash = data["config_hash"]
    assert isinstance(config_hash, str) and len(config_hash) > 0
    # compute_config_hash truncates SHA256 to 16 hex chars.
    assert len(config_hash) == 16
    int(config_hash, 16)  # raises if not hex

    logger.info(
        "energy prefs get returned %d sources, %d devices, %d water devices; hash=%s",
        len(config.get("energy_sources", [])),
        len(config.get("device_consumption", [])),
        len(config.get("device_consumption_water", [])),
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
        "config_hash must be deterministic across repeated reads of an unchanged state"
    )


@pytest.mark.asyncio
async def test_energy_prefs_set_roundtrip(mcp_client):
    """mode='set' with minimal payload, re-read, assert hash symmetry.

    Covers the bits unit tests cannot: that ``energy/save_prefs`` is
    still the right WebSocket command name and accepts the payload
    shape this tool sends. A rename on the HA side would make the
    unit tests pass (mocks still match the old name) while the real
    call 404s in CI.
    """
    # 1. Fresh get
    get_before = await mcp_client.call_tool("ha_manage_energy_prefs", {"mode": "get"})
    assert_mcp_success(get_before)
    hash_before = get_before.data["config_hash"]

    # 2. Minimal set — empty device_consumption
    set_result = await mcp_client.call_tool(
        "ha_manage_energy_prefs",
        {
            "mode": "set",
            "config": {"device_consumption": []},
            "config_hash": hash_before,
        },
    )
    assert_mcp_success(set_result)
    hash_after_set = set_result.data["config_hash"]

    # 3. Re-read, confirm hash matches the one returned by set
    get_after = await mcp_client.call_tool("ha_manage_energy_prefs", {"mode": "get"})
    assert_mcp_success(get_after)
    assert get_after.data["config_hash"] == hash_after_set, (
        "Hash from mode='set' return should match hash from subsequent "
        "mode='get' — they compute over the same effective state."
    )


@pytest.mark.asyncio
async def test_energy_prefs_per_key_config_hash_roundtrip(mcp_client):
    """Issue #1049: ``mode='get'`` exposes ``config_hash_per_key``, and
    ``mode='set'`` accepts a per-top-level-key dict-form ``config_hash``
    over the wire (Pydantic union in the tool schema). One round-trip
    proves the new shape transports cleanly.

    Mutates only ``device_consumption``; cleans up afterwards by
    restoring the original list under a fresh per-key hash.
    """
    initial = await mcp_client.call_tool("ha_manage_energy_prefs", {"mode": "get"})
    assert_mcp_success(initial)
    initial_data = initial.data
    if not initial_data["config"]:
        # Let Core create its defaults; this test must also pass in isolation
        # on the absent-component lane, where defaults cannot be discovered.
        initialized = await mcp_client.call_tool(
            "ha_manage_energy_prefs",
            {
                "mode": "set",
                "config": {},
                "config_hash": initial_data["config_hash"],
            },
        )
        assert_mcp_success(initialized)
        initial_data = initialized.data
    assert "config_hash_per_key" in initial_data, (
        "mode='get' must surface config_hash_per_key alongside config_hash"
    )
    per_key = initial_data["config_hash_per_key"]
    assert set(per_key) == set(initial_data["config"])
    assert all(isinstance(h, str) and len(h) == 16 for h in per_key.values())

    original_dc = list(initial_data["config"]["device_consumption"])
    test_stat = "sensor.e2e_per_key_hash_test"
    new_dc = original_dc + [{"stat_consumption": test_stat}]

    try:
        write = await mcp_client.call_tool(
            "ha_manage_energy_prefs",
            {
                "mode": "set",
                "config": {"device_consumption": new_dc},
                "config_hash": {
                    "device_consumption": per_key["device_consumption"],
                },
            },
        )
        assert_mcp_success(write)
        assert "config_hash_per_key" in write.data, (
            "set response must surface fresh config_hash_per_key for "
            "chained writes without an intermediate get"
        )

        verify = await mcp_client.call_tool("ha_manage_energy_prefs", {"mode": "get"})
        assert_mcp_success(verify)
        verify_dc = verify.data["config"]["device_consumption"]
        assert any(d.get("stat_consumption") == test_stat for d in verify_dc), (
            f"Per-key set must persist; got device_consumption={verify_dc}"
        )
    finally:
        with CleanupSteps() as cleanup, cleanup.step():
            # Restore original device_consumption under a fresh per-key hash.
            mcp = MCPAssertions(mcp_client)
            cleanup_initial = await mcp.call_tool_success(
                "ha_manage_energy_prefs", {"mode": "get"}
            )
            cleanup_per_key = cleanup_initial.get("config_hash_per_key", {})
            if "device_consumption" in cleanup_per_key:
                await mcp.call_tool_success(
                    "ha_manage_energy_prefs",
                    {
                        "mode": "set",
                        "config": {"device_consumption": original_dc},
                        "config_hash": {
                            "device_consumption": cleanup_per_key["device_consumption"],
                        },
                    },
                )


async def _save_devices(mcp_client, devices: list[dict]) -> None:
    """Save ``device_consumption`` whole under its per-key hash from a fresh read."""
    mcp = MCPAssertions(mcp_client)
    current = await mcp.call_tool_success("ha_manage_energy_prefs", {"mode": "get"})
    await mcp.call_tool_success(
        "ha_manage_energy_prefs",
        {
            "mode": "set",
            "config": {"device_consumption": devices},
            "config_hash": {
                "device_consumption": current["config_hash_per_key"][
                    "device_consumption"
                ]
            },
        },
    )


@pytest.mark.asyncio
async def test_energy_inspection_exposes_native_statistics_metadata(mcp_client):
    """Configured device statistics can be discovered with their real recorder units."""
    statistic_id = "sensor.total_energy_kwh"
    before = assert_mcp_success(
        await mcp_client.call_tool("ha_manage_energy_prefs", {"mode": "get"})
    )
    before = before.get("data", before)
    already_present = any(
        d.get("stat_consumption") == statistic_id
        for d in before["config"].get("device_consumption", [])
    )
    original = list(before["config"].get("device_consumption", []))
    added = False
    try:
        if not already_present:
            await _save_devices(
                mcp_client,
                original
                + [
                    {
                        "stat_consumption": statistic_id,
                        "name": "E2E statistics metadata",
                    }
                ],
            )
            added = True
        result = assert_mcp_success(
            await mcp_client.call_tool(
                "ha_manage_energy_prefs",
                {
                    "mode": "get",
                    "include_statistics": True,
                },
            )
        )
        result = result.get("data", result)
        record = next(
            r
            for r in result["statistics_metadata"]
            if r["statistic_id"] == statistic_id
        )
        assert record["unit_of_measurement"] == "kWh"
        assert record["statistics_unit_of_measurement"] == "kWh"
        assert record["has_sum"] is True
        assert "statistics_metadata" not in result["config"]
    finally:
        with CleanupSteps() as cleanup, cleanup.step():
            if added:
                await _save_devices(mcp_client, original)
