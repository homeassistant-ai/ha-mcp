"""Unit tests for ``ha_get_logs`` ordering and system-log message capping.

Covers newest-first (default) vs oldest-first ordering for every time-ordered
source, plus the warning emitted when ``order`` is supplied to the non
time-ordered ``logger`` source. The logbook windowing mirrors the traces
newest-first fix (#1178). ``TestSystemMessageCap`` covers how ``compact``
caps oversized ``system`` message strings.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.models import ErrorLogPage
from ha_mcp.client.rest_client import (
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.component_api import ComponentCaps
from ha_mcp.tools.log_sources import _SYSTEM_LOG_MESSAGE_CAP
from ha_mcp.tools.tools_logs import LogTools

from ._logbook_fallback import disable_logbook_websocket


@pytest.fixture(autouse=True)
def _no_real_caps_probe():
    """Keep the ``ha_mcp_tools/info`` caps probe from opening a real socket.

    The mock clients here carry a real-looking ``base_url``/``token`` but never
    patch ``get_websocket_client``, so ``get_component_caps`` (invoked by the
    logbook path's ``add_timezone_metadata`` for every completed
    ``ha_get_logs`` call) would otherwise attempt a real WS connection and pay
    its full failure latency per test. Forcing a
    ``HomeAssistantConnectionError`` reproduces the "component absent" outcome
    these tests already assume, without the real connection attempt (mirrors
    ``test_tools_history.py``'s fixture of the same name).
    """
    with patch(
        "ha_mcp.tools.component_api.get_websocket_client",
        AsyncMock(side_effect=HomeAssistantConnectionError("no WS in unit tests")),
    ):
        yield


@pytest.fixture
def component_caps():
    """Pretend the component advertises ``logbook_events``."""
    caps = ComponentCaps(
        schema_version=1,
        component_version="2.2.3",
        capabilities=frozenset({"logbook_events"}),
        limits={},
    )
    with patch(
        "ha_mcp.tools.log_sources_logbook.get_component_caps",
        AsyncMock(return_value=caps),
    ):
        yield caps


def _call_kwargs(**overrides):
    """Full keyword set for ``LogTools.get_logs`` with sensible defaults."""
    base = {
        "source": "logbook",
        "limit": None,
        "search": None,
        "hours_back": 1,
        "entity_id": None,
        "device_id": None,
        "context_id": None,
        "end_time": None,
        "offset": 0,
        "compact": True,
        "level": None,
        "slug": None,
    }
    base.update(overrides)
    return base


class TestLogbookOrder:
    """source='logbook' — offset pagination over a reversible window."""

    @staticmethod
    def _client(entries):
        client = AsyncMock()
        client.get_logbook = AsyncMock(return_value=entries)
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        disable_logbook_websocket(client)
        return client

    @staticmethod
    def _entries():
        # Oldest-first, exactly as HA's /logbook REST endpoint returns them.
        return [
            {
                "when": f"2026-06-16T00:0{i}:00+00:00",
                "entity_id": "light.x",
                "state": f"s{i}",  # s0 oldest ... s4 newest
            }
            for i in range(5)
        ]

    @pytest.mark.asyncio
    async def test_newest_is_default_and_reverses_window(self):
        tools = LogTools(self._client(self._entries()))
        result = await tools.get_logs(**_call_kwargs(source="logbook", limit=2))
        data = result["data"]
        assert data["order"] == "newest"
        assert [e["state"] for e in data["entries"]] == ["s4", "s3"]
        assert data["offset"] == 0
        assert data["total_entries"] == 5
        assert data["has_more"] is True

    @pytest.mark.asyncio
    async def test_oldest_returns_chronological_window(self):
        tools = LogTools(self._client(self._entries()))
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", limit=2, order="oldest")
        )
        data = result["data"]
        assert data["order"] == "oldest"
        assert [e["state"] for e in data["entries"]] == ["s0", "s1"]
        assert data["has_more"] is True

    @pytest.mark.asyncio
    async def test_newest_offset_pages_into_older_entries(self):
        tools = LogTools(self._client(self._entries()))
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", limit=2, offset=2)
        )
        data = result["data"]
        # total=5, offset=2 -> end=3, start=1, response[1:3]=[s1,s2] reversed.
        assert [e["state"] for e in data["entries"]] == ["s2", "s1"]
        assert data["has_more"] is True  # offset(2) + len(2) = 4 < 5

    @pytest.mark.asyncio
    async def test_oldest_pagination_hint_carries_order(self):
        tools = LogTools(self._client(self._entries()))
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", limit=2, order="oldest")
        )
        # Quoted: the hint is meant to be copied back as a call.
        assert "order='oldest'" in result["data"]["pagination_hint"]

    @pytest.mark.asyncio
    async def test_newest_default_hint_omits_order(self):
        tools = LogTools(self._client(self._entries()))
        result = await tools.get_logs(**_call_kwargs(source="logbook", limit=2))
        assert "order=" not in result["data"]["pagination_hint"]

    @pytest.mark.asyncio
    async def test_newest_offset_beyond_total_returns_empty(self):
        tools = LogTools(self._client(self._entries()))
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", limit=2, offset=10)
        )
        data = result["data"]
        # end = 5 - 10 < 0 -> guarded to []; no negative-index slice.
        assert data["entries"] == []
        assert data["has_more"] is False
        assert data["offset"] == 10

    @pytest.mark.asyncio
    async def test_newest_limit_exceeds_total_returns_all_reversed(self):
        tools = LogTools(self._client(self._entries()))
        result = await tools.get_logs(**_call_kwargs(source="logbook", limit=100))
        data = result["data"]
        # start clamped to 0 via max(end - limit, 0).
        assert [e["state"] for e in data["entries"]] == ["s4", "s3", "s2", "s1", "s0"]
        assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_newest_last_page_sets_has_more_false(self):
        tools = LogTools(self._client(self._entries()))
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", limit=2, offset=4)
        )
        data = result["data"]
        # total=5, offset=4 -> end=1, start=0, response[0:1]=[s0] reversed.
        assert [e["state"] for e in data["entries"]] == ["s0"]
        assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_newest_filters_by_search_then_orders(self):
        entries = [
            {
                "when": f"2026-06-16T00:0{i}:00+00:00",
                "entity_id": "light.match" if i % 2 == 0 else "light.other",
                "state": f"s{i}",
            }
            for i in range(5)
        ]
        tools = LogTools(self._client(entries))
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", limit=10, search="match")
        )
        data = result["data"]
        # s0,s2,s4 match (oldest-first); newest order reverses to s4,s2,s0.
        assert [e["state"] for e in data["entries"]] == ["s4", "s2", "s0"]
        assert data["total_entries"] == 3
        assert data["filters_applied"]["search"] == "match"

    @pytest.mark.asyncio
    async def test_non_list_response_still_echoes_order(self):
        # If HA returns a non-list shape, the windowing is skipped but the
        # response must still echo `order` and not page.
        client = AsyncMock()
        client.get_logbook = AsyncMock(return_value={"unexpected": "shape"})
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        disable_logbook_websocket(client)
        tools = LogTools(client)
        result = await tools.get_logs(**_call_kwargs(source="logbook", limit=2))
        data = result["data"]
        assert data["order"] == "newest"
        assert data["has_more"] is False
        assert data["total_entries"] == 1


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
    async def test_component_rows_pass_through_untouched(self, component_caps) -> None:
        rows = self._component_rows()
        client = self._client(ws_payload={"success": True, "result": {"events": rows}})
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
    async def test_component_receives_the_native_filters(self, component_caps) -> None:
        client = self._client(ws_payload={"success": True, "result": {"events": []}})
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
        self, component_caps
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
    async def test_component_rejection_is_authoritative(self, component_caps) -> None:
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
    async def test_timeout_surfaces_without_a_rest_fallback(
        self, component_caps
    ) -> None:
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
        tools = LogTools(client)

        with patch("ha_mcp.tools.log_sources_logbook.datetime") as datetime_mock:
            datetime_mock.now.return_value = fixed_now
            datetime_mock.fromisoformat.side_effect = datetime.fromisoformat
            await tools.get_logs(**_call_kwargs(source="logbook", hours_back=48))
            omitted = client.get_logbook.await_args.kwargs
            await tools.get_logs(
                **_call_kwargs(
                    source="logbook",
                    hours_back=48,
                    end_time=fixed_now.isoformat(),
                )
            )
            explicit = client.get_logbook.await_args.kwargs

        assert omitted == explicit

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


class TestSystemOrder:
    """source='system' — sorted deterministically by entry timestamp."""

    @staticmethod
    def _client():
        client = AsyncMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {
                        "name": "a",
                        "message": ["m"],
                        "level": "ERROR",
                        "timestamp": 100.0,
                    },
                    {
                        "name": "b",
                        "message": ["m"],
                        "level": "ERROR",
                        "timestamp": 300.0,
                    },
                    {
                        "name": "c",
                        "message": ["m"],
                        "level": "ERROR",
                        "timestamp": 200.0,
                    },
                ],
            }
        )
        return client

    @pytest.mark.asyncio
    async def test_newest_sorts_timestamp_descending(self):
        tools = LogTools(self._client())
        result = await tools.get_logs(**_call_kwargs(source="system"))
        assert result["order"] == "newest"
        assert [e["timestamp"] for e in result["entries"]] == [300.0, 200.0, 100.0]

    @pytest.mark.asyncio
    async def test_oldest_sorts_timestamp_ascending(self):
        tools = LogTools(self._client())
        result = await tools.get_logs(**_call_kwargs(source="system", order="oldest"))
        assert result["order"] == "oldest"
        assert [e["timestamp"] for e in result["entries"]] == [100.0, 200.0, 300.0]

    @pytest.mark.asyncio
    async def test_tolerates_missing_none_and_non_dict_entries(self):
        # system_log/list does not guarantee a numeric timestamp on every
        # record; the sort key must not raise a cross-type TypeError.
        client = AsyncMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {"name": "has_ts", "level": "ERROR", "timestamp": 100.0},
                    {"name": "no_ts", "level": "ERROR"},
                    {"name": "none_ts", "level": "ERROR", "timestamp": None},
                    "not-a-dict",
                ],
            }
        )
        tools = LogTools(client)
        result = await tools.get_logs(**_call_kwargs(source="system"))
        assert result["order"] == "newest"
        assert result["returned_entries"] == 4
        # The only timestamped entry sorts newest-first; keyless/non-dict
        # entries collapse to 0.0 and trail it.
        assert result["entries"][0]["timestamp"] == 100.0


class TestLogbookEndTimeValidation:
    """source='logbook' — a bad end_time must fail with the structured shape."""

    @pytest.mark.asyncio
    async def test_invalid_end_time_raises_structured_validation_error(self):
        """`datetime.fromisoformat` runs before the fetch try-block; without
        the guard a raw ValueError escapes the ToolError contract."""
        client = AsyncMock()
        tools = LogTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.get_logs(**_call_kwargs(source="logbook", end_time="invalid"))
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "end_time" in payload["error"]["message"]
        client.get_logbook.assert_not_called()


class TestSystemMalformedEntries:
    """source='system' — malformed records must not escape the error envelope."""

    @pytest.mark.asyncio
    async def test_non_dict_entries_are_dropped_by_filters(self):
        """A non-dict record would AttributeError inside the level/search
        comprehensions and leave the tool without its structured ToolError
        shape; a filter drops it instead (unfiltered calls keep it, pinned
        by test_tolerates_missing_none_and_non_dict_entries)."""
        client = AsyncMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    "bogus string record",
                    {
                        "name": "a",
                        "message": ["m"],
                        "level": "ERROR",
                        "timestamp": 100.0,
                    },
                ],
            }
        )
        tools = LogTools(client)
        result = await tools.get_logs(
            **_call_kwargs(source="system", level="ERROR", search="m")
        )
        assert result["total_entries"] == 1
        assert [e["name"] for e in result["entries"]] == ["a"]


class TestRawTextOrder:
    """error_log / supervisor / system_service — reversible most-recent window."""

    @pytest.mark.asyncio
    async def test_error_log_newest_reverses_recent_window(self):
        client = AsyncMock()
        client.get_error_log = AsyncMock(
            return_value=ErrorLogPage(text="l0\nl1\nl2\nl3", has_more=False)
        )
        tools = LogTools(client)
        result = await tools.get_logs(**_call_kwargs(source="error_log", limit=2))
        assert result["order"] == "newest"
        assert result["log"] == "l3\nl2"  # newest line first
        assert result["returned_lines"] == 2
        assert result["total_lines"] == 4

    @pytest.mark.asyncio
    async def test_error_log_oldest_keeps_chronological_window(self):
        client = AsyncMock()
        client.get_error_log = AsyncMock(
            return_value=ErrorLogPage(text="l0\nl1\nl2\nl3", has_more=False)
        )
        tools = LogTools(client)
        result = await tools.get_logs(
            **_call_kwargs(source="error_log", limit=2, order="oldest")
        )
        assert result["order"] == "oldest"
        assert result["log"] == "l2\nl3"  # recent window, ascending

    @pytest.mark.asyncio
    async def test_supervisor_newest_reverses(self):
        client = AsyncMock()
        client.get_addon_logs = AsyncMock(return_value="a\nb\nc\nd")
        tools = LogTools(client)
        result = await tools.get_logs(
            **_call_kwargs(source="supervisor", slug="core_x", limit=2)
        )
        assert result["order"] == "newest"
        assert result["log"] == "d\nc"
        assert result["slug"] == "core_x"

    @pytest.mark.asyncio
    async def test_system_service_oldest_keeps_chronological(self):
        client = AsyncMock()
        client._get_system_service_logs = AsyncMock(return_value="a\nb\nc\nd")
        tools = LogTools(client)
        result = await tools.get_logs(
            **_call_kwargs(
                source="system_service", slug="supervisor", limit=2, order="oldest"
            )
        )
        assert result["order"] == "oldest"
        assert result["log"] == "c\nd"


class TestLoggerOrderWarning:
    """source='logger' is not time-ordered: 'order' is ignored, with a warning."""

    @staticmethod
    def _client():
        client = AsyncMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [{"domain": "homeassistant", "level": 20}],
            }
        )
        return client

    @pytest.mark.asyncio
    async def test_non_default_order_warns_and_is_ignored(self):
        tools = LogTools(self._client())
        result = await tools.get_logs(**_call_kwargs(source="logger", order="oldest"))
        assert "order" not in result  # logger response has no order field
        assert any("order" in w for w in result.get("warnings", []))

    @pytest.mark.asyncio
    async def test_default_order_emits_no_warning(self):
        tools = LogTools(self._client())
        result = await tools.get_logs(**_call_kwargs(source="logger"))
        assert "warnings" not in result


def _blocking_call_message() -> str:
    """A system_log blocking-call warning in Home Assistant's own field order.

    ``raise_for_blocking_call`` formats the call's args (here a large bytes
    repr) before the integration and a ``format_stack()`` of the whole stack,
    so the frames that identify the offender come last.
    """
    frames = "".join(
        f'  File "/usr/src/homeassistant/homeassistant/core.py", line {n}, '
        f"in _run\n    self._loop.run_forever()\n"
        for n in range(60)
    )
    return (
        "Detected blocking call to write_bytes with args "
        "(PosixPath('/config/www/snap.jpg'), b'" + "\\xff" * 50_000 + "') "
        "inside the event loop by custom integration 'pyscript' at "
        "custom_components/pyscript/eval.py, line 1915: return func(*args)\n"
        "Traceback (most recent call last):\n"
        + frames
        + '  File "/config/custom_components/pyscript/eval.py", line 1915, '
        "in call_func\n    return func(*args, **kwargs)\n"
    )


class TestSystemMessageCap:
    """source='system' — compact caps oversized messages; compact=False lifts it."""

    _LAST_FRAME = '  File "/config/custom_components/pyscript/eval.py", line 1915'

    @staticmethod
    def _client(*records: Any) -> AsyncMock:
        client = AsyncMock()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": list(records)}
        )
        return client

    @pytest.mark.asyncio
    async def test_compact_keeps_both_ends_of_an_oversized_message(self) -> None:
        # The lead names the call and the stack's innermost frames name the
        # offender; the bytes repr between them is what has to go.
        message = _blocking_call_message()
        tools = LogTools(
            self._client(
                {"name": "homeassistant.util.loop", "message": [message, "short"]}
            )
        )
        result = await tools.get_logs(**_call_kwargs(source="system"))
        capped, short = result["entries"][0]["message"]
        assert len(capped) <= _SYSTEM_LOG_MESSAGE_CAP
        assert capped.startswith("Detected blocking call to write_bytes")
        assert self._LAST_FRAME in capped
        cut = (
            len(message)
            - len(capped.split("…[", 1)[0])
            - len(capped.rsplit("]…", 1)[1])
        )
        assert f"{cut:,} chars cut" in capped
        assert "limit=1" in capped
        assert short == "short"
        assert result["truncated_messages"] == 1

    @pytest.mark.asyncio
    async def test_compact_false_returns_the_full_message(self) -> None:
        message = _blocking_call_message()
        tools = LogTools(self._client({"name": "a", "message": [message]}))
        result = await tools.get_logs(**_call_kwargs(source="system", compact=False))
        assert result["entries"][0]["message"] == [message]
        assert "truncated_messages" not in result

    @pytest.mark.asyncio
    async def test_compact_never_caps_the_traceback(self) -> None:
        traceback = "Traceback (most recent call last):\n" + "  frame\n" * 1_000
        assert len(traceback) > _SYSTEM_LOG_MESSAGE_CAP
        tools = LogTools(
            self._client({"name": "a", "message": ["m"], "exception": traceback})
        )
        result = await tools.get_logs(**_call_kwargs(source="system"))
        assert result["entries"][0]["exception"] == traceback

    @pytest.mark.asyncio
    async def test_search_matches_text_inside_the_cut(self) -> None:
        # The filter reads the full message before the cap, so an entry whose
        # only match falls inside the cut is still returned, without the match.
        message = "lead " + "a" * 50_000 + "needle" + "a" * 50_000 + " tail"
        tools = LogTools(self._client({"name": "a", "message": [message]}))
        result = await tools.get_logs(**_call_kwargs(source="system", search="needle"))
        assert result["returned_entries"] == 1
        assert "needle" not in result["entries"][0]["message"][0]

    @pytest.mark.asyncio
    async def test_compact_handles_every_message_shape_and_malformed_record(
        self,
    ) -> None:
        tools = LogTools(
            self._client(
                {"name": "s", "message": _blocking_call_message(), "timestamp": 3.0},
                {"name": "mixed", "message": [123], "timestamp": 2.0},
                {"name": "none", "timestamp": 1.0},
                "not-a-dict",
            )
        )
        result = await tools.get_logs(**_call_kwargs(source="system"))
        as_str, mixed, absent, bogus = result["entries"]
        assert isinstance(as_str["message"], str)
        assert len(as_str["message"]) <= _SYSTEM_LOG_MESSAGE_CAP
        assert mixed["message"] == [123]
        assert "message" not in absent
        assert bogus == "not-a-dict"
        assert result["truncated_messages"] == 1
