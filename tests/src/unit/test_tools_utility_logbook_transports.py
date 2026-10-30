"""Unit tests for the ``ha_get_logs`` logbook transports.

Component → REST → WebSocket routing, the component failure taxonomy, the
device-filter fallback warning, and the status-aware REST suggestions. Split
from ``test_tools_utility_log_order`` to keep both modules under the
module-size ratchet.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_logs import LogTools

from ._logbook_fallback import call_kwargs as _call_kwargs
from .test_tools_history import _no_real_caps_probe  # noqa: F401


def _real_timeout_shape() -> HomeAssistantConnectionError:
    """The shape the real client raises: a timeout wrapped as a no-answer error.

    ``send_websocket_message`` funnels ``HomeAssistantCommandTimeout`` into
    ``_NO_ANSWER_ERRORS`` and re-raises it as a ``HomeAssistantConnectionError``
    with the timeout as the cause (``rest_client``), so tests must exercise
    that chain rather than raising the timeout directly.
    """
    try:
        raise HomeAssistantCommandTimeout("logbook query timed out")
    except HomeAssistantCommandTimeout as timeout:
        try:
            raise HomeAssistantConnectionError(
                "WebSocket transport failed"
            ) from timeout
        except HomeAssistantConnectionError as wrapped:
            return wrapped


class TestLogbookTransports:
    """source='logbook' — component → REST → WS transport routing."""

    @staticmethod
    def _client(
        ws_result: Any = None,
        ws_payload: Any = None,
        ws_error: Exception | None = None,
    ) -> AsyncMock:
        client = AsyncMock()
        if ws_error is not None:
            client.send_websocket_message = AsyncMock(side_effect=ws_error)
        elif ws_payload is not None:
            client.send_websocket_message = AsyncMock(return_value=ws_payload)
        else:
            client.send_websocket_message = AsyncMock(
                return_value={"success": True, "result": ws_result or []}
            )
        client.get_logbook = AsyncMock(return_value=[])
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        return client

    @staticmethod
    def _component_rows() -> list[dict[str, Any]]:
        """Native component/REST rows: ISO ``when``, names from Core."""
        return [
            {
                "state": "on",
                "entity_id": "light.kitchen",
                "name": "Kitchen Light",
                "when": "2026-06-16T00:00:00+00:00",
            },
            {
                "name": "Movie night",
                "message": "started",
                "domain": "automation",
                "entity_id": "automation.movie_night",
                "when": "2026-06-16T00:01:00+00:00",
            },
        ]

    @staticmethod
    def _ws_events() -> list[dict[str, Any]]:
        """Raw WS fallback shapes: epoch-second ``when``, no ``name``."""
        return [
            {
                "state": "on",
                "entity_id": "light.kitchen",
                "icon": "mdi:lightbulb",
                "when": 1780000000.0,
            },
            {
                "name": "Movie night",
                "message": "started",
                "domain": "automation",
                "entity_id": "automation.movie_night",
                "when": 1780000060.0,
            },
        ]

    @pytest.mark.asyncio
    async def test_component_rows_pass_through_untouched(
        self, logbook_component_caps
    ) -> None:
        rows = self._component_rows()
        client = self._client(ws_payload={"success": True, "result": rows})
        tools = LogTools(client)
        result = await tools.get_logs(**_call_kwargs(source="logbook"))

        command = client.send_websocket_message.await_args.args[0]
        assert command["type"] == "ha_mcp_tools/logbook_events"
        assert command["start_time"] < command["end_time"]
        client.get_logbook.assert_not_awaited()
        by_entity = {e["entity_id"]: e for e in result["data"]["entries"]}
        assert by_entity["light.kitchen"]["name"] == "Kitchen Light"
        assert by_entity["light.kitchen"]["when"] == "2026-06-16T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_component_receives_the_native_filters(
        self, logbook_component_caps
    ) -> None:
        client = self._client(ws_payload={"success": True, "result": []})
        tools = LogTools(client)
        await tools.get_logs(
            **_call_kwargs(
                source="logbook",
                entity_id="light.a, light.b",
                device_id="dev1",
            )
        )
        command = client.send_websocket_message.await_args.args[0]
        assert command["type"] == "ha_mcp_tools/logbook_events"
        assert command["entity_ids"] == ["light.a", "light.b"]
        assert command["device_ids"] == ["dev1"]

    @pytest.mark.asyncio
    async def test_component_unknown_command_falls_back_to_rest(
        self, logbook_component_caps
    ) -> None:
        client = self._client(
            ws_payload={
                "success": False,
                "error": "Command failed: Unknown command.",
                "error_code": "unknown_command",
            }
        )
        client.get_logbook = AsyncMock(return_value=self._component_rows())
        tools = LogTools(client)
        with patch("ha_mcp.tools.log_sources_logbook.invalidate_caps") as invalidate:
            result = await tools.get_logs(**_call_kwargs(source="logbook"))
        invalidate.assert_called_once()
        assert result["data"]["total_entries"] == 2
        client.get_logbook.assert_awaited()

    @pytest.mark.asyncio
    async def test_component_rejection_is_authoritative(
        self, logbook_component_caps
    ) -> None:
        client = self._client(
            ws_payload={
                "success": False,
                "error": "Invalid start_time",
                "error_code": "invalid_start_time",
            }
        )
        tools = LogTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.get_logs(**_call_kwargs(source="logbook"))
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_component_invalid_format_maps_to_validation_error(
        self, logbook_component_caps
    ) -> None:
        client = self._client(
            ws_payload={
                "success": False,
                "error": "Entity ID kitchen_light is an invalid entity ID",
                "error_code": "invalid_format",
            }
        )
        tools = LogTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.get_logs(
                **_call_kwargs(source="logbook", entity_id="kitchen_light")
            )
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "invalid entity ID" in payload["error"]["message"]
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_timeout_surfaces_without_a_rest_fallback(
        self, logbook_component_caps
    ) -> None:
        client = self._client(ws_error=_real_timeout_shape())
        tools = LogTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.get_logs(**_call_kwargs(source="logbook"))
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "TIMEOUT_OPERATION"
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_timeout_surfaces_without_a_rest_fallback(
        self, logbook_component_caps
    ) -> None:
        # Defensive branch: a caller/mock raising the timeout untranslated.
        client = self._client(
            ws_error=HomeAssistantCommandTimeout("logbook query timed out")
        )
        tools = LogTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.get_logs(**_call_kwargs(source="logbook"))
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "TIMEOUT_OPERATION"
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wrapped_timeout_on_the_device_path_surfaces(self) -> None:
        # No component: a device_id request uses the WebSocket read, and a
        # timed-out recorder query must not be reported as "no WebSocket".
        client = self._client(ws_error=_real_timeout_shape())
        tools = LogTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.get_logs(**_call_kwargs(source="logbook", device_id="dev1"))
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "TIMEOUT_OPERATION"
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_component_absent_uses_rest_with_names(self) -> None:
        client = self._client()
        client.get_logbook = AsyncMock(return_value=self._component_rows())
        tools = LogTools(client)
        result = await tools.get_logs(**_call_kwargs(source="logbook"))
        client.send_websocket_message.assert_not_awaited()
        by_entity = {e["entity_id"]: e for e in result["data"]["entries"]}
        assert by_entity["light.kitchen"]["name"] == "Kitchen Light"

    @pytest.mark.asyncio
    async def test_device_filter_uses_websocket_when_component_is_absent(self) -> None:
        client = self._client(ws_result=self._ws_events())
        tools = LogTools(client)
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", device_id="dev1")
        )
        command = client.send_websocket_message.await_args.args[0]
        assert command["type"] == "logbook/get_events"
        assert command["device_ids"] == ["dev1"]
        client.get_logbook.assert_not_awaited()
        by_entity = {e["entity_id"]: e for e in result["data"]["entries"]}
        assert (
            by_entity["light.kitchen"]["when"]
            == datetime.fromtimestamp(1780000000.0, UTC).isoformat()
        )
        assert "name" not in by_entity["light.kitchen"]
        assert any("carry no 'name'" in w for w in result["warnings"])
        assert "warnings" not in result["data"]

    @pytest.mark.asyncio
    async def test_device_websocket_empty_result_has_no_name_warning(self) -> None:
        client = self._client(ws_result=[])
        tools = LogTools(client)
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", device_id="dev1")
        )
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_device_websocket_named_rows_have_no_name_warning(self) -> None:
        client = self._client(
            ws_result=[
                {
                    "name": "Movie night",
                    "message": "started",
                    "domain": "automation",
                    "entity_id": "automation.movie_night",
                    "when": 1780000060.0,
                }
            ]
        )
        tools = LogTools(client)
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", device_id="dev1")
        )
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_context_filter_falls_back_to_rest(self) -> None:
        client = self._client(ws_error=HomeAssistantConnectionError("no WS"))
        tools = LogTools(client)
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", context_id="ctx1")
        )
        assert result["data"]["context_filter"] == "ctx1"
        assert client.get_logbook.await_args.kwargs["context_id"] == "ctx1"

    @pytest.mark.asyncio
    async def test_device_filter_requires_the_websocket_path(self) -> None:
        client = self._client(ws_error=HomeAssistantConnectionError("no WS"))
        tools = LogTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.get_logs(**_call_kwargs(source="logbook", device_id="dev1"))
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "CONNECTION_FAILED"
        client.get_logbook.assert_not_awaited()

    @pytest.mark.parametrize(
        "envelope",
        [
            {"success": False, "error": {"code": "forbidden"}},
            {"success": False, "error_code": "unknown_command"},
        ],
    )
    @pytest.mark.asyncio
    async def test_device_soft_failure_envelope_is_not_an_empty_success(
        self, envelope: dict[str, Any]
    ) -> None:
        # No REST fallback exists for a device filter: a tolerated soft
        # failure must surface, not read as "no entries".
        client = self._client(ws_payload=envelope)
        tools = LogTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.get_logs(**_call_kwargs(source="logbook", device_id="dev1"))
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "CONNECTION_FAILED"
        assert "device_id" in payload["error"]["message"]
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blank_entity_filter_is_rejected(self) -> None:
        client = self._client()
        tools = LogTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.get_logs(**_call_kwargs(source="logbook", entity_id=", "))
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_awaited()
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blank_device_filter_is_rejected(self) -> None:
        client = self._client()
        tools = LogTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.get_logs(**_call_kwargs(source="logbook", device_id=" , "))
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_awaited()
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rest_fallback_uses_normalized_entity_ids(self) -> None:
        client = self._client()
        tools = LogTools(client)
        await tools.get_logs(
            **_call_kwargs(source="logbook", entity_id="light.a, light.b")
        )
        assert client.get_logbook.await_args.kwargs["entity_id"] == "light.a,light.b"

    @pytest.mark.asyncio
    async def test_context_id_cannot_combine_with_other_filters(self) -> None:
        for extra in ({"entity_id": "light.a"}, {"device_id": "dev1"}):
            client = self._client()
            tools = LogTools(client)
            with pytest.raises(ToolError) as exc_info:
                await tools.get_logs(
                    **_call_kwargs(source="logbook", context_id="ctx1", **extra)
                )
            payload = json.loads(str(exc_info.value))
            assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
            client.send_websocket_message.assert_not_awaited()
            client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_omitted_end_time_still_covers_the_requested_window(self) -> None:
        client = self._client()
        tools = LogTools(client)
        fixed_now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)

        with patch("ha_mcp.tools.log_sources_logbook.datetime") as datetime_mock:
            datetime_mock.now.return_value = fixed_now
            await tools.get_logs(**_call_kwargs(source="logbook", hours_back=48))

        params = client.get_logbook.await_args.kwargs
        assert params["end_time"] == fixed_now.isoformat()
        start = datetime.fromisoformat(params["start_time"])
        end = datetime.fromisoformat(params["end_time"])
        assert end - start == timedelta(hours=48)

    @pytest.mark.asyncio
    async def test_explicit_end_time_still_honors_the_callers_window(self) -> None:
        client = self._client()
        tools = LogTools(client)
        await tools.get_logs(
            **_call_kwargs(
                source="logbook", hours_back=48, end_time="2026-06-15T00:00:00Z"
            )
        )
        params = client.get_logbook.await_args.kwargs
        start = datetime.fromisoformat(params["start_time"])
        end = datetime.fromisoformat(params["end_time"])
        assert end == datetime(2026, 6, 15, tzinfo=UTC)
        assert end - start == timedelta(hours=48)

    @pytest.mark.asyncio
    async def test_omitted_and_explicit_end_time_cover_the_same_window(self) -> None:
        fixed_now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
        client = self._client()
        client.get_logbook = AsyncMock(return_value=self._component_rows())
        tools = LogTools(client)

        with patch("ha_mcp.tools.log_sources_logbook.datetime") as datetime_mock:
            datetime_mock.now.return_value = fixed_now
            datetime_mock.fromisoformat.side_effect = datetime.fromisoformat
            omitted_result = await tools.get_logs(
                **_call_kwargs(source="logbook", hours_back=48)
            )
            omitted = client.get_logbook.await_args.kwargs
            explicit_result = await tools.get_logs(
                **_call_kwargs(
                    source="logbook",
                    hours_back=48,
                    end_time=fixed_now.isoformat(),
                )
            )
            explicit = client.get_logbook.await_args.kwargs

        # Same forwarded bounds AND same returned window (#2767).
        assert omitted == explicit
        assert omitted_result["data"]["entries"] == explicit_result["data"]["entries"]
        assert omitted_result["data"]["total_entries"] == 2
        assert explicit_result["data"]["total_entries"] == 2

    @pytest.mark.asyncio
    async def test_pagination_hint_keeps_pages_in_the_same_window(self) -> None:
        client = self._client()
        client.get_logbook = AsyncMock(return_value=self._component_rows())
        tools = LogTools(client)
        fixed_now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)

        with patch("ha_mcp.tools.log_sources_logbook.datetime") as datetime_mock:
            datetime_mock.now.return_value = fixed_now
            datetime_mock.fromtimestamp.side_effect = datetime.fromtimestamp
            result = await tools.get_logs(**_call_kwargs(source="logbook", limit=1))

        hint = result["data"]["pagination_hint"]
        assert f"end_time='{fixed_now.isoformat()}'" in hint


class TestLogbookErrorSuggestions:
    """Status-aware remediation text for the REST fallback."""

    def test_500_names_input_shape_and_the_error_log_tool(self) -> None:
        exc = HomeAssistantAPIError(
            "API error: 500 - 500 Internal Server Error", status_code=500
        )
        suggestions = LogTools._logbook_error_suggestions(exc)
        assert any("domain.object_id" in s for s in suggestions)
        assert any("ha_get_logs(source='error_log')" in s for s in suggestions)
        assert not any("too many results" in s.lower() for s in suggestions)

    def test_400_is_reported_as_validation(self) -> None:
        exc = HomeAssistantAPIError("API error: 400 - Bad Request", status_code=400)
        suggestions = LogTools._logbook_error_suggestions(exc)
        assert any("rejected the request as invalid" in s for s in suggestions)
        assert any("domain.object_id" in s for s in suggestions)

    def test_other_failures_keep_scope_hints(self) -> None:
        suggestions = LogTools._logbook_error_suggestions(
            HomeAssistantConnectionError("boom")
        )
        assert any("hours_back" in s for s in suggestions)


class TestLogbookRestClient:
    """The REST fallback forwards every supported logbook filter."""

    @pytest.mark.asyncio
    async def test_get_logbook_sends_the_context_query_param(self) -> None:
        from ha_mcp.client.rest_client import HomeAssistantClient

        client = AsyncMock()
        client._request = AsyncMock(return_value=[])
        await HomeAssistantClient.get_logbook(
            client,
            entity_id="light.kitchen",
            start_time="2026-06-16T00:00:00+00:00",
            end_time="2026-06-16T12:00:00+00:00",
            context_id="ctx1",
        )
        kwargs = client._request.await_args.kwargs
        assert kwargs["params"]["context_id"] == "ctx1"
        assert kwargs["params"]["entity"] == "light.kitchen"
