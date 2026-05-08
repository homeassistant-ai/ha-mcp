"""End-to-end tests for Home Assistant Scene Configuration tools (issue #995).

Validates the lifecycle of ha_config_get_scene / ha_config_set_scene /
ha_config_remove_scene against a real Home Assistant test container:

- Create a scene with a dict-shaped ``entities`` field
- Retrieve and verify ``config_hash`` is stable across reads
- Full-config replacement
- python_transform-based surgical edits
- Optimistic locking on stale config_hash
- Delete and verify removal

Mirrors the shape of ``tests/src/e2e/workflows/scripts/test_lifecycle.py``;
the key shape difference is that scene ``entities`` is a dict keyed by
entity_id, not a list of actions.
"""

import asyncio
import json
import logging
import time
from typing import Any

import pytest

from ...utilities.assertions import safe_call_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _parse_mcp_result(result) -> dict[str, Any]:
    """Parse an MCP tool-call result into a dict, with Python-literal fallback."""
    try:
        if hasattr(result, "content") and result.content:
            response_text = str(result.content[0].text)
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                try:
                    fixed_text = (
                        response_text.replace("true", "True")
                        .replace("false", "False")
                        .replace("null", "None")
                    )
                    return eval(fixed_text)
                except (SyntaxError, NameError, ValueError):
                    return {"raw_response": response_text, "parse_error": True}

        return {
            "content": (
                str(result.content[0]) if hasattr(result, "content") else str(result)
            )
        }
    except Exception as e:
        logger.warning(f"Failed to parse MCP result: {e}")
        return {"error": "Failed to parse result", "exception": str(e)}


def _extract_scene_config(get_data: dict[str, Any]) -> dict[str, Any]:
    """Pull the inner scene body out of the get response.

    The get response wraps the scene config as ``{"config": {"config": <body>}}``
    (REST-wrapper inside the tool wrapper). Mirror the script extractor.
    """
    config_wrapper = get_data.get("config", {})
    if isinstance(config_wrapper, dict) and "config" in config_wrapper:
        return config_wrapper.get("config", {})
    return config_wrapper


