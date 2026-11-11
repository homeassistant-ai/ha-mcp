"""Direct tests for resolve_context_trace and resolve_global_context."""

import pytest

from ha_mcp.tools.trace_lookup import resolve_global_context

from .test_tools_traces import _make_pooled_client


class TestTraceResolverDirectMatches:
    """Direct tests of ``resolve_context_trace`` and ``resolve_global_context``."""

    @pytest.mark.asyncio
    async def test_global_entry_missing_domain_item_id_is_returned(self):
        """A global direct entry without domain/item_id is returned as None fields."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {
                    "success": True,
                    "result": {"ctx-1": {"run_id": "own-run"}},
                }
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await resolve_global_context(client, "ctx-1")

        assert result.run_id == "own-run"
        assert result.match == "trace"
        assert result.domain is None
        assert result.item_id is None
