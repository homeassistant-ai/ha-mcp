"""Regression tests for issue #1297 D1 error-shape consistency work.

Label / category / device not-found maps to ``RESOURCE_NOT_FOUND``, not
``ENTITY_NOT_FOUND``. These are registry metadata (labels, categories) or
their own non-entity registry (devices), so the prior ``ENTITY_NOT_FOUND``
auto-routed agent retry to ``ha_search_entities()`` — which doesn't list
any of them, creating a wrong-tool spiral.

The complementary D2 work (dashboards / dashboard-resource not-found
suggestions) landed via #1386 and is covered there by
``TestDeleteDashboardNotFoundShape`` in ``test_tools_config_dashboards.py``.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError


@pytest.fixture
def mock_ws_client():
    """Mock client with an AsyncMock send_websocket_message ready for per-test programming."""
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    return client


def _all_suggestions(error_payload: dict[str, Any]) -> list[str]:
    """Collect every suggestion regardless of which field holds it.

    ``create_error_response`` writes the first suggestion into the singular
    ``suggestion`` key and only emits the plural ``suggestions`` list when
    there are two or more — so a single-suggestion caller produces only
    ``suggestion``. Tests need to look at both.
    """
    singular = error_payload.get("suggestion")
    plural = error_payload.get("suggestions") or []
    return ([singular] if singular else []) + list(plural)


# ---------------------------------------------------------------------------
# D1 — Label / Category get-by-id → RESOURCE_NOT_FOUND (was ENTITY_NOT_FOUND)
# ---------------------------------------------------------------------------


class TestLabelGetMissingReturnsResourceNotFound:
    """Regression: ha_config_get_label(missing) must surface
    ``RESOURCE_NOT_FOUND``. Pre-#1297 it surfaced ``ENTITY_NOT_FOUND``,
    routing agents to the entity-search path for non-entity metadata.
    """

    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_labels import LabelTools

        return LabelTools(mock_ws_client)

    async def test_missing_label_id_surfaces_resource_not_found(
        self, tools, mock_ws_client
    ):
        mock_ws_client.send_websocket_message.return_value = {
            "success": True,
            "result": [{"label_id": "existing", "name": "Existing"}],
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_label(label_id="missing")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND", (
            "Labels are registry metadata, not entities — must classify as "
            "RESOURCE_NOT_FOUND so agents route to ha_config_get_label() "
            "instead of ha_search_entities()."
        )
        # The list-tool recovery suggestion (already pre-#1297) must survive.
        assert any(
            "ha_config_get_label" in s
            for s in _all_suggestions(error_data["error"])
        )
        # The available_label_ids surface (pre-#1297) must survive too.
        assert "available_label_ids" in error_data


class TestCategoryGetMissingReturnsResourceNotFound:
    """Regression: ha_config_get_category(scope, missing) must surface
    ``RESOURCE_NOT_FOUND``. Same reasoning as labels.
    """

    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_categories import CategoryTools

        return CategoryTools(mock_ws_client)

    async def test_missing_category_id_surfaces_resource_not_found(
        self, tools, mock_ws_client
    ):
        mock_ws_client.send_websocket_message.return_value = {
            "success": True,
            "result": [{"category_id": "existing", "name": "Existing"}],
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_category(
                scope="automation", category_id="missing"
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND", (
            "Categories are registry metadata, not entities — must classify "
            "as RESOURCE_NOT_FOUND."
        )
        assert any(
            "ha_config_get_category" in s
            for s in _all_suggestions(error_data["error"])
        )
        assert "available_category_ids" in error_data
        assert error_data["scope"] == "automation"


def _register_registry_tools_and_capture(mock_client):
    """Closure-pattern capture for tools_registry (same shape as tools_config_dashboards)."""
    from ha_mcp.tools.tools_registry import register_registry_tools

    mock_mcp = MagicMock()
    captured: dict[str, Any] = {}

    def fake_tool(**kwargs):
        def decorator(fn):
            captured[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp.tool = fake_tool
    register_registry_tools(mock_mcp, mock_client)
    return captured


class TestDeviceLookupMissingReturnsResourceNotFound:
    """Sibling-bug fix discovered during the #1297 cross-family audit:
    ``ha_get_device(device_id=missing)`` and ``ha_remove_device(device_id=missing)``
    previously raised ``ENTITY_NOT_FOUND``. Devices are NOT entities — they live
    in the device registry, addressed by device_id (UUID), with their own
    suggestion (``ha_get_device()``). Same mis-classification class as the
    labels/categories sites above.
    """

    async def test_get_device_missing_id_surfaces_resource_not_found(
        self, mock_ws_client
    ):
        # Two list-registry calls: device_registry/list + entity_registry/list.
        # Return empty for both so the lookup misses cleanly.
        mock_ws_client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True, "result": []},  # device_registry/list
                {"success": True, "result": []},  # entity_registry/list
            ]
        )
        captured = _register_registry_tools_and_capture(mock_ws_client)
        ha_get_device = captured["ha_get_device"]

        with pytest.raises(ToolError) as exc_info:
            await ha_get_device(device_id="missing-device-uuid")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND", (
            "Devices are not entities — must classify as RESOURCE_NOT_FOUND."
        )

    async def test_remove_device_missing_id_surfaces_resource_not_found(
        self, mock_ws_client
    ):
        mock_ws_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        captured = _register_registry_tools_and_capture(mock_ws_client)
        ha_remove_device = captured["ha_remove_device"]

        with pytest.raises(ToolError) as exc_info:
            await ha_remove_device(device_id="missing-device-uuid")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND", (
            "Devices are not entities — must classify as RESOURCE_NOT_FOUND."
        )
