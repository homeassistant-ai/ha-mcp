"""Unit tests for the supervisor add-on log fix (#950).

Covers:
- `HomeAssistantClient.get_addon_logs()` — new REST-client method that fetches
  add-on container logs via HA Core's `/api/hassio/addons/{slug}/logs` proxy,
  which HA Core returns as text/plain (no JSON decode in the hot path).
- `_get_supervisor_log` (the `ha_get_logs(source="supervisor")` wrapper) —
  response shape, tail slicing, search filter, and API-error → ToolError
  translation with status-code-specific suggestions.
- Stale `ha_list_addons()` suggestion strings are replaced with
  `ha_get_addon()`.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_utility import register_utility_tools


@pytest.fixture
def mock_client():
    """HomeAssistantClient with stubbed internals — no real network."""
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        client = HomeAssistantClient()
        client.base_url = "http://test.local:8123"
        client.token = "test-token"
        client.timeout = 30
        client.httpx_client = MagicMock()
        return client


def _register_and_collect(client: Any) -> dict[str, Any]:
    """Register utility tools on a collector mcp and return the registered tools.

    The production decorator chain is ``@mcp.tool(...)`` outside ``@log_tool_usage``,
    so the collected entry is the ``log_tool_usage``-wrapped async function.
    """
    collected: dict[str, Any] = {}

    def _tool(**_kwargs: Any) -> Any:
        def _wrap(fn: Any) -> Any:
            collected[fn.__name__] = fn
            return fn
        return _wrap

    mcp = SimpleNamespace(tool=_tool)
    register_utility_tools(mcp, client)
    return collected


def _parse_tool_error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    """Parse the JSON payload from a ToolError raised by a tool."""
    payload: dict[str, Any] = json.loads(str(exc_info.value))
    return payload


class TestGetAddonLogs:
    """Tests for the REST-client `get_addon_logs` method (the core fix)."""

    @pytest.mark.asyncio
    async def test_returns_text_on_200(self, mock_client):
        """Successful 200 response returns the raw text body."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "2026-04-11 10:00:00 addon starting\nready\n"
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        result = await mock_client.get_addon_logs("core_mosquitto")

        assert "addon starting" in result
        assert "ready" in result

    @pytest.mark.asyncio
    async def test_calls_correct_endpoint_with_text_accept(self, mock_client):
        """Endpoint path and Accept: text/plain header must match the HA proxy contract."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = ""
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        await mock_client.get_addon_logs("81f33d0f_ha_mcp_dev")

        mock_client.httpx_client.request.assert_called_once()
        args, kwargs = mock_client.httpx_client.request.call_args
        assert args[0] == "GET"
        assert args[1] == "/hassio/addons/81f33d0f_ha_mcp_dev/logs"
        assert kwargs["headers"]["Accept"] == "text/plain"

    @pytest.mark.asyncio
    async def test_raises_auth_error_on_401(self, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "unauthorized"
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(HomeAssistantAuthError):
            await mock_client.get_addon_logs("core_mosquitto")

    @pytest.mark.asyncio
    async def test_raises_api_error_on_404_with_slug_context(self, mock_client):
        """404 (unknown slug) raises HomeAssistantAPIError with status 404 and body.

        The Supervisor-proxied endpoint returns `text/plain` error bodies, not
        JSON, so `response.json()` raises and the error message falls back to
        `response.text`. Mirror that here.
        """
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Addon is not installed"
        mock_response.json = MagicMock(side_effect=ValueError("not json"))
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.get_addon_logs("nonexistent_slug")

        assert exc_info.value.status_code == 404
        assert "Addon is not installed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_connection_error_on_network_failure(self, mock_client):
        mock_client.httpx_client.request = AsyncMock(
            side_effect=httpx.ConnectError("no route")
        )

        with pytest.raises(HomeAssistantConnectionError):
            await mock_client.get_addon_logs("core_mosquitto")

    @pytest.mark.asyncio
    async def test_raises_connection_error_on_timeout(self, mock_client):
        mock_client.httpx_client.request = AsyncMock(
            side_effect=httpx.TimeoutException("timeout")
        )

        with pytest.raises(HomeAssistantConnectionError):
            await mock_client.get_addon_logs("core_mosquitto")

    @pytest.mark.asyncio
    async def test_does_not_parse_json(self, mock_client):
        """Regression guard for #950: the fetch must not try to JSON-decode the
        text/plain log body (that's what broke the old websocket path)."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "plain log line 1\nplain log line 2\n"
        # Make .json() raise so any stray call would fail the test.
        mock_response.json = MagicMock(
            side_effect=ValueError("json parse should not be called")
        )
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        result = await mock_client.get_addon_logs("core_mosquitto")

        assert "plain log line 1" in result
        mock_response.json.assert_not_called()


class TestRawRequestEmptyBodyFallback:
    """Error message must stay actionable even when the 4xx body is empty.

    If `_raw_request` just used `error_data.get("message", "Unknown error")`
    when the proxy returned a blank body, the raised error read
    `"API error: 4xx - "` — same silent-failure signature #950 describes,
    one layer down.
    """

    @pytest.mark.asyncio
    async def test_empty_body_falls_back_to_reason_phrase(self, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.reason_phrase = "Bad Gateway"
        mock_response.text = ""
        mock_response.json = MagicMock(side_effect=ValueError("empty"))
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client._raw_request("GET", "/anything")

        # Message must not be the bare "API error: 502 - " with an empty tail.
        assert "Bad Gateway" in str(exc_info.value)
        assert not str(exc_info.value).endswith(" - ")

    @pytest.mark.asyncio
    async def test_whitespace_only_body_falls_back(self, mock_client):
        """A whitespace-only JSON body like `{"message": "   "}` still yields an
        actionable tail, not `"API error: 4xx -    "`."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.reason_phrase = "Internal Server Error"
        mock_response.text = '{"message": "   "}'
        mock_response.json = MagicMock(return_value={"message": "   "})
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client._raw_request("GET", "/anything")

        assert "Internal Server Error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_empty_body_and_no_reason_phrase_uses_placeholder(self, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.reason_phrase = ""
        mock_response.text = ""
        mock_response.json = MagicMock(side_effect=ValueError("empty"))
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client._raw_request("GET", "/anything")

        assert "<empty body>" in str(exc_info.value)


class TestGetSupervisorLogWrapper:
    """Tests for the `_get_supervisor_log` wrapper exercised via `ha_get_logs`.

    Locks down the response shape, the `[-limit:]` tail slicing, the `search`
    filter, and the `HomeAssistantAPIError → exception_to_structured_error`
    translation the REST-client tests don't cover.
    """

    @pytest.fixture
    def client_with_logs(self):
        """Client whose `get_addon_logs` is a configurable AsyncMock."""
        client = MagicMock()
        client.get_addon_logs = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_happy_path_response_shape(self, client_with_logs):
        client_with_logs.get_addon_logs.return_value = "line 1\nline 2\nline 3\n"
        tools = _register_and_collect(client_with_logs)

        result = await tools["ha_get_logs"](source="supervisor", slug="core_mosquitto")

        assert result["success"] is True
        assert result["source"] == "supervisor"
        assert result["slug"] == "core_mosquitto"
        assert result["log"] == "line 1\nline 2\nline 3"
        assert result["total_lines"] == 3
        assert result["returned_lines"] == 3
        assert "limit" in result
        # No filters applied → key is omitted
        assert "filters_applied" not in result
        client_with_logs.get_addon_logs.assert_awaited_once_with("core_mosquitto")

    @pytest.mark.asyncio
    async def test_tail_slicing_returns_last_n_lines(self, client_with_logs):
        """`lines[-effective_limit:]` — users want recent activity, not the head."""
        client_with_logs.get_addon_logs.return_value = "\n".join(
            f"line {i}" for i in range(1, 21)
        ) + "\n"
        tools = _register_and_collect(client_with_logs)

        result = await tools["ha_get_logs"](
            source="supervisor", slug="core_mosquitto", limit=5
        )

        returned = result["log"].splitlines()
        assert returned == ["line 16", "line 17", "line 18", "line 19", "line 20"]
        assert result["total_lines"] == 20
        assert result["returned_lines"] == 5
        assert result["limit"] == 5

    @pytest.mark.asyncio
    async def test_search_filter_is_case_insensitive_and_recorded(
        self, client_with_logs
    ):
        client_with_logs.get_addon_logs.return_value = (
            "INFO startup complete\n"
            "ERROR something broke\n"
            "DEBUG trivial\n"
            "ERROR another failure\n"
        )
        tools = _register_and_collect(client_with_logs)

        result = await tools["ha_get_logs"](
            source="supervisor", slug="core_mosquitto", search="error"
        )

        lines = result["log"].splitlines()
        assert len(lines) == 2
        assert all("ERROR" in ln for ln in lines)
        assert result["total_lines"] == 2  # total after filter
        assert result["filters_applied"] == {"search": "error"}

    @pytest.mark.asyncio
    async def test_404_raises_tool_error_with_not_found_suggestion(
        self, client_with_logs
    ):
        client_with_logs.get_addon_logs.side_effect = HomeAssistantAPIError(
            "API error: 404 - Addon is not installed",
            status_code=404,
            response_data={"message": "Addon is not installed"},
        )
        tools = _register_and_collect(client_with_logs)

        with pytest.raises(ToolError) as exc_info:
            await tools["ha_get_logs"](
                source="supervisor", slug="nonexistent"
            )

        payload = _parse_tool_error(exc_info)
        suggestions = payload["error"]["suggestions"]
        assert any("not found or not installed" in s for s in suggestions)
        assert any("ha_get_addon" in s for s in suggestions)
        # context kwargs get spread onto the response root by create_error_response
        assert payload.get("slug") == "nonexistent"
        assert payload.get("source") == "supervisor"

    @pytest.mark.asyncio
    async def test_400_uses_distinct_suggestion_and_service_error_code(
        self, client_with_logs
    ):
        """400 means Supervisor rejected the request, not caller input validation.

        The default `exception_to_structured_error` path would map 400 →
        VALIDATION_INVALID_PARAMETER; for a downstream proxy rejection,
        SERVICE_CALL_FAILED is more accurate.
        """
        client_with_logs.get_addon_logs.side_effect = HomeAssistantAPIError(
            "API error: 400 - bad request",
            status_code=400,
            response_data={"message": "bad request"},
        )
        tools = _register_and_collect(client_with_logs)

        with pytest.raises(ToolError) as exc_info:
            await tools["ha_get_logs"](
                source="supervisor", slug="weird_slug"
            )

        payload = _parse_tool_error(exc_info)
        suggestions = payload["error"]["suggestions"]
        # 400 must NOT say "not found or not installed" — different root cause.
        assert not any("not found or not installed" in s for s in suggestions)
        assert any("Supervisor rejected" in s for s in suggestions)
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"

    @pytest.mark.asyncio
    async def test_connection_error_keeps_slug_hint(self, client_with_logs):
        """A transient network failure on a wrong slug should still hint at slug
        verification — the connection-error path must not drop that suggestion."""
        client_with_logs.get_addon_logs.side_effect = HomeAssistantConnectionError(
            "no route"
        )
        tools = _register_and_collect(client_with_logs)

        with pytest.raises(ToolError) as exc_info:
            await tools["ha_get_logs"](
                source="supervisor", slug="core_mosquitto"
            )

        payload = _parse_tool_error(exc_info)
        suggestions = payload["error"]["suggestions"]
        assert any("Check Home Assistant connection" in s for s in suggestions)
        assert any(
            "Verify add-on slug 'core_mosquitto' is correct" in s for s in suggestions
        )
        assert any("ha_get_addon" in s for s in suggestions)

    @pytest.mark.asyncio
    async def test_level_param_emits_warning_for_supervisor_source(
        self, client_with_logs
    ):
        """`level` doesn't apply to supervisor logs (raw container text); the
        validation layer should warn rather than silently drop the parameter.
        """
        client_with_logs.get_addon_logs.return_value = "line 1\n"
        tools = _register_and_collect(client_with_logs)

        result = await tools["ha_get_logs"](
            source="supervisor", slug="core_mosquitto", level="ERROR"
        )

        assert result["success"] is True
        assert "warnings" in result, "Expected a warning when level is set on supervisor"
        assert any(
            "level" in w and "supervisor" in w for w in result["warnings"]
        ), f"Expected level/supervisor warning, got: {result['warnings']}"


class TestStaleToolNameReferences:
    """Regression guard for #950 bug 2: stale `ha_list_addons()` suggestions."""

    def test_no_tool_module_references_removed_ha_list_addons(self):
        """`ha_list_addons` was consolidated into `ha_get_addon` — no stale refs.

        Scans every `src/ha_mcp/tools/**/*.py` with a word-boundary regex so
        the guard catches regressions in any module, not just tools_utility.py,
        and ignores substrings inside longer identifiers.
        """
        tools_dir = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "ha_mcp"
            / "tools"
        )
        pattern = re.compile(r"\bha_list_addons\b")
        offenders = [
            f.relative_to(tools_dir)
            for f in tools_dir.rglob("*.py")
            if pattern.search(f.read_text(encoding="utf-8"))
        ]
        assert not offenders, (
            f"Stale `ha_list_addons` reference in: {offenders}. "
            "Replace suggestions/docs with `ha_get_addon()` — see #950."
        )
