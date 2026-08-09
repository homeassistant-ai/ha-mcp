"""Unit tests for ha_config_set_dashboard metadata-update path."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.utils.config_hash import compute_config_hash


class TestSetDashboardMetadataUpdate:
    """Test the metadata update path introduced by merging ha_config_update_dashboard_metadata."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def set_tool(self, mock_client):
        return DashboardConfigTools(mock_client).ha_config_set_dashboard

    def _make_dashboard_list(self, url_path: str, dashboard_id: str = "dash-1"):
        """Helper: mock existing dashboards list response."""
        return {"result": [{"url_path": url_path, "id": dashboard_id}]}

    @pytest.mark.asyncio
    async def test_metadata_updated_true_when_title_provided_for_existing(
        self, set_tool, mock_client
    ):
        """metadata_updated=True when title provided for an existing dashboard."""
        mock_client.send_websocket_message.side_effect = [
            self._make_dashboard_list("my-dashboard"),  # lovelace/dashboards/list
            {"success": True},  # lovelace/dashboards/update (metadata)
            {"result": {"views": []}},  # authoritative post-write config
        ]

        result = await set_tool(url_path="my-dashboard", title="New Title")

        assert result["success"] is True
        assert result["metadata_updated"] is True
        assert result["dashboard_created"] is False

        # Verify the metadata update call was made with correct args
        calls = mock_client.send_websocket_message.call_args_list
        meta_call = calls[1][0][0]
        assert meta_call["type"] == "lovelace/dashboards/update"
        assert meta_call["dashboard_id"] == "dash-1"
        assert meta_call["title"] == "New Title"

    @pytest.mark.asyncio
    async def test_existing_dashboard_with_no_changes_is_rejected(
        self, set_tool, mock_client
    ):
        """An existing dashboard cannot report a successful update with no write."""
        mock_client.send_websocket_message.return_value = self._make_dashboard_list(
            "my-dashboard"
        )

        with pytest.raises(ToolError) as exc_info:
            await set_tool(url_path="my-dashboard")

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["error"]["message"] == "No dashboard changes were requested"
        assert mock_client.send_websocket_message.call_count == 1

    @pytest.mark.asyncio
    async def test_metadata_update_multiple_fields(self, set_tool, mock_client):
        """Multiple metadata fields are sent in a single update call."""
        mock_client.send_websocket_message.side_effect = [
            self._make_dashboard_list("my-dashboard"),
            {"success": True},
            {"result": {"views": []}},
        ]

        result = await set_tool(
            url_path="my-dashboard",
            title="Updated",
            icon="mdi:home",
            require_admin=True,
            show_in_sidebar=False,
        )

        assert result["success"] is True
        assert result["metadata_updated"] is True

        meta_call = mock_client.send_websocket_message.call_args_list[1][0][0]
        assert meta_call["title"] == "Updated"
        assert meta_call["icon"] == "mdi:home"
        assert meta_call["require_admin"] is True
        assert meta_call["show_in_sidebar"] is False

    @pytest.mark.asyncio
    async def test_metadata_update_fails_returns_error(self, set_tool, mock_client):
        """When the metadata update WS call fails, the tool raises ToolError."""
        mock_client.send_websocket_message.side_effect = [
            self._make_dashboard_list("my-dashboard"),
            {"success": False, "error": {"message": "Permission denied"}},
        ]

        with pytest.raises(ToolError) as exc_info:
            await set_tool(url_path="my-dashboard", title="Unauthorized")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "metadata" in error_data["error"]["message"].lower()
        assert "Permission denied" in error_data["error"]["message"]

    @pytest.mark.asyncio
    async def test_blank_view_path_rejected_before_write_on_return_screenshot(
        self, set_tool, mock_client
    ):
        """A blank view_path with return_screenshot fails before any write."""
        with pytest.raises(ToolError) as exc_info:
            await set_tool(
                url_path="my-dashboard",
                config={"views": []},
                return_screenshot=True,
                view_path="   ",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        # Validation is pre-mutation: no dashboard write must have been attempted.
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_metadata_only_update_without_dashboard_id_is_rejected(
        self, set_tool, mock_client
    ):
        """A skipped metadata write cannot report a successful dashboard update."""
        # Lovelace dashboard not in the list (fresh install scenario)
        mock_client.send_websocket_message.return_value = {"result": []}

        with pytest.raises(ToolError) as exc_info:
            await set_tool(url_path="lovelace", title="My Home")

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert body["error"]["message"] == "No dashboard changes were applied"
        assert "no storage ID" in body["error"]["details"]
        assert mock_client.send_websocket_message.call_count == 1

    @pytest.mark.asyncio
    async def test_config_write_with_unavailable_metadata_uses_warning(
        self, set_tool, mock_client
    ):
        """A committed config write reports skipped metadata as a top-level warning."""
        replacement = {"views": [{"title": "Home"}]}
        mock_client.send_websocket_message.side_effect = [
            {"result": []},  # default dashboard is absent from the registry
            {"result": {"views": []}},  # replacement safety/hash pre-read
            {"success": True},  # config save
            {"result": replacement},  # authoritative post-write config
        ]

        result = await set_tool(
            url_path="lovelace", title="My Home", config=replacement
        )

        assert result["success"] is True
        assert result["config_updated"] is True
        assert result["metadata_updated"] is False
        assert "hint" not in result
        metadata_warning = next(
            warning for warning in result["warnings"] if "no storage ID" in warning
        )
        assert "Dashboard config was saved" in metadata_warning

    @pytest.mark.asyncio
    async def test_false_booleans_are_not_filtered_out(self, set_tool, mock_client):
        """False bool values for require_admin/show_in_sidebar must be passed through."""
        mock_client.send_websocket_message.side_effect = [
            self._make_dashboard_list("my-dashboard"),
            {"success": True},
            {"result": {"views": []}},
        ]

        await set_tool(
            url_path="my-dashboard",
            require_admin=False,
            show_in_sidebar=False,
        )

        meta_call = mock_client.send_websocket_message.call_args_list[1][0][0]
        assert meta_call["require_admin"] is False
        assert meta_call["show_in_sidebar"] is False

    @pytest.mark.asyncio
    async def test_full_config_write_uses_authoritative_render_paths(
        self, set_tool, mock_client
    ):
        submitted = {"views": [{"title": "Home", "path": "submitted"}]}
        authoritative = {"views": [{"title": "Home", "path": "normalized"}]}
        mock_client.send_websocket_message.side_effect = [
            self._make_dashboard_list("my-dashboard"),
            {"result": {"views": []}},  # pre-write conflict/size read
            {"success": True},  # lovelace/config/save
            {"result": authoritative},  # authoritative post-write readback
        ]

        result = await set_tool(url_path="my-dashboard", config=submitted)

        assert result["success"] is True
        assert result["render_paths"][0]["view_path"] == "normalized"
        assert result["render_paths"][0]["render_path"] == ("my-dashboard/normalized")
        requests = [
            call.args[0] for call in mock_client.send_websocket_message.call_args_list
        ]
        assert requests[2]["type"] == "lovelace/config/save"
        assert requests[2]["config"] == submitted
        assert requests[3] == {
            "type": "lovelace/config",
            "force": True,
            "url_path": "my-dashboard",
        }


class TestSetDashboardListCallDedup:
    """When the pre-resolver fires (internal-id branch), the existence-check
    site reuses the pre-fetched dashboards list rather than issuing a second
    ``lovelace/dashboards/list`` round-trip.

    The other-branch tests act as regression guards so a future change
    that re-introduces a redundant list call (or accidentally drops the
    one fetch on the canonical-url_path branch) is caught here."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def set_tool(self, mock_client):
        return DashboardConfigTools(mock_client).ha_config_set_dashboard

    @staticmethod
    def _list_call_count(mock_client) -> int:
        return sum(
            1
            for c in mock_client.send_websocket_message.call_args_list
            if c.args and c.args[0].get("type") == "lovelace/dashboards/list"
        )

    @pytest.mark.asyncio
    async def test_internal_id_branch_calls_list_only_once(self, set_tool, mock_client):
        """Pre-resolver fires (hyphenless ``my_dash``) and matches; the
        existence-check site MUST reuse that list instead of fetching
        again. Total ``lovelace/dashboards/list`` calls = 1."""
        dashboards_list = {"result": [{"url_path": "my-dash", "id": "my_dash"}]}
        mock_client.send_websocket_message.side_effect = [
            dashboards_list,  # pre-resolver fetch
            {"success": True},  # metadata update
            {"result": {"views": []}},  # authoritative post-write config
        ]

        result = await set_tool(url_path="my_dash", title="Renamed")

        assert self._list_call_count(mock_client) == 1, (
            "internal-id branch must reuse the pre-resolver's dashboards list"
        )
        assert result["success"] is True
        # Pre-resolver rewrote my_dash -> my-dash; surface marker stays.
        assert result.get("resolved_from") == "my_dash"
        # Metadata update did fire on the canonical url_path.
        meta_call = mock_client.send_websocket_message.call_args_list[1].args[0]
        assert meta_call["type"] == "lovelace/dashboards/update"
        assert meta_call["dashboard_id"] == "my_dash"

    @pytest.mark.asyncio
    async def test_canonical_url_path_branch_still_calls_list_once(
        self, set_tool, mock_client
    ):
        """Already-canonical ``my-dash`` (hyphen present) skips the
        pre-resolver; the existence-check site still fetches once.
        Regression guard: total list calls = 1, not 0 (pre-resolver
        didn't fire) and not 2 (no redundant fetch)."""
        mock_client.send_websocket_message.side_effect = [
            {"result": [{"url_path": "my-dash", "id": "my_dash"}]},
            {"success": True},
            {"result": {"views": []}},
        ]

        result = await set_tool(url_path="my-dash", title="Renamed")

        assert self._list_call_count(mock_client) == 1
        assert result["success"] is True
        assert result.get("resolved_from") is None

    @pytest.mark.asyncio
    async def test_internal_id_no_match_falls_through_to_hyphen_check(
        self, set_tool, mock_client
    ):
        """Hyphenless identifier with no matching dashboard: pre-resolver
        fetches the list, finds no match, ``url_path`` stays
        unchanged, then fails the hyphen-validation check before any
        existence-check fetch can fire. Total list calls = 1."""
        mock_client.send_websocket_message.side_effect = [
            {"result": [{"url_path": "other-dash", "id": "other_dash"}]},
        ]

        with pytest.raises(ToolError) as exc_info:
            await set_tool(url_path="ghost", title="X")

        body = json.loads(str(exc_info.value))
        assert "url_path must contain a hyphen" in body["error"]["message"]
        assert self._list_call_count(mock_client) == 1

    @pytest.mark.asyncio
    async def test_canonical_url_path_branch_rejects_unreadable_registry(
        self, set_tool, mock_client, caplog
    ):
        """An unexpected registry shape is not treated as an empty registry."""
        import logging

        mock_client.send_websocket_message.return_value = "unexpected string"

        with (
            caplog.at_level(
                logging.WARNING, logger="ha_mcp.tools.tools_config_dashboards"
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await set_tool(url_path="my-dash", title="New")

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "dashboard registry" in body["error"]["message"]
        assert body["url_path"] == "my-dash"
        assert mock_client.send_websocket_message.call_count == 1
        assert any(
            "unexpected shape" in rec.message and "type=str" in rec.message
            for rec in caplog.records
        ), (
            f"expected an 'unexpected shape' warning naming the response "
            f"type; got {[rec.message for rec in caplog.records]}"
        )


class TestSetDashboardUrlPathCreationContract:
    """Hyphenless input is exempt only for an exact existing url_path."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def set_tool(self, mock_client):
        return DashboardConfigTools(mock_client).ha_config_set_dashboard

    @pytest.mark.asyncio
    async def test_existing_hyphenless_dashboard_update_is_allowed(
        self, set_tool, mock_client
    ):
        mock_client.send_websocket_message.side_effect = [
            {"result": [{"url_path": "map", "id": "map"}]},
            {"success": True},
            {"result": {"views": []}},
        ]

        result = await set_tool(url_path="map", title="Updated Map")

        assert result["success"] is True
        assert result["action"] == "update"
        assert result["url_path"] == "map"
        assert result["dashboard_created"] is False
        assert "resolved_from" not in result
        assert mock_client.send_websocket_message.call_args_list[1].args[0]["type"] == (
            "lovelace/dashboards/update"
        )

    @pytest.mark.asyncio
    async def test_existing_hyphenated_dashboard_update_is_allowed(
        self, set_tool, mock_client
    ):
        mock_client.send_websocket_message.side_effect = [
            {"result": [{"url_path": "floor-map", "id": "floor_map"}]},
            {"success": True},
            {"result": {"views": []}},
        ]

        result = await set_tool(url_path="floor-map", title="Updated Floor Map")

        assert result["success"] is True
        assert result["action"] == "update"
        assert result["dashboard_created"] is False
        assert mock_client.send_websocket_message.call_args_list[1].args[0]["type"] == (
            "lovelace/dashboards/update"
        )

    @pytest.mark.asyncio
    async def test_new_hyphenless_dashboard_is_rejected(self, set_tool, mock_client):
        mock_client.send_websocket_message.return_value = {"result": []}

        with pytest.raises(ToolError) as exc_info:
            await set_tool(url_path="newmap", title="New Map")

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "url_path must contain a hyphen" in body["error"]["message"]
        assert all(
            "Try 'newmap'" not in suggestion
            for suggestion in body["error"]["suggestions"]
        )
        assert mock_client.send_websocket_message.call_count == 1

    @pytest.mark.asyncio
    async def test_internal_id_match_cannot_exempt_other_hyphenless_path(
        self, set_tool, mock_client
    ):
        mock_client.send_websocket_message.return_value = {
            "result": [{"url_path": "map", "id": "map_internal"}]
        }

        with pytest.raises(ToolError) as exc_info:
            await set_tool(url_path="map_internal", title="Updated Map")

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "url_path must contain a hyphen" in body["error"]["message"]
        assert body["url_path"] == "map_internal"
        assert body["error"]["suggestion"] == "Try 'map-internal' instead"
        assert "Try 'map' instead" not in body["error"]["suggestions"]
        assert mock_client.send_websocket_message.call_count == 1

    @pytest.mark.asyncio
    async def test_unreadable_registry_is_not_reported_as_bad_hyphenless_input(
        self, set_tool, mock_client
    ):
        mock_client.send_websocket_message.return_value = "unexpected string"

        with pytest.raises(ToolError) as exc_info:
            await set_tool(url_path="map", title="Updated Map")

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "dashboard registry" in body["error"]["message"]
        assert "hyphen" not in body["error"]["message"]
        assert body["url_path"] == "map"
        assert mock_client.send_websocket_message.call_count == 1

    @pytest.mark.asyncio
    async def test_new_hyphenated_dashboard_is_allowed(self, set_tool, mock_client):
        mock_client.send_websocket_message.side_effect = [
            {"result": []},
            {"success": True, "result": {"id": "new_map"}},
            {"result": {"views": []}},
        ]

        result = await set_tool(url_path="new-map", title="New Map")

        assert result["success"] is True
        assert result["action"] == "create"
        assert result["dashboard_created"] is True
        assert mock_client.send_websocket_message.call_args_list[1].args[0]["type"] == (
            "lovelace/dashboards/create"
        )

    @pytest.mark.asyncio
    async def test_existing_hyphenless_dashboard_full_config_update_is_allowed(
        self, set_tool, mock_client
    ):
        replacement = {"views": [{"title": "Updated"}]}
        mock_client.send_websocket_message.side_effect = [
            {"result": [{"url_path": "panel", "id": "panel"}]},
            {"result": {"views": []}},
            {"success": True},
            {"result": replacement},
        ]

        result = await set_tool(url_path="panel", config=replacement)

        assert result["success"] is True
        assert result["action"] == "update"
        assert result["config_updated"] is True
        assert "resolved_from" not in result
        save_call = mock_client.send_websocket_message.call_args_list[2].args[0]
        assert save_call["type"] == "lovelace/config/save"
        assert save_call["config"] == replacement

    @pytest.mark.asyncio
    async def test_existing_hyphenless_dashboard_python_transform_is_allowed(
        self, set_tool, mock_client
    ):
        current = {"views": []}
        transformed = {"views": [{"title": "Added"}]}
        mock_client.send_websocket_message.side_effect = [
            {"result": [{"url_path": "panel", "id": "panel"}]},
            {"result": current},
            {"success": True},
            {"result": transformed},
        ]

        result = await set_tool(
            url_path="panel",
            python_transform='config["views"].append({"title": "Added"})',
            config_hash=compute_config_hash(current),
        )

        assert result["success"] is True
        assert result["action"] == "python_transform"
        assert "resolved_from" not in result
        save_call = mock_client.send_websocket_message.call_args_list[2].args[0]
        assert save_call["type"] == "lovelace/config/save"
        assert save_call["config"] == transformed

    @pytest.mark.asyncio
    async def test_full_config_cannot_take_control_of_strategy_dashboard(
        self, set_tool, mock_client
    ):
        mock_client.send_websocket_message.side_effect = [
            {"result": [{"url_path": "map", "id": "map"}]},
            {"result": {"strategy": {"type": "map"}}},
        ]

        with pytest.raises(ToolError) as exc_info:
            await set_tool(url_path="map", config={"views": []})

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert "cannot be converted" in body["error"]["message"]
        assert "Take Control" in body["error"]["suggestion"]
        assert mock_client.send_websocket_message.call_count == 2

    @pytest.mark.asyncio
    async def test_full_config_pre_read_failure_blocks_unverified_replacement(
        self, set_tool, mock_client
    ):
        mock_client.send_websocket_message.side_effect = [
            {"result": [{"url_path": "map", "id": "map"}]},
            {"success": False, "error": {"message": "temporary read failure"}},
        ]

        with pytest.raises(ToolError) as exc_info:
            await set_tool(url_path="map", config={"views": []})

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "Cannot verify" in body["error"]["message"]
        assert "temporary read failure" in body["error"]["details"]
        assert body["url_path"] == "map"
        assert mock_client.send_websocket_message.call_count == 2

    @pytest.mark.asyncio
    async def test_python_transform_cannot_take_control_of_strategy_dashboard(
        self, set_tool, mock_client
    ):
        current = {"strategy": {"type": "map"}}
        mock_client.send_websocket_message.side_effect = [
            {"result": [{"url_path": "map", "id": "map"}]},
            {"result": current},
        ]

        with pytest.raises(ToolError) as exc_info:
            await set_tool(
                url_path="map",
                python_transform='config = {"views": []}',
                config_hash=compute_config_hash(current),
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert "cannot be converted" in body["error"]["message"]
        assert body["action"] == "python_transform"
        assert mock_client.send_websocket_message.call_count == 2
