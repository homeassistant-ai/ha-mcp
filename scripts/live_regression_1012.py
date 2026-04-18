#!/usr/bin/env python3
"""Live-run of the TestHelperRegistryClear assertions against a real HA instance.

Runs the same logical checks as
tests/src/e2e/workflows/config/test_helper_crud.py::TestHelperRegistryClear
but outside pytest: no testcontainer, no conftest fixtures — direct FastMCP
in-memory transport against whichever HA base_url + token you provide via env.

Usage (WSL / Linux bash):
    export HA_BASE_URL="https://abiyvoolfcx1rt5zq7aijqxazg4tgwro.ui.nabu.casa"
    export HA_TOKEN="<RBO LLAT>"
    uv run python scripts/live_regression_1012.py

Exit codes:
    0 — all three tests passed
    1 — at least one test failed

The script creates and deletes its own areas / labels / helpers in whatever
HA instance it points to. Safe to run against a live instance: every resource
it creates has an "E2E Clear …" prefix and is removed in the finally block
(even on assertion failure).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Allow running from repo root without installing
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastmcp import Client

from ha_mcp.client import HomeAssistantClient
from ha_mcp.server import HomeAssistantSmartMCPServer


def parse_result(result):
    """Extract dict from FastMCP result (handles both success and isError)."""
    if hasattr(result, "isError") and result.isError:
        if hasattr(result, "content") and result.content and hasattr(result.content[0], "text"):
            try:
                return json.loads(result.content[0].text)
            except json.JSONDecodeError:
                return {"success": False, "error": result.content[0].text}
        return {"success": False, "error": "isError with no content"}
    if hasattr(result, "content") and result.content and hasattr(result.content[0], "text"):
        try:
            return json.loads(result.content[0].text)
        except json.JSONDecodeError:
            return {"raw": result.content[0].text}
    return {}


def assert_success(result, label):
    data = parse_result(result)
    if data.get("success") is not True:
        raise AssertionError(f"{label} failed: {json.dumps(data, indent=2)[:500]}")
    return data


async def test_simple_clear_area(mcp):
    print("\n[1/3] test_helper_clear_area_with_empty_string (SIMPLE)")
    area_id = None
    entity_id = None
    try:
        area_data = assert_success(
            await mcp.call_tool("ha_config_set_area", {"name": "E2E Helper Clear Area"}),
            "Create area",
        )
        area_id = area_data["area_id"]
        print(f"  area_id = {area_id}")

        create_data = assert_success(
            await mcp.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "input_boolean",
                    "name": "E2E Clear Area Helper",
                    "area_id": area_id,
                },
            ),
            "Create helper with area",
        )
        entity_id = create_data.get("entity_id") or f"input_boolean.{create_data['helper_data']['id']}"
        print(f"  entity_id = {entity_id}")

        state = assert_success(
            await mcp.call_tool("ha_get_entity", {"entity_id": entity_id}),
            "Get entity after create",
        )
        assigned = state.get("entity_entry", {}).get("area_id")
        assert assigned == area_id, f"Area not assigned on create: expected {area_id!r}, got {assigned!r}"
        print(f"  area_id on entity after create: {assigned} ✓")

        assert_success(
            await mcp.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "input_boolean",
                    "helper_id": entity_id,
                    "name": "E2E Clear Area Helper",
                    "area_id": "",
                },
            ),
            "Clear helper area",
        )

        state = assert_success(
            await mcp.call_tool("ha_get_entity", {"entity_id": entity_id}),
            "Get entity after clear",
        )
        cleared = state.get("entity_entry", {}).get("area_id")
        assert cleared is None, f"Area not cleared: expected None, got {cleared!r}"
        print(f"  area_id after clear: {cleared!r} ✓")
        print("  PASS")
        return True
    except AssertionError as e:
        print(f"  FAIL: {e}")
        return False
    finally:
        if entity_id:
            try:
                await mcp.call_tool(
                    "ha_config_remove_helper",
                    {"helper_type": "input_boolean", "helper_id": entity_id},
                )
            except Exception as e:
                print(f"  (cleanup helper: {e})")
        if area_id:
            try:
                await mcp.call_tool("ha_config_remove_area", {"area_id": area_id})
            except Exception as e:
                print(f"  (cleanup area: {e})")


async def test_simple_clear_labels(mcp):
    print("\n[2/3] test_helper_clear_labels_with_empty_list (SIMPLE)")
    label_id = None
    entity_id = None
    try:
        label_data = assert_success(
            await mcp.call_tool("ha_config_set_label", {"name": "E2E Helper Clear Label"}),
            "Create label",
        )
        label_id = label_data["label_id"]
        print(f"  label_id = {label_id}")

        create_data = assert_success(
            await mcp.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "input_boolean",
                    "name": "E2E Clear Labels Helper",
                    "labels": [label_id],
                },
            ),
            "Create helper with labels",
        )
        entity_id = create_data.get("entity_id") or f"input_boolean.{create_data['helper_data']['id']}"
        print(f"  entity_id = {entity_id}")

        state = assert_success(
            await mcp.call_tool("ha_get_entity", {"entity_id": entity_id}),
            "Get entity after create",
        )
        assigned_labels = state.get("entity_entry", {}).get("labels") or []
        assert label_id in assigned_labels, f"Label not assigned: expected {label_id} in {assigned_labels}"
        print(f"  labels on entity after create: {assigned_labels} ✓")

        assert_success(
            await mcp.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "input_boolean",
                    "helper_id": entity_id,
                    "name": "E2E Clear Labels Helper",
                    "labels": [],
                },
            ),
            "Clear helper labels",
        )

        state = assert_success(
            await mcp.call_tool("ha_get_entity", {"entity_id": entity_id}),
            "Get entity after clear",
        )
        cleared_labels = state.get("entity_entry", {}).get("labels") or []
        assert cleared_labels == [], f"Labels not cleared: expected [], got {cleared_labels!r}"
        print(f"  labels after clear: {cleared_labels!r} ✓")
        print("  PASS")
        return True
    except AssertionError as e:
        print(f"  FAIL: {e}")
        return False
    finally:
        if entity_id:
            try:
                await mcp.call_tool(
                    "ha_config_remove_helper",
                    {"helper_type": "input_boolean", "helper_id": entity_id},
                )
            except Exception as e:
                print(f"  (cleanup helper: {e})")
        if label_id:
            try:
                await mcp.call_tool("ha_config_remove_label", {"label_id": label_id})
            except Exception as e:
                print(f"  (cleanup label: {e})")


async def test_flow_clear_area(mcp):
    print("\n[3/3] test_flow_helper_clear_area_with_empty_string (FLOW, min_max)")
    area_id = None
    entry_id = None
    try:
        area_data = assert_success(
            await mcp.call_tool("ha_config_set_area", {"name": "E2E Flow Clear Area"}),
            "Create area",
        )
        area_id = area_data["area_id"]
        print(f"  area_id = {area_id}")

        create_data = assert_success(
            await mcp.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "min_max",
                    "name": "E2E Flow Clear Area Helper",
                    "config": {
                        "name": "E2E Flow Clear Area Helper",
                        "entity_ids": ["sensor.demo_temperature", "sensor.demo_outside_temperature"],
                        "type": "min",
                    },
                    "area_id": area_id,
                },
            ),
            "Create flow helper with area",
        )
        entry_id = create_data["entry_id"]
        entity_ids = create_data.get("entity_ids") or []
        assert entity_ids, f"Flow create returned no entity_ids: {create_data}"
        target = entity_ids[0]
        print(f"  entry_id = {entry_id}, entity = {target}")

        state = assert_success(
            await mcp.call_tool("ha_get_entity", {"entity_id": target}),
            "Get flow entity after create",
        )
        assigned = state.get("entity_entry", {}).get("area_id")
        assert assigned == area_id, f"Area not assigned on flow create: expected {area_id!r}, got {assigned!r}"
        print(f"  area_id on entity after create: {assigned} ✓")

        assert_success(
            await mcp.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "min_max",
                    "helper_id": entry_id,
                    "name": "E2E Flow Clear Area Helper",
                    "config": {
                        "entity_ids": ["sensor.demo_temperature", "sensor.demo_outside_temperature"],
                        "type": "min",
                    },
                    "area_id": "",
                },
            ),
            "Clear flow helper area",
        )

        state = assert_success(
            await mcp.call_tool("ha_get_entity", {"entity_id": target}),
            "Get flow entity after clear",
        )
        cleared = state.get("entity_entry", {}).get("area_id")
        assert cleared is None, f"Flow helper area not cleared: expected None, got {cleared!r}"
        print(f"  area_id after clear: {cleared!r} ✓")
        print("  PASS")
        return True
    except AssertionError as e:
        print(f"  FAIL: {e}")
        return False
    finally:
        if entry_id:
            try:
                await mcp.call_tool(
                    "ha_delete_config_entry", {"entry_id": entry_id, "confirm": True}
                )
            except Exception as e:
                print(f"  (cleanup entry: {e})")
        if area_id:
            try:
                await mcp.call_tool("ha_config_remove_area", {"area_id": area_id})
            except Exception as e:
                print(f"  (cleanup area: {e})")


async def test_simple_clear_combined(mcp):
    print("\n[4/5] test_helper_clear_area_and_labels_together (SIMPLE)")
    area_id = None
    label_id = None
    entity_id = None
    try:
        area_data = assert_success(
            await mcp.call_tool(
                "ha_config_set_area", {"name": "E2E Combined Clear Area"}
            ),
            "Create test area",
        )
        area_id = area_data.get("area_id")
        print(f"  area_id = {area_id}")

        label_data = assert_success(
            await mcp.call_tool(
                "ha_config_set_label", {"name": "E2E Combined Clear Label"}
            ),
            "Create test label",
        )
        label_id = label_data.get("label_id")
        print(f"  label_id = {label_id}")

        create_data = assert_success(
            await mcp.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "input_boolean",
                    "name": "E2E Combined Clear Helper",
                    "area_id": area_id,
                    "labels": [label_id],
                },
            ),
            "Create helper with area+labels",
        )
        entity_id = create_data.get("entity_id") or f"input_boolean.{create_data['helper_data']['id']}"
        print(f"  entity_id = {entity_id}")

        before = assert_success(
            await mcp.call_tool("ha_get_entity", {"entity_id": entity_id}),
            "Get entity before clear",
        )
        assert before.get("entity_entry", {}).get("area_id") == area_id
        assert label_id in (before.get("entity_entry", {}).get("labels") or [])
        print("  both area_id and labels set on entity ✓")

        assert_success(
            await mcp.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "input_boolean",
                    "helper_id": entity_id,
                    "name": "E2E Combined Clear Helper",
                    "area_id": "",
                    "labels": [],
                },
            ),
            "Clear both in one call",
        )

        after = assert_success(
            await mcp.call_tool("ha_get_entity", {"entity_id": entity_id}),
            "Get entity after clear",
        )
        cleared_area = after.get("entity_entry", {}).get("area_id")
        cleared_labels = after.get("entity_entry", {}).get("labels") or []
        assert cleared_area is None, f"Combined clear dropped area_id: got {cleared_area!r}"
        assert cleared_labels == [], f"Combined clear dropped labels: got {cleared_labels!r}"
        print(f"  area_id={cleared_area!r}, labels={cleared_labels!r} ✓")
        print("  PASS")
        return True
    except AssertionError as e:
        print(f"  FAIL: {e}")
        return False
    finally:
        if entity_id:
            try:
                await mcp.call_tool(
                    "ha_config_remove_helper",
                    {"helper_type": "input_boolean", "helper_id": entity_id},
                )
            except Exception as e:
                print(f"  (cleanup helper: {e})")
        if label_id:
            try:
                await mcp.call_tool("ha_config_remove_label", {"label_id": label_id})
            except Exception as e:
                print(f"  (cleanup label: {e})")
        if area_id:
            try:
                await mcp.call_tool("ha_config_remove_area", {"area_id": area_id})
            except Exception as e:
                print(f"  (cleanup area: {e})")


async def test_flow_clear_labels(mcp):
    print("\n[5/5] test_flow_helper_clear_labels_with_empty_list (FLOW, min_max)")
    label_id = None
    entry_id = None
    try:
        label_data = assert_success(
            await mcp.call_tool(
                "ha_config_set_label", {"name": "E2E Flow Clear Label"}
            ),
            "Create test label",
        )
        label_id = label_data.get("label_id")
        print(f"  label_id = {label_id}")

        create_data = assert_success(
            await mcp.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "min_max",
                    "name": "E2E Flow Clear Labels Helper",
                    "config": {
                        "name": "E2E Flow Clear Labels Helper",
                        "entity_ids": ["sensor.demo_temperature", "sensor.demo_outside_temperature"],
                        "type": "min",
                    },
                    "labels": [label_id],
                },
            ),
            "Create min_max helper with labels",
        )
        entry_id = create_data.get("entry_id")
        entities = create_data.get("entity_ids") or []
        assert entities, f"Flow helper returned no entities: {create_data}"
        target = entities[0]
        print(f"  entry_id = {entry_id}, entity = {target}")

        before = assert_success(
            await mcp.call_tool("ha_get_entity", {"entity_id": target}),
            "Get flow entity before clear",
        )
        assigned_labels = before.get("entity_entry", {}).get("labels") or []
        assert label_id in assigned_labels, f"Label not assigned: {assigned_labels}"
        print(f"  labels on entity after create: {assigned_labels} ✓")

        assert_success(
            await mcp.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "min_max",
                    "helper_id": entry_id,
                    "name": "E2E Flow Clear Labels Helper",
                    "config": {
                        "entity_ids": ["sensor.demo_temperature", "sensor.demo_outside_temperature"],
                        "type": "min",
                    },
                    "labels": [],
                },
            ),
            "Clear flow helper labels",
        )

        after = assert_success(
            await mcp.call_tool("ha_get_entity", {"entity_id": target}),
            "Get flow entity after clear",
        )
        cleared_labels = after.get("entity_entry", {}).get("labels") or []
        assert cleared_labels == [], f"Flow labels not cleared: got {cleared_labels!r}"
        print(f"  labels after clear: {cleared_labels!r} ✓")
        print("  PASS")
        return True
    except AssertionError as e:
        print(f"  FAIL: {e}")
        return False
    finally:
        if entry_id:
            try:
                await mcp.call_tool(
                    "ha_delete_config_entry",
                    {"entry_id": entry_id, "confirm": True},
                )
            except Exception as e:
                print(f"  (cleanup flow entry: {e})")
        if label_id:
            try:
                await mcp.call_tool("ha_config_remove_label", {"label_id": label_id})
            except Exception as e:
                print(f"  (cleanup label: {e})")


async def main():
    base_url = os.environ.get("HA_BASE_URL")
    token = os.environ.get("HA_TOKEN")
    if not base_url or not token:
        print("ERROR: set HA_BASE_URL and HA_TOKEN env vars first.", file=sys.stderr)
        return 2

    print(f"HA: {base_url}")
    ha_client = HomeAssistantClient(base_url=base_url, token=token)
    server = HomeAssistantSmartMCPServer(client=ha_client)
    _ = await server.mcp.list_tools()  # warm up

    async with Client(server.mcp) as mcp:
        r1 = await test_simple_clear_area(mcp)
        r2 = await test_simple_clear_labels(mcp)
        r3 = await test_flow_clear_area(mcp)
        r4 = await test_simple_clear_combined(mcp)
        r5 = await test_flow_clear_labels(mcp)

    total = sum([r1, r2, r3, r4, r5])
    print(f"\n=== RESULT: {total}/5 tests passed ===")
    return 0 if total == 5 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
