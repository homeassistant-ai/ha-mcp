"""Dashboard edits preserve the reporter's exact data on every existing backend."""

import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest
from ruamel.yaml import YAML

from ...utilities.assertions import MCPAssertions, safe_call_tool

FIXTURES = Path(__file__).with_name("fixtures")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["patch", "python_transform"])
async def test_reporter_dashboard_edits_and_conflicts(
    mcp_client, mode, record_property
):
    """Nested template edits and stale hashes work with and without the component."""
    fixture = (FIXTURES / "reporter-media.yaml").read_bytes()
    assert (
        hashlib.sha256(fixture).hexdigest()
        == "2ccc763882e7abe4dd74b4a55bbc293db23e7a647f20696f0e2b23106c160329"
    )
    baseline = YAML(typ="safe").load(fixture)
    section = json.loads((FIXTURES / "reporter-section.json").read_text())
    transform = (FIXTURES / "reporter-transform.txt").read_text()
    path = "native-edit-" + uuid4().hex[:10]
    mcp = MCPAssertions(mcp_client)
    timings = []
    try:
        await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {"url_path": path, "config": baseline, "MandatoryBPS": False},
        )
        original = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"url_path": path}
        )
        assert len(original["config"]["views"][1]["sections"]) == 3
        for _ in range(3):
            before = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": path}
            )
            arguments = {
                "url_path": path,
                "config_hash": before["config_hash"],
                "MandatoryBPS": False,
            }
            if mode == "patch":
                arguments["patch"] = [
                    {"op": "add", "path": "/views/1/sections/-", "value": section}
                ]
            else:
                arguments["python_transform"] = transform
            started = time.monotonic()
            result = await mcp.call_tool_success("ha_config_set_dashboard", arguments)
            timings.append(time.monotonic() - started)
            assert result["write_committed"] is True
            assert result["post_write_verified"] is True
            after = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": path}
            )
            assert after["config"]["views"][1]["sections"] == before["config"]["views"][
                1
            ]["sections"] + [section]
            assert (
                result["config_hash"] == after["config_hash"] != before["config_hash"]
            )
            await mcp.call_tool_failure(
                "ha_config_set_dashboard", arguments, expected_error="conflict"
            )
            unchanged = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": path}
            )
            assert unchanged["config_hash"] == after["config_hash"]
            await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": path,
                    "config_hash": after["config_hash"],
                    "patch": [{"op": "remove", "path": "/views/1/sections/3"}],
                    "MandatoryBPS": False,
                },
            )
        restored = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"url_path": path}
        )
        assert restored["config"] == original["config"]
        assert restored["config_hash"] == original["config_hash"]
        # The reporter's smallest edit is already present in the supplied fixture.
        noop = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": path,
                "config_hash": restored["config_hash"],
                "patch": [
                    {
                        "op": "replace",
                        "path": "/views/0/sections/1/cards/0/icon",
                        "value": "mdi:music-box-multiple",
                    }
                ],
                "MandatoryBPS": False,
            },
        )
        assert noop["config_hash"] == restored["config_hash"]
        record_property("dashboard_edit_seconds", json.dumps(timings))
    finally:
        await safe_call_tool(
            mcp_client, "ha_config_delete_dashboard", {"url_path": path}
        )


@pytest.mark.asyncio
async def test_native_dashboard_backend_measurements(
    mcp_client, ha_client, record_property
):
    """Compare equivalent legacy/native backend work; timing is evidence, not a gate."""
    from ha_mcp.utils.config_hash import compute_config_hash

    info = await ha_client.send_websocket_message({"type": "ha_mcp_tools/info"})
    if "dashboard_edit" not in (info.get("result") or {}).get("capabilities", []):
        # Component-absent lanes exercise the public fallback in the test above.
        record_property(
            "dashboard_backend_measurement",
            "component absent; public fallback tested separately",
        )
        return

    baseline = YAML(typ="safe").load((FIXTURES / "reporter-media.yaml").read_text())
    path = "edit-bench-" + uuid4().hex[:10]
    mcp = MCPAssertions(mcp_client)
    measurements = {"legacy": [], "native": []}

    async def exchange(message):
        response = await ha_client.send_websocket_message(message)
        assert response.get("success", True), response
        size = len(json.dumps(message).encode()) + len(json.dumps(response).encode())
        return response.get("result"), size

    try:
        await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {"url_path": path, "config": baseline, "MandatoryBPS": False},
        )
        current = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"url_path": path}
        )
        current_hash = current["config_hash"]
        # Alternate order to reduce warm-up/order bias; all edits are actual changes.
        for iteration in range(8):
            for mode in (
                ["legacy", "native"] if iteration % 2 == 0 else ["native", "legacy"]
            ):
                icon = "mdi:lamp" if mode == "legacy" else "mdi:music-box-multiple"
                # Adjacent iterations may begin with the same mode; vary each value.
                icon += "-outline" if iteration % 2 else ""
                started = time.monotonic()
                if mode == "legacy":
                    document, received = await exchange(
                        {"type": "lovelace/config", "url_path": path, "force": True}
                    )
                    assert compute_config_hash(document) == current_hash
                    document["views"][0]["sections"][1]["cards"][0]["icon"] = icon
                    _, saved = await exchange(
                        {
                            "type": "lovelace/config/save",
                            "url_path": path,
                            "config": document,
                        }
                    )
                    verified, reloaded = await exchange(
                        {"type": "lovelace/config", "url_path": path, "force": True}
                    )
                    byte_count, calls = received + saved + reloaded, 3
                    current_hash = compute_config_hash(verified)
                else:
                    result, byte_count = await exchange(
                        {
                            "type": "ha_mcp_tools/dashboard_edit",
                            "url_path": path,
                            "expected_hash": current_hash,
                            "patch": [
                                {
                                    "op": "replace",
                                    "path": "/views/0/sections/1/cards/0/icon",
                                    "value": icon,
                                }
                            ],
                        }
                    )
                    assert result["success"] and result["post_write_verified"], result
                    verified = result["config"]
                    current_hash = result["config_hash"]
                    calls = 1
                assert verified["views"][0]["sections"][1]["cards"][0]["icon"] == icon
                measurements[mode].append(
                    {
                        "seconds": time.monotonic() - started,
                        "bytes": byte_count,
                        "commands": calls,
                    }
                )
        assert max(row["bytes"] for row in measurements["native"]) < min(
            row["bytes"] for row in measurements["legacy"]
        )
        record_property("dashboard_backend_measurements", json.dumps(measurements))
        print("DASHBOARD_BACKEND_MEASUREMENTS " + json.dumps(measurements), flush=True)
    finally:
        await safe_call_tool(
            mcp_client, "ha_config_delete_dashboard", {"url_path": path}
        )
