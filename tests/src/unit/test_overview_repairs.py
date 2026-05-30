"""Unit tests for ha_get_overview repairs filtering and projection.

Regression coverage for issue #1307: dismissed repairs must be filtered by
default (matching the HA Repairs UI), and the projection must surface the
dismissal-state fields so callers that opt in can distinguish active vs.
ignored repairs.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_search import register_search_tools


def _ws_response(issues: list[dict]) -> dict:
    return {"success": True, "result": {"issues": issues}}


def _active_issue(issue_id: str) -> dict:
    return {
        "issue_id": issue_id,
        "domain": "demo",
        "severity": "warning",
        "translation_key": "demo_issue",
        "ignored": False,
        "dismissed_version": None,
        "is_fixable": True,
        "breaks_in_ha_version": None,
        "created": "2026-05-01T00:00:00+00:00",
        "issue_domain": "demo",
    }


def _ignored_issue(issue_id: str, dismissed_version: str = "2026.4.0") -> dict:
    return {
        "issue_id": issue_id,
        "domain": "demo",
        "severity": "warning",
        "translation_key": "demo_issue",
        "ignored": True,
        "dismissed_version": dismissed_version,
        "is_fixable": False,
        "breaks_in_ha_version": None,
        "created": "2026-04-01T00:00:00+00:00",
        "issue_domain": "demo",
    }


class TestHaGetOverviewRepairs:
    """ha_get_overview repairs filtering, count, and field projection."""

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    def _make_client(self, issues: list[dict]) -> MagicMock:
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={})

        async def fake_ws(msg):
            if msg.get("type") == "repairs/list_issues":
                return _ws_response(issues)
            # Persistent notifications fetch — return empty success
            return {"success": True, "result": []}

        client.send_websocket_message = AsyncMock(side_effect=fake_ws)
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        smart = MagicMock()
        smart.get_system_overview = AsyncMock(return_value={"success": True})
        return smart

    def _build_tool(self, mock_mcp, client, smart_tools):
        register_search_tools(mock_mcp, client, smart_tools=smart_tools)
        return self.registered_tools["ha_get_overview"]

    @pytest.mark.asyncio
    async def test_ignored_repairs_filtered_by_default(
        self, mock_mcp, mock_smart_tools
    ):
        """Default behavior: dismissed repairs do not appear in `repairs[]`
        and are not counted in `repair_count`. Matches the HA Repairs UI.
        """
        issues = [
            _active_issue("active_one"),
            _ignored_issue("dismissed_one"),
            _ignored_issue("dismissed_two", dismissed_version="2025.12.0"),
        ]
        client = self._make_client(issues)
        tool = self._build_tool(mock_mcp, client, mock_smart_tools)

        result = await tool(detail_level="minimal")

        assert result["repair_count"] == 1
        repair_ids = [r["issue_id"] for r in result["repairs"]]
        assert repair_ids == ["active_one"]
        # Surface dismissed count separately so agents can decide to opt in
        assert result["dismissed_repair_count"] == 2

    @pytest.mark.asyncio
    async def test_include_dismissed_returns_all_repairs(
        self, mock_mcp, mock_smart_tools
    ):
        """With `include_dismissed_repairs=True`, all repairs surface and
        the standalone dismissed counter is omitted.
        """
        issues = [_active_issue("active_one"), _ignored_issue("dismissed_one")]
        client = self._make_client(issues)
        tool = self._build_tool(mock_mcp, client, mock_smart_tools)

        result = await tool(detail_level="minimal", include_dismissed_repairs=True)

        assert result["repair_count"] == 2
        assert "dismissed_repair_count" not in result
        ids = {r["issue_id"] for r in result["repairs"]}
        assert ids == {"active_one", "dismissed_one"}

    @pytest.mark.asyncio
    async def test_repair_projection_includes_dismissal_state(
        self, mock_mcp, mock_smart_tools
    ):
        """`ignored`, `dismissed_version`, `is_fixable`, etc. must be present
        on each repair entry so callers can distinguish state.
        """
        issues = [_active_issue("active_one"), _ignored_issue("dismissed_one")]
        client = self._make_client(issues)
        tool = self._build_tool(mock_mcp, client, mock_smart_tools)

        result = await tool(detail_level="minimal", include_dismissed_repairs=True)

        for entry in result["repairs"]:
            for field in (
                "issue_id",
                "domain",
                "severity",
                "translation_key",
                "ignored",
                "dismissed_version",
                "is_fixable",
            ):
                assert field in entry, f"Missing field {field} in repair entry"

        by_id = {r["issue_id"]: r for r in result["repairs"]}
        assert by_id["active_one"]["ignored"] is False
        assert by_id["dismissed_one"]["ignored"] is True
        assert by_id["dismissed_one"]["dismissed_version"] == "2026.4.0"

    @pytest.mark.asyncio
    async def test_no_repairs_yields_zero_counts(self, mock_mcp, mock_smart_tools):
        """Empty issue list yields repair_count = 0 and ``repairs == []``.

        ``repairs`` is advertised in the ``fields=`` docstring as an
        available key, so it must always be present (even as an empty
        list) — otherwise ``fields=["repairs"]`` trips
        ``project_fields``' typo-guard warning on a clean instance.
        """
        client = self._make_client([])
        tool = self._build_tool(mock_mcp, client, mock_smart_tools)

        result = await tool(detail_level="minimal")

        assert result["repair_count"] == 0
        assert result["repairs"] == []
        assert "dismissed_repair_count" not in result

    @pytest.mark.asyncio
    async def test_only_dismissed_repairs_yields_zero_active(
        self, mock_mcp, mock_smart_tools
    ):
        """When every repair is dismissed, repair_count=0 (agents see clean
        state), ``repairs == []`` (no visible issues), and
        ``dismissed_repair_count`` reports the hidden total.
        """
        issues = [_ignored_issue(f"dismissed_{i}") for i in range(3)]
        client = self._make_client(issues)
        tool = self._build_tool(mock_mcp, client, mock_smart_tools)

        result = await tool(detail_level="minimal")

        assert result["repair_count"] == 0
        assert result["repairs"] == []
        assert result["dismissed_repair_count"] == 3

    @pytest.mark.asyncio
    async def test_repairs_ws_failure_does_not_break_overview(
        self, mock_mcp, mock_smart_tools
    ):
        """A websocket exception while fetching repairs leaves the overview
        functional with a `repairs_error` message and repair_count=0.
        """
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={})

        async def fake_ws(msg):
            if msg.get("type") == "repairs/list_issues":
                raise RuntimeError("ws disconnect")
            return {"success": True, "result": []}

        client.send_websocket_message = AsyncMock(side_effect=fake_ws)
        tool = self._build_tool(mock_mcp, client, mock_smart_tools)

        result = await tool(detail_level="minimal")

        assert result["repair_count"] == 0
        # Error path: ``repairs`` stays as the default empty list
        # (set before the try/except so the docstring contract holds
        # even when the WS call fails); ``repairs_error`` carries the
        # diagnostic.
        assert result["repairs"] == []
        assert "repairs_error" in result
        assert "ws disconnect" in result["repairs_error"]

    @pytest.mark.asyncio
    async def test_repairs_ws_success_false_surfaces_error(
        self, mock_mcp, mock_smart_tools
    ):
        """When HA responds `success: False`, surface the error message
        instead of silently returning repair_count=0.
        """
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={})

        async def fake_ws(msg):
            if msg.get("type") == "repairs/list_issues":
                return {
                    "success": False,
                    "error": {"code": "unknown_error", "message": "boom"},
                }
            return {"success": True, "result": []}

        client.send_websocket_message = AsyncMock(side_effect=fake_ws)
        tool = self._build_tool(mock_mcp, client, mock_smart_tools)

        result = await tool(detail_level="minimal")

        assert result["repair_count"] == 0
        assert result["repairs"] == []
        assert "boom" in result["repairs_error"]
