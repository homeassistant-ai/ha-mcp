"""Unit tests for ha_get_overview system_info builder."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_search import register_search_tools


class TestHaGetOverviewSystemInfo:
    """Test system_info field assembly in ha_get_overview at detail_level='full'."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures registered tool functions."""
        mcp = MagicMock()
        self.registered_tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client with default-empty config."""
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={})
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        """Create a mock smart_tools that returns a minimal success result."""
        smart = MagicMock()
        smart.get_system_overview = AsyncMock(return_value={"success": True})
        return smart

    @pytest.fixture
    def overview_tool(self, mock_mcp, mock_client, mock_smart_tools):
        """Register search tools and return the ha_get_overview function."""
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_get_overview"]

    @pytest.mark.asyncio
    async def test_allowlist_external_dirs_missing_key_yields_none(
        self, mock_client, overview_tool
    ):
        """When HA config omits the key entirely, the field is None — not [].

        Distinguishes 'HA didn't expose the key' from 'HA reported an empty
        allowlist' for security-sensitive agent reasoning. Locks in the contract
        so a future refactor cannot silently switch the default back to [].
        """
        mock_client.get_config = AsyncMock(return_value={})

        result = await overview_tool(detail_level="full")

        system_info = result["system_info"]
        assert "allowlist_external_dirs" in system_info
        assert system_info["allowlist_external_dirs"] is None

    @pytest.mark.asyncio
    async def test_allowlist_external_dirs_passes_through_list_value(
        self, mock_client, overview_tool
    ):
        """When HA config exposes the key, the list value passes through unchanged."""
        mock_client.get_config = AsyncMock(
            return_value={"allowlist_external_dirs": ["/media", "/share"]}
        )

        result = await overview_tool(detail_level="full")

        assert result["system_info"]["allowlist_external_dirs"] == [
            "/media",
            "/share",
        ]

    @pytest.mark.asyncio
    async def test_allowlist_external_dirs_omitted_at_minimal_detail_level(
        self, mock_client, overview_tool
    ):
        """The field must not appear in system_info when detail_level != 'full'."""
        mock_client.get_config = AsyncMock(
            return_value={"allowlist_external_dirs": ["/media"]}
        )

        result = await overview_tool(detail_level="minimal")

        assert "allowlist_external_dirs" not in result["system_info"]


class TestHaGetOverviewFieldsProjection:
    """fields= projects the response to the requested top-level keys.

    Pins the contract from issue #1199: callers that only need one section
    (e.g. system_info) can request it via fields= and receive a response
    that omits all other top-level keys.
    """

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(
            return_value={"version": "2026.5.0", "location_name": "Home"}
        )
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        smart = MagicMock()
        smart.get_system_overview = AsyncMock(
            return_value={
                "success": True,
                "domains": {"light": {"count": 3}},
                "entity_summary": [],
                "total_entities": 3,
            }
        )
        return smart

    @pytest.fixture
    def overview_tool(self, mock_mcp, mock_client, mock_smart_tools):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_get_overview"]

    @pytest.mark.asyncio
    async def test_fields_none_returns_full_response(self, overview_tool):
        """fields=None (default) returns the full response — no projection."""
        result = await overview_tool()
        assert "success" in result
        assert "system_info" in result
        assert "domains" in result

    @pytest.mark.asyncio
    async def test_fields_single_key_projects_correctly(self, overview_tool):
        """fields=["system_info"] keeps only system_info (+ success always)."""
        result = await overview_tool(fields=["system_info"])
        assert result["success"] is True
        assert "system_info" in result
        assert result["system_info"]["version"] == "2026.5.0"
        # All other top-level keys must be absent.
        for key in ("domains", "entity_summary", "total_entities", "repair_count"):
            assert key not in result, f"unexpected key {key!r} survived projection"

    @pytest.mark.asyncio
    async def test_fields_multiple_keys(self, overview_tool):
        """fields=["system_info", "domains"] keeps exactly those two (+ success)."""
        result = await overview_tool(fields=["system_info", "domains"])
        assert "system_info" in result
        assert "domains" in result
        assert "entity_summary" not in result

    @pytest.mark.asyncio
    async def test_fields_success_always_included(self, overview_tool):
        """success is always present even when the caller omits it from fields."""
        result = await overview_tool(fields=["domains"])
        assert "success" in result

    @pytest.mark.asyncio
    async def test_fields_unknown_key_silently_absent(self, overview_tool):
        """Requesting a non-existent key silently produces no entry — no error."""
        result = await overview_tool(fields=["nonexistent_key"])
        assert result["success"] is True
        assert "nonexistent_key" not in result

    @pytest.mark.asyncio
    async def test_bad_fields_integer_raises_tool_error(self, overview_tool):
        """fields=123 raises ToolError with VALIDATION_FAILED + parameter='fields'.

        Pins the early-validate raise path (``tools_search.py`` ha_get_overview)
        so a regression dropping the try/except still surfaces a regression.
        """
        import json

        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as exc_info:
            await overview_tool(fields=123)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert error.get("parameter") == "fields"

    @pytest.mark.asyncio
    async def test_bad_json_fields_raises_tool_error(self, overview_tool):
        """fields='[\"' (malformed JSON) raises ToolError."""
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            await overview_tool(fields='["')


class TestHaGetOverviewSystemSummaryVersion:
    """Tests for system_summary["version"] enrichment from HA config (issue #1199)."""

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_smart_tools_with_summary(self):
        """smart_tools returns a result that includes a system_summary sub-dict."""
        smart = MagicMock()
        smart.get_system_overview = AsyncMock(
            return_value={
                "success": True,
                "system_summary": {"entity_count": 10},
            }
        )
        return smart

    @pytest.fixture
    def overview_tool_with_version(self, mock_mcp, mock_smart_tools_with_summary):
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={"version": "2026.5.3"})
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        register_search_tools(
            mock_mcp, client, smart_tools=mock_smart_tools_with_summary
        )
        return self.registered_tools["ha_get_overview"]

    @pytest.fixture
    def overview_tool_config_fails(self, mock_mcp, mock_smart_tools_with_summary):
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(side_effect=RuntimeError("connection refused"))
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        register_search_tools(
            mock_mcp, client, smart_tools=mock_smart_tools_with_summary
        )
        return self.registered_tools["ha_get_overview"]

    @pytest.fixture
    def overview_tool_version_none(self, mock_mcp, mock_smart_tools_with_summary):
        """Config returns successfully but version key is absent."""
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={})  # version key missing
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        register_search_tools(
            mock_mcp, client, smart_tools=mock_smart_tools_with_summary
        )
        return self.registered_tools["ha_get_overview"]

    @pytest.mark.asyncio
    async def test_version_populated_from_config(self, overview_tool_with_version):
        """system_summary["version"] reflects the HA version from config."""
        result = await overview_tool_with_version()
        assert result["system_summary"]["version"] == "2026.5.3"

    @pytest.mark.asyncio
    async def test_version_unknown_on_config_failure(self, overview_tool_config_fails):
        """system_summary["version"] is "unknown" when config fetch raises."""
        result = await overview_tool_config_fails()
        assert "system_summary" in result
        assert result["system_summary"]["version"] == "unknown"

    @pytest.mark.asyncio
    async def test_version_unknown_when_config_omits_key(
        self, overview_tool_version_none
    ):
        """system_summary["version"] is "unknown" when config has no version key."""
        result = await overview_tool_version_none()
        assert result["system_summary"]["version"] == "unknown"


