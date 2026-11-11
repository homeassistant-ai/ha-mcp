"""Context-id lookup tests for ha_get_automation_traces."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.tools_traces import TraceTools
from ha_mcp.tools.trace_logbook import TraceLogbook

from .test_tools_traces import _dispatch, _make_pooled_client


class TestTraceListWarnings:
    """Warnings attached to trace list responses."""

    @pytest.mark.asyncio
    async def test_item_only_empty_list_warns(self) -> None:
        """domain+item_id with no traces carries an item-only warning."""
        client = _make_pooled_client(_dispatch())

        result = await TraceTools(client).ha_get_automation_traces(
            domain="automation", item_id="uid_1"
        )

        assert result["trace_count"] == 0
        assert result["warnings"] == [
            "No traces found for automation/uid_1; "
            "the item_id may be incorrect or the automation/script has not run."
        ]


class TestTraceContextLookup:
    """``ha_get_automation_traces`` resolves a context id through Core."""

    @pytest.mark.asyncio
    async def test_context_id_resolves_to_run(self):
        """A known context id fetches that run's detail."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
                "config": {"alias": "Test"},
                "context": {"id": "ctx-1"},
            },
        }
        context_result = {
            "success": True,
            "result": {
                "ctx-1": {
                    "run_id": "r1",
                    "domain": "automation",
                    "item_id": "uid_1",
                }
            },
        }
        client = _make_pooled_client(
            _dispatch(detail_result=detail_result, context_result=context_result)
        )

        result = await TraceTools(client).ha_get_automation_traces(
            entity_id="automation.test", context_id="ctx-1"
        )

        assert result["success"] is True
        assert result["run_id"] == "r1"
        assert result["context_id"] == "ctx-1"
        assert result["context_match"] == "trace"
        assert result["context_scanned"] == 0

    @pytest.mark.asyncio
    async def test_unknown_context_id_is_not_found(self):
        """A context with no trace gets a not-found error, not an empty success."""
        client = _make_pooled_client(
            _dispatch(context_result={"success": True, "result": {}})
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", context_id="missing"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "missing" in error["error"]["message"]
        assert "ha_mcp_tools component" in error["error"]["suggestion"]

    @pytest.mark.asyncio
    async def test_global_unknown_context_id_is_not_found(self):
        """A context-only miss names the component that resolves parent contexts."""
        client = _make_pooled_client(
            _dispatch(context_result={"success": True, "result": {}})
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(context_id="missing")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "ha_mcp_tools component" in error["error"]["suggestion"]

    @pytest.mark.asyncio
    async def test_malformed_context_result_is_not_found(self):
        """A non-map trace/contexts result is treated as no matching trace."""
        client = _make_pooled_client(
            _dispatch(context_result={"success": True, "result": []})
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", context_id="ctx-1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"

    @pytest.mark.parametrize(
        ("kwargs", "dispatch"),
        [
            pytest.param(
                {
                    "entity_id": "automation.test",
                    "run_id": "r1",
                    "context_id": "ctx-1",
                },
                _dispatch(),
                id="run-and-context",
            ),
            pytest.param(
                {"entity_id": "automation.test", "context_id": "   "},
                _dispatch(),
                id="empty-context",
            ),
            pytest.param(
                {"item_id": "uid_1", "run_id": "r1"},
                _dispatch(),
                id="item-without-domain",
            ),
            pytest.param(
                {
                    "entity_id": "automation.test",
                    "domain": "automation",
                    "item_id": "uid_1",
                },
                _dispatch(),
                id="entity-and-item",
            ),
            pytest.param({"run_id": "r1"}, _dispatch(), id="run-without-target"),
            pytest.param({}, _dispatch(), id="no-selector"),
            pytest.param(
                {"context_id": "ctx-1", "domain": "script"},
                _dispatch(
                    context_result={
                        "success": True,
                        "result": {
                            "ctx-1": {
                                "run_id": "r1",
                                "domain": "automation",
                                "item_id": "uid_1",
                            }
                        },
                    }
                ),
                id="context-domain-mismatch",
            ),
            pytest.param(
                {"entity_id": "sensor.test"}, _dispatch(), id="invalid-entity-prefix"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_rejected_selector_combinations(self, kwargs, dispatch):
        """Each invalid selector combination is a validation error."""
        client = _make_pooled_client(dispatch)

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(**kwargs)

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_context_only_resolves_through_global_contexts(self):
        """No entity_id: the global trace/contexts map finds the run."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
                "context": {"id": "ctx-1"},
            },
        }
        context_result = {
            "success": True,
            "result": {
                "ctx-1": {
                    "run_id": "r1",
                    "domain": "automation",
                    "item_id": "uid_1",
                }
            },
        }
        client = _make_pooled_client(
            _dispatch(detail_result=detail_result, context_result=context_result)
        )

        result = await TraceTools(client).ha_get_automation_traces(context_id="ctx-1")

        assert "entity_id" not in result
        assert result["domain"] == "automation"
        assert result["item_id"] == "uid_1"
        assert result["run_id"] == "r1"
        assert result["context_match"] == "trace"

    @pytest.mark.asyncio
    async def test_domain_only_lists_across_items(self):
        """No entity_id: trace/list runs for the whole domain."""
        list_result = {
            "success": True,
            "result": [
                {
                    "run_id": "r1",
                    "domain": "automation",
                    "item_id": "one",
                    "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
                },
                {
                    "run_id": "r2",
                    "domain": "automation",
                    "item_id": "two",
                    "timestamp": {"start": "2026-11-08T16:01:00+00:00"},
                },
            ],
        }
        client = _make_pooled_client(_dispatch(list_result=list_result))

        result = await TraceTools(client).ha_get_automation_traces(domain="automation")

        assert result["domain"] == "automation"
        assert "entity_id" not in result
        assert result["trace_count"] == 2

    @pytest.mark.asyncio
    async def test_domain_list_row_fetches_detail_by_item_id(self):
        """A list row's domain/item_id/run_id fetches its detail."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {"action/0": [{"path": "action/0"}]},
                "context": {"id": "ctx-1"},
            },
        }
        client = _make_pooled_client(_dispatch(detail_result=detail_result))

        result = await TraceTools(client).ha_get_automation_traces(
            domain="automation", item_id="uid_1", run_id="r1"
        )

        assert result["domain"] == "automation"
        assert result["item_id"] == "uid_1"
        assert result["run_id"] == "r1"
        assert "entity_id" not in result

    @pytest.mark.asyncio
    async def test_domain_scoped_context_resolves_the_direct_map(self):
        """domain is validated against the run's own context entry."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
                "context": {"id": "ctx-1"},
            },
        }
        context_result = {
            "success": True,
            "result": {
                "ctx-1": {
                    "run_id": "r1",
                    "domain": "automation",
                    "item_id": "uid_1",
                }
            },
        }
        client = _make_pooled_client(
            _dispatch(detail_result=detail_result, context_result=context_result)
        )

        result = await TraceTools(client).ha_get_automation_traces(
            context_id="ctx-1", domain="automation"
        )

        assert result["context_match"] == "trace"
        assert result["domain"] == "automation"
        assert result["item_id"] == "uid_1"

    @pytest.mark.asyncio
    async def test_item_only_lists_that_trace_key(self):
        """domain+item_id lists one item's runs."""
        list_result = {
            "success": True,
            "result": [
                {
                    "run_id": "r1",
                    "item_id": "uid_1",
                    "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
                }
            ],
        }
        client = _make_pooled_client(_dispatch(list_result=list_result))

        result = await TraceTools(client).ha_get_automation_traces(
            domain="automation", item_id="uid_1"
        )

        assert result["trace_count"] == 1
        assert "entity_id" not in result

    @pytest.mark.asyncio
    async def test_whitespace_sections_with_detailed_fetches_logbook(self):
        """detailed=True still fetches logbook for a whitespace section string."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
                "context": {"id": "ctx-1"},
                "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
            },
        }
        client = _make_pooled_client(_dispatch(detail_result=detail_result))
        logbook = TraceLogbook([{"name": "Light"}], "component", [], 1, False)

        with patch(
            "ha_mcp.tools.tools_traces.fetch_trace_logbook",
            AsyncMock(return_value=logbook),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test",
                run_id="r1",
                sections="   ",
                detailed=True,
            )

        assert result["logbook_entries"] == [{"name": "Light"}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            ToolError("logbook timed out"),
            HomeAssistantConnectionError("REST logbook unavailable"),
        ],
    )
    async def test_logbook_failure_keeps_the_trace_with_a_warning(self, error):
        """A failed optional logbook read must not discard the trace."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {"action/0": [{"path": "action/0"}]},
                "context": {"id": "ctx-1"},
            },
        }
        client = _make_pooled_client(_dispatch(detail_result=detail_result))

        with patch(
            "ha_mcp.tools.tools_traces.fetch_trace_logbook",
            AsyncMock(side_effect=error),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test",
                run_id="r1",
                sections="trace,logbook",
            )

        assert result["trace"]["action/0"][0]["path"] == "action/0"
        assert result["logbook_entries"] == []
        assert any("logbook unavailable" in warning for warning in result["warnings"])

    @pytest.mark.asyncio
    async def test_context_lookup_transport_failure_is_classified(self):
        """A dead transport during trace/contexts reports CONNECTION_FAILED."""
        client = _make_pooled_client(
            _dispatch(
                context_result={
                    "success": False,
                    "error": "Failed to connect to Home Assistant",
                }
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", context_id="ctx-1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_context_lookup_command_failure_is_service_call_failed(self):
        """A non-connection trace/contexts failure keeps its command error."""
        client = _make_pooled_client(
            _dispatch(context_result={"success": False, "error": "unknown_command"})
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", context_id="ctx-1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"

    @pytest.mark.asyncio
    async def test_global_context_without_domain_item_is_resource_not_found(self):
        """A global context hit lacking domain/item_id raises a structured tool
        error."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {
                    "success": True,
                    "result": {"ctx-1": {"run_id": "own-run"}},
                }
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(context_id="ctx-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "context resolved without a domain/item" in error["error"]["message"]
