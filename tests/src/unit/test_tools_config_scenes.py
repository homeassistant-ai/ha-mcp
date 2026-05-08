"""Unit tests for Scene configuration tools.

Validates the input-validation gates and python_transform flow on the
scene CRUD tools (issue #995). Mirrors test_tools_config_scripts.py shape;
the key shape difference is that scene ``entities`` is a dict, not a list.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_config_scenes import ConfigSceneTools


@pytest.fixture
def mock_client():
    """Mock client that satisfies the upsert / get / reference-validator paths."""
    client = MagicMock()
    client.upsert_scene_config = AsyncMock(
        return_value={"success": True, "scene_id": "test_scene"}
    )
    client.get_scene_config = AsyncMock(
        return_value={
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
    )
    client.delete_scene_config = AsyncMock(
        return_value={"success": True, "scene_id": "test_scene"}
    )
    client.get_entity_state = AsyncMock(
        return_value={
            "state": "2026-05-08T07:00:00+00:00",  # scenes' state is the last-activated ISO timestamp
            "entity_id": "scene.test_scene",
        }
    )
    # validate_config_references reaches for these — keep them empty-but-callable.
    client.get_services = AsyncMock(return_value=[])
    client.get_states = AsyncMock(return_value=[])
    return client


@pytest.fixture
def tools(mock_client):
    return ConfigSceneTools(mock_client)


class TestSceneToolsValidation:
    """Validation gates on ha_config_set_scene."""

    async def test_set_scene_missing_entities_field(self, tools):
        """A config without 'entities' is rejected with a structured error."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={"name": "No entities"},  # Missing 'entities'
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "entities" in error_data["error"]["message"].lower()

    async def test_set_scene_entities_must_be_dict_not_list(self, tools):
        """Scene shape: ``entities`` is a dict — list rejected (the script→scene confusion vector)."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={
                    "name": "Wrong shape",
                    # Common LLM-misroute: list-of-actions like an automation
                    "entities": [{"entity_id": "light.kitchen", "state": "on"}],
                },
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        msg = error_data["error"]["message"]
        assert "dict" in msg.lower() and "entities" in msg.lower()

    async def test_set_scene_with_entities_dict_succeeds(self, tools, mock_client):
        """Happy path: a dict-shaped 'entities' is accepted."""
        result = await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={
                "name": "Test Scene",
                "entities": {
                    "light.kitchen": {"state": "on", "brightness": 200},
                },
            },
            wait=False,
        )

        assert result["success"] is True
        mock_client.upsert_scene_config.assert_called_once()

    async def test_set_scene_config_and_python_transform_mutex(self, tools):
        """Passing both config and python_transform is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={"entities": {"light.kitchen": {"state": "on"}}},
                python_transform="config['entities'].clear()",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "both" in error_data["error"]["message"].lower()

    async def test_set_scene_neither_config_nor_transform(self, tools):
        """Calling with neither config nor python_transform is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(scene_id="test_scene")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False

    async def test_set_scene_python_transform_requires_config_hash(self, tools):
        """python_transform without config_hash is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                python_transform="config['entities']['light.kitchen']['brightness'] = 100",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "config_hash" in error_data["error"]["message"]

    async def test_set_scene_invalid_json_string(self, tools):
        """A malformed JSON string for config surfaces a parse error."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config="{not valid json",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "config" in error_data["error"]["message"].lower()

    async def test_set_scene_config_must_be_object(self, tools):
        """A JSON-array config (not an object) is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config="[1, 2, 3]",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        msg = error_data["error"]["message"].lower()
        assert "object" in msg or "dict" in msg


class TestScenePythonTransform:
    """Coverage for the python_transform code path."""

    async def test_transform_updates_entity_state(self, tools, mock_client):
        """A successful transform routes through the upsert path."""
        # First call (hash verify) returns the seed config.
        # Second call (re-fetch authoritative hash) returns the same shape.
        seed = {
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on", "brightness": 100}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)

        # Compute the hash the tool will see on the verify step.
        from ha_mcp.utils.config_hash import compute_config_hash

        seed_hash = compute_config_hash(seed)

        result = await tools.ha_config_set_scene(
            scene_id="test_scene",
            python_transform=(
                "config['entities']['light.kitchen']['brightness'] = 200"
            ),
            config_hash=seed_hash,
        )

        assert result["success"] is True
        assert result["action"] == "python_transform"

        # The transform was applied before upsert: verify the brightness change.
        upsert_call_args = mock_client.upsert_scene_config.call_args
        config_passed = upsert_call_args[0][0]
        assert config_passed["entities"]["light.kitchen"]["brightness"] == 200

    async def test_transform_hash_mismatch_raises_conflict(self, tools, mock_client):
        """A stale config_hash on python_transform surfaces a conflict error."""
        seed = {
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                python_transform="config['entities']['light.kitchen']['state'] = 'off'",
                config_hash="stale-hash-that-doesnt-match",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "modified" in error_data["error"]["message"].lower()

    async def test_transform_must_keep_entities_dict_shape(self, tools, mock_client):
        """A transform that drops the entities key fails the post-transform check."""
        seed = {
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)

        from ha_mcp.utils.config_hash import compute_config_hash

        seed_hash = compute_config_hash(seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                python_transform="del config['entities']",
                config_hash=seed_hash,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "entities" in error_data["error"]["message"].lower()