class TestHaGetOverviewSettingsUrl:
    """Pin the stdio sidecar URL surfacing in ha_get_overview (issue #863).

    The ``settings_url`` field is the ONLY path the LLM ever sees the
    URL through — a regression that silently emits an empty string (or
    inverts the conditional) would route users to a broken bookmark
    without test failure. Mirrors the system_info test scaffolding so
    the file's pacing stays consistent.
    """

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={})
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        smart = MagicMock()
        smart.get_system_overview = AsyncMock(return_value={"success": True})
        return smart

    @pytest.fixture
    def overview_tool(self, mock_mcp, mock_client, mock_smart_tools):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_get_overview"]

    @pytest.mark.asyncio
    async def test_settings_url_surfaced_when_sidecar_running(
        self, overview_tool, monkeypatch
    ):
        """Sidecar URL file present → field appears verbatim in the result."""
        url = "http://127.0.0.1:8099/private_abc/settings"
        monkeypatch.setattr(
            "ha_mcp.stdio_settings_sidecar.read_sidecar_url",
            lambda: url,
        )
        result = await overview_tool(detail_level="minimal")
        assert result.get("settings_url") == url

    @pytest.mark.asyncio
    async def test_settings_url_omitted_when_no_sidecar(
        self, overview_tool, monkeypatch
    ):
        """No URL file → ``settings_url`` MUST NOT be in the result.

        Strict ``not in`` rather than ``== None`` so a future regression
        that emits ``settings_url=""`` or ``settings_url=None`` to every
        overview call is caught.
        """
        monkeypatch.setattr(
            "ha_mcp.stdio_settings_sidecar.read_sidecar_url",
            lambda: None,
        )
        result = await overview_tool(detail_level="minimal")
        assert "settings_url" not in result

    @pytest.mark.asyncio
    async def test_settings_url_survives_fields_projection(
        self, overview_tool, monkeypatch
    ):
        """``settings_url`` MUST be returned even when ``fields=`` filters
        the rest of the payload.

        A less-attentive LLM that minimizes payload via
        ``fields=["system_info"]`` (or any narrow projection) would
        otherwise lose the URL silently — and the LLM cannot hand the
        user a URL it never receives. Pinning the post-projection
        emission keeps ``settings_url`` discoverable regardless of how
        the caller scopes the overview response.
        """
        url = "http://127.0.0.1:8099/private_abc/settings"
        monkeypatch.setattr(
            "ha_mcp.stdio_settings_sidecar.read_sidecar_url",
            lambda: url,
        )
        result = await overview_tool(fields=["system_info"])
        assert result.get("settings_url") == url
        # system_info is still projected; settings_url is the only
        # extra survivor (plus the always-retained success/warnings).
        assert "system_info" in result


