"""Pagination contract for the three collection-listing tools (issue #1869).

``ha_config_list_groups`` / ``ha_config_list_dashboard_resources`` /
``ha_config_list_helpers`` returned their whole collection on every call. They
now take ``limit``/``offset`` and emit the shared ``build_pagination_metadata``
envelope, joining ``ha_get_history`` / ``ha_list_services`` / ``ha_search``.

Pinned here because the contract is cross-tool and easy to break in one place
only: ``count`` is the page size (``total_count`` carries the full size), the
records below the default limit stay exactly what they were, and the aggregate
summaries that describe the whole collection (``inline_count`` / ``by_type``)
must NOT shrink to the page.

The helper suite covers both routes that can serve a listing — the legacy
``{type}/list`` body and the component's merged all-types mode — because each
builds its own envelope and the slice is applied at one shared normalization
point rather than in each builder.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp.tools import component_api, tools_config_helpers
from ha_mcp.tools.tools_config_helpers import register_config_helper_tools
from ha_mcp.tools.tools_groups import GroupTools
from ha_mcp.tools.tools_resources import ResourceTools

from ._component_routing_helpers import make_ws, patch_ws

_PAGINATION_KEYS = {
    "total_count",
    "offset",
    "limit",
    "count",
    "has_more",
    "next_offset",
}


def _group_states(count: int) -> list[dict[str, Any]]:
    """``get_states``-shaped payload with ``count`` groups, sorted by name."""
    return [
        {
            "entity_id": f"group.g{i:03d}",
            "state": "on",
            "attributes": {"friendly_name": f"Group {i:03d}", "entity_id": []},
        }
        for i in range(count)
    ]


def _resource_records(count: int) -> list[dict[str, Any]]:
    return [
        {"id": f"res{i:03d}", "url": f"/local/card{i:03d}.js", "type": "module"}
        for i in range(count)
    ]


def _legacy_helper_items(count: int) -> list[dict[str, Any]]:
    return [{"id": f"h{i:03d}", "name": f"Helper {i:03d}"} for i in range(count)]


class _LegacyHelperClient:
    """Uncredentialed HA client: no base_url/token ⇒ caps probe returns None.

    That pins ``ha_config_list_helpers`` to its legacy ``{type}/list`` body
    without patching the WS layer — the component path is negotiated only for a
    credentialed client (``component_api.get_component_caps``).
    """

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") == "config/entity_registry/list":
            return {"success": True, "result": []}
        if msg.get("type") == "input_boolean/list":
            return {"success": True, "result": [dict(i) for i in self._items]}
        return {"success": False, "error": "unexpected list type"}


def _build_list_helpers(client: Any) -> Any:
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        registered[method.__fastmcp__.name] = method

    mcp = MagicMock()
    mcp.add_tool = capture_add_tool
    register_config_helper_tools(mcp, client)
    return registered["ha_config_list_helpers"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


class TestListGroupsPagination:
    def _tools(self, count: int) -> GroupTools:
        client = MagicMock()

        async def _get_states() -> list[dict[str, Any]]:
            return _group_states(count)

        client.get_states = _get_states
        return GroupTools(client)

    async def test_collection_below_limit_is_unchanged_plus_metadata(self):
        result = await self._tools(3).ha_config_list_groups()

        assert [g["entity_id"] for g in result["groups"]] == [
            "group.g000",
            "group.g001",
            "group.g002",
        ]
        assert result["count"] == 3
        assert result["total_count"] == 3
        assert result["has_more"] is False
        assert result["next_offset"] is None

    async def test_first_page_reports_more(self):
        result = await self._tools(250).ha_config_list_groups()

        assert len(result["groups"]) == 100
        assert result["count"] == 100
        assert result["total_count"] == 250
        assert result["limit"] == 100
        assert result["offset"] == 0
        assert result["has_more"] is True
        assert result["next_offset"] == 100

    async def test_offset_selects_the_next_window(self):
        result = await self._tools(250).ha_config_list_groups(limit=100, offset=100)

        assert result["groups"][0]["entity_id"] == "group.g100"
        assert result["count"] == 100
        assert result["next_offset"] == 200

    async def test_last_page_is_short_and_final(self):
        result = await self._tools(250).ha_config_list_groups(limit=100, offset=200)

        assert result["count"] == 50
        assert result["has_more"] is False
        assert result["next_offset"] is None

    async def test_offset_past_the_end_yields_an_empty_final_page(self):
        result = await self._tools(5).ha_config_list_groups(offset=500)

        assert result["groups"] == []
        assert result["count"] == 0
        assert result["total_count"] == 5
        assert result["has_more"] is False
        assert result["next_offset"] is None

    async def test_message_reports_the_full_collection_not_the_page(self):
        result = await self._tools(250).ha_config_list_groups()

        assert result["message"] == "Found 250 group(s)"


class TestListDashboardResourcesPagination:
    def _tools(self, count: int) -> ResourceTools:
        client = MagicMock()

        async def _send(msg: dict[str, Any]) -> dict[str, Any]:
            return {"result": _resource_records(count)}

        client.send_websocket_message = _send
        return ResourceTools(client)

    async def test_first_page_reports_more(self):
        result = await self._tools(250).ha_config_list_dashboard_resources()

        assert len(result["resources"]) == 100
        assert result["count"] == 100
        assert result["total_count"] == 250
        assert result["has_more"] is True
        assert result["next_offset"] == 100

    async def test_offset_selects_the_next_window(self):
        result = await self._tools(250).ha_config_list_dashboard_resources(
            limit=10, offset=100
        )

        assert [r["id"] for r in result["resources"]][:2] == ["res100", "res101"]
        assert result["count"] == 10

    async def test_aggregate_summaries_describe_the_whole_collection(self):
        """``by_type``/``inline_count`` must not shrink to the returned page."""
        result = await self._tools(250).ha_config_list_dashboard_resources(limit=10)

        assert len(result["resources"]) == 10
        assert result["by_type"]["module"] == 250
        assert result["inline_count"] == 0


class TestListHelpersPagination:
    async def test_legacy_path_paginates(self):
        client = _LegacyHelperClient(_legacy_helper_items(250))
        list_helpers = _build_list_helpers(client)

        result = await list_helpers("input_boolean", limit=100, offset=100)

        assert len(result["helpers"]) == 100
        assert result["helpers"][0]["id"] == "h100"
        assert result["count"] == 100
        assert result["total_count"] == 250
        assert result["has_more"] is True
        assert result["next_offset"] == 200

    async def test_legacy_path_below_limit_is_unchanged_plus_metadata(self):
        client = _LegacyHelperClient(_legacy_helper_items(2))
        list_helpers = _build_list_helpers(client)

        result = await list_helpers("input_boolean")

        assert [h["id"] for h in result["helpers"]] == ["h000", "h001"]
        assert result["count"] == 2
        assert result["total_count"] == 2
        assert result["has_more"] is False
        assert set(result) >= _PAGINATION_KEYS

    async def test_all_types_mode_paginates_the_merged_listing(self):
        """All-types is component-served and merges legacy ``tag`` records in.

        The slice must therefore run after that merge, not inside the component
        response — otherwise the merged-in records would land outside the page.
        """
        component_result = {
            "helpers": [
                {
                    "helper_type": "input_boolean",
                    "object_id": f"b{i:03d}",
                    "entity_id": f"input_boolean.b{i:03d}",
                    "name": f"Bool {i:03d}",
                    "kind": "collection",
                    "config": {"id": f"b{i:03d}"},
                }
                for i in range(120)
            ],
            "count": 120,
            "covered_types": sorted(
                tools_config_helpers.SIMPLE_HELPER_TYPES - {"tag"}
                | tools_config_helpers.FLOW_HELPER_TYPES
            ),
        }
        caps = {
            "schema_version": 1,
            "component_version": "1.1.0",
            "capabilities": ["helpers_list"],
            "limits": {},
        }

        class _AllClient:
            base_url = "http://ha.local:8123"
            token = "tok"

            async def send_websocket_message(
                self, msg: dict[str, Any]
            ) -> dict[str, Any]:
                if msg.get("type") == "tag/list":
                    return {
                        "success": True,
                        "result": [{"id": "tag-42", "name": "Front Door"}],
                    }
                return {"success": True, "result": []}

        client = _AllClient()
        ws = make_ws(
            "ha_mcp_tools/helpers_list",
            info_result=caps,
            cmd_result=component_result,
        )
        with patch_ws(ws, tools_config_helpers):
            list_helpers = _build_list_helpers(client)
            result = await list_helpers("all", limit=100)

        # 120 component records + 1 legacy-merged tag = 121 total, page of 100.
        assert result["total_count"] == 121
        assert result["count"] == 100
        assert len(result["helpers"]) == 100
        assert result["has_more"] is True
        assert result["next_offset"] == 100

    async def test_all_types_mode_last_page_holds_the_merged_tag_record(self):
        """The legacy-merged record is reachable via offset, not stranded."""
        component_result = {
            "helpers": [
                {
                    "helper_type": "input_boolean",
                    "object_id": f"b{i:03d}",
                    "entity_id": f"input_boolean.b{i:03d}",
                    "name": f"Bool {i:03d}",
                    "kind": "collection",
                    "config": {"id": f"b{i:03d}"},
                }
                for i in range(120)
            ],
            "count": 120,
            "covered_types": sorted(
                tools_config_helpers.SIMPLE_HELPER_TYPES - {"tag"}
                | tools_config_helpers.FLOW_HELPER_TYPES
            ),
        }
        caps = {
            "schema_version": 1,
            "component_version": "1.1.0",
            "capabilities": ["helpers_list"],
            "limits": {},
        }

        class _AllClient:
            base_url = "http://ha.local:8123"
            token = "tok"

            async def send_websocket_message(
                self, msg: dict[str, Any]
            ) -> dict[str, Any]:
                if msg.get("type") == "tag/list":
                    return {
                        "success": True,
                        "result": [{"id": "tag-42", "name": "Front Door"}],
                    }
                return {"success": True, "result": []}

        ws = make_ws(
            "ha_mcp_tools/helpers_list",
            info_result=caps,
            cmd_result=component_result,
        )
        with patch_ws(ws, tools_config_helpers):
            list_helpers = _build_list_helpers(_AllClient())
            result = await list_helpers("all", limit=100, offset=100)

        assert result["count"] == 21
        assert result["helpers"][-1]["id"] == "tag-42"
        assert result["has_more"] is False
        assert result["next_offset"] is None