async def _wait_for_scene_registered(
    mcp_client, scene_id: str, timeout: int = 15, poll_interval: float = 1.0
) -> bool:
    """Poll until the scene is queryable via the management API or state API."""
    start_time = time.time()
    scene_entity = f"scene.{scene_id}"
    while time.time() - start_time < timeout:
        try:
            get_result = await mcp_client.call_tool(
                "ha_config_get_scene", {"scene_id": scene_id}
            )
            get_data = _parse_mcp_result(get_result)
            if get_data.get("success") and get_data.get("config"):
                return True

            state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": scene_entity}
            )
            state_data = _parse_mcp_result(state_result)
            if state_data.get("success"):
                return True
        except Exception as e:
            logger.debug(f"Scene registration check failed: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(f"⚠️ Scene {scene_entity} was not registered within {timeout}s")
    return False


async def _wait_for_scene_removed(
    mcp_client, scene_id: str, timeout: int = 15, poll_interval: float = 1.0
) -> bool:
    """Poll until the scene is no longer queryable."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            get_result = await mcp_client.call_tool(
                "ha_config_get_scene", {"scene_id": scene_id}
            )
            get_data = _parse_mcp_result(get_result)
            # Either success=False OR an empty/missing config means it's gone.
            if not get_data.get("success") or not get_data.get("config"):
                return True
        except Exception:
            return True
        await asyncio.sleep(poll_interval)
    return False


def _make_test_scene_config(name: str, **overrides: Any) -> dict[str, Any]:
    """Standard test scene shape: dict-keyed entities + a couple of attributes."""
    config = {
        "name": f"E2E {name}",
        "icon": "mdi:flask",
        "entities": {
            "light.bed_light": {"state": "on", "brightness": 200},
        },
    }
    config.update(overrides)
    return config


@pytest.mark.cleanup
class TestSceneLifecycle:
    """End-to-end coverage for the scene CRUD tools."""

    async def test_scene_basic_lifecycle(self, mcp_client, cleanup_tracker):
        """Create → get → update → delete with stable hash semantics."""
        scene_id = "test_basic_e2e_scene"
        cleanup_tracker.track("scene", f"scene.{scene_id}")

        # 1. Create
        create_result = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": _make_test_scene_config("Basic"),
                "wait": True,
            },
        )
        create_data = _parse_mcp_result(create_result)
        assert create_data.get("success") is True, f"Create failed: {create_data}"

        registered = await _wait_for_scene_registered(mcp_client, scene_id)
        assert registered, f"Scene {scene_id} not registered after create"

        # 2. Get + verify config_hash stability
        get_result_1 = await safe_call_tool(
            mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
        )
        get_data_1 = _parse_mcp_result(get_result_1)
        assert get_data_1.get("success") is True
        body_1 = _extract_scene_config(get_data_1)
        assert "entities" in body_1
        assert "light.bed_light" in body_1["entities"]
        hash_1 = get_data_1.get("config_hash")
        assert hash_1, "config_hash missing on first get"

        get_result_2 = await safe_call_tool(
            mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
        )
        get_data_2 = _parse_mcp_result(get_result_2)
        assert get_data_2.get("config_hash") == hash_1, (
            "config_hash should be stable across reads of an unchanged scene"
        )

        # 3. Full-config replacement
        replacement = _make_test_scene_config(
            "Basic Updated",
            entities={
                "light.bed_light": {"state": "on", "brightness": 80},
                "light.kitchen_lights": {"state": "off"},
            },
        )
        update_result = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": replacement,
                "config_hash": hash_1,
                "wait": True,
            },
        )
        update_data = _parse_mcp_result(update_result)
        assert update_data.get("success") is True

        get_result_3 = await safe_call_tool(
            mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
        )
        get_data_3 = _parse_mcp_result(get_result_3)
        body_3 = _extract_scene_config(get_data_3)
        assert "light.kitchen_lights" in body_3.get("entities", {})

        # 4. Delete
        delete_result = await safe_call_tool(
            mcp_client, "ha_config_remove_scene", {"scene_id": scene_id, "wait": True}
        )
        delete_data = _parse_mcp_result(delete_result)
        # 405 on add-on / YAML-mode is an acceptable outcome — tested separately.
        if delete_data.get("success"):
            removed = await _wait_for_scene_removed(mcp_client, scene_id)
            assert removed, f"Scene {scene_id} still queryable after delete"

    async def test_scene_python_transform_surgical_edit(
        self, mcp_client, cleanup_tracker
    ):
        """python_transform updates a single entity's state without full replacement."""
        scene_id = "test_transform_e2e_scene"
        cleanup_tracker.track("scene", f"scene.{scene_id}")

        # Seed
        await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": _make_test_scene_config(
                    "Transform",
                    entities={
                        "light.bed_light": {"state": "on", "brightness": 100},
                    },
                ),
                "wait": True,
            },
        )
        await _wait_for_scene_registered(mcp_client, scene_id)

        get_result = await safe_call_tool(
            mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
        )
        get_data = _parse_mcp_result(get_result)
        scene_hash = get_data.get("config_hash")
        assert scene_hash

        # Surgical edit: bump brightness
        transform_result = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "python_transform": (
                    "config['entities']['light.bed_light']['brightness'] = 220"
                ),
                "config_hash": scene_hash,
            },
        )
        transform_data = _parse_mcp_result(transform_result)
        assert transform_data.get("success") is True
        assert transform_data.get("action") == "python_transform"

        # Verify the change landed
        verify_result = await safe_call_tool(
            mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
        )
        verify_data = _parse_mcp_result(verify_result)
        body = _extract_scene_config(verify_data)
        assert (
            body.get("entities", {}).get("light.bed_light", {}).get("brightness")
            == 220
        )

        # Cleanup
        await safe_call_tool(
            mcp_client, "ha_config_remove_scene", {"scene_id": scene_id, "wait": False}
        )

    async def test_scene_python_transform_rejects_stale_hash(
        self, mcp_client, cleanup_tracker
    ):
        """A python_transform call with a stale config_hash returns a conflict error."""
        scene_id = "test_stale_hash_e2e_scene"
        cleanup_tracker.track("scene", f"scene.{scene_id}")

        await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": _make_test_scene_config("Stale"),
                "wait": True,
            },
        )
        await _wait_for_scene_registered(mcp_client, scene_id)

        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "python_transform": (
                    "config['entities']['light.bed_light']['brightness'] = 50"
                ),
                "config_hash": "stale-hash-that-doesnt-match",
            },
        )
        data = _parse_mcp_result(result)
        # The tool surfaces the conflict as a structured error.
        assert data.get("success") is False
        err = data.get("error") or {}
        assert "modified" in (err.get("message") or "").lower() or (
            "conflict" in str(data).lower()
        )

        # Cleanup
        await safe_call_tool(
            mcp_client, "ha_config_remove_scene", {"scene_id": scene_id, "wait": False}
        )

    async def test_set_scene_rejects_list_shaped_entities(
        self, mcp_client, cleanup_tracker
    ):
        """Common LLM-misroute: list-shaped entities (the automation/script confusion) is rejected upfront."""
        scene_id = "test_wrong_shape_e2e_scene"
        cleanup_tracker.track("scene", f"scene.{scene_id}")

        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": {
                    "name": "Wrong shape",
                    # Looks like an automation action list — must be rejected.
                    "entities": [
                        {"entity_id": "light.bed_light", "state": "on"}
                    ],
                },
            },
        )
        data = _parse_mcp_result(result)
        assert data.get("success") is False
        err = data.get("error") or {}
        msg = (err.get("message") or "").lower()
        assert "dict" in msg and "entities" in msg