class TestHaGetOverviewAlwaysEmittedKeys:
    """Keys advertised in the ``fields=`` docstring must always be in
    the result so ``fields=[<key>]`` never trips the ``project_fields``
    typo-guard warning on a clean instance.

    Regression coverage for the LLM complaint that ``notifications`` /
    ``repairs`` were "not in available keys" on an HA instance with no
    active alerts — the docstring promised them but the code emitted
    them only when non-empty.
    """

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client_empty_ws(self):
        """Client whose WS calls all return success with empty lists."""
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={})

        async def empty_ws(msg):
            if msg.get("type") == "persistent_notification/get":
                return {"success": True, "result": []}
            if msg.get("type") == "repairs/list_issues":
                return {"success": True, "result": {"issues": []}}
            return {"success": True, "result": []}

        client.send_websocket_message = AsyncMock(side_effect=empty_ws)
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        smart = MagicMock()
        smart.get_system_overview = AsyncMock(return_value={"success": True})
        return smart

    @pytest.fixture
    def overview_tool(self, mock_mcp, mock_client_empty_ws, mock_smart_tools):
        register_search_tools(
            mock_mcp, mock_client_empty_ws, smart_tools=mock_smart_tools
        )
        return self.registered_tools["ha_get_overview"]

    @pytest.mark.asyncio
    async def test_notifications_emitted_as_empty_list_when_none(self, overview_tool):
        result = await overview_tool(detail_level="minimal")
        assert result["notifications"] == []
        assert result["notification_count"] == 0

    @pytest.mark.asyncio
    async def test_repairs_emitted_as_empty_list_when_none(self, overview_tool):
        result = await overview_tool(detail_level="minimal")
        assert result["repairs"] == []
        assert result["repair_count"] == 0

    @pytest.mark.asyncio
    async def test_fields_projection_returns_empty_lists_without_warning(
        self, overview_tool
    ):
        """``fields=["notifications","repairs"]`` on a clean instance must
        return both as empty lists, with no ``warnings`` entry complaining
        about missing keys.
        """
        result = await overview_tool(fields=["notifications", "repairs"])
        assert result["notifications"] == []
        assert result["repairs"] == []
        # project_fields() appends a "not found in response" warning when
        # the requested key is absent. The whole point of the empty-list
        # default is to keep this warning silent on a clean instance.
        warnings = result.get("warnings") or []
        joined = " ".join(str(w) for w in warnings)
        assert "notifications" not in joined
        assert "repairs" not in joined
