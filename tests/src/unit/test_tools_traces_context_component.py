"""Component trace-store tests for ha_get_automation_traces."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_traces import TraceTools

from .test_tools_traces import _make_pooled_client


class TestTraceContextComponent:
    """Component trace-store behavior."""

    @pytest.mark.asyncio
    async def test_component_context_matches_without_a_public_scan(self):
        """The component trace-store pass returns the match and its trace."""
        payload = {
            "match": "parent",
            "run_id": "r1",
            "domain": "automation",
            "item_id": "uid_1",
            "trace": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
                "context": {"id": "own-1", "parent_id": "parent-1"},
            },
            "scanned": 7,
            "truncated": False,
            "errors": [],
            "other_matches": [
                {
                    "domain": "script",
                    "item_id": "uid_2",
                    "run_id": "r2",
                    "match": "parent",
                }
            ],
        }

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "ha_mcp_tools/trace_context":
                return {"success": True, "result": payload}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        caps = SimpleNamespace(capabilities=frozenset({"trace_context"}))
        with patch(
            "ha_mcp.tools.trace_lookup.get_component_caps",
            AsyncMock(return_value=caps),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                context_id="parent-1"
            )

        assert result["context_match"] == "parent"
        assert result["context_scanned"] == 7
        assert result["domain"] == "automation"
        assert result["item_id"] == "uid_1"
        assert result["context_other_matches"] == [
            {
                "domain": "script",
                "item_id": "uid_2",
                "run_id": "r2",
                "match": "parent",
            }
        ]
        types = [
            call.args[0].get("type")
            for call in client.send_websocket_message.await_args_list
        ]
        assert "ha_mcp_tools/trace_context" in types
        assert "trace/list" not in types
        assert "trace/get" not in types

    @pytest.mark.asyncio
    async def test_component_no_match_is_authoritative(self):
        """A component miss does not fall back to a public scan."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "ha_mcp_tools/trace_context":
                return {
                    "success": True,
                    "result": {
                        "match": None,
                        "run_id": None,
                        "domain": None,
                        "item_id": None,
                        "trace": None,
                        "scanned": 7,
                        "truncated": False,
                        "errors": [],
                    },
                }
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        caps = SimpleNamespace(capabilities=frozenset({"trace_context"}))
        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value=caps),
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await TraceTools(client).ha_get_automation_traces(context_id="parent-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error["scanned"] == 7
        types = [
            call.args[0].get("type")
            for call in client.send_websocket_message.await_args_list
        ]
        assert "trace/list" not in types
