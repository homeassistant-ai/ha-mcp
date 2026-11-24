"""``ha_get_logs`` logbook transports against Core's own logbook.

The rows come from Core's ``logbook`` and ``recorder`` integrations over the
component command, Core's REST view and Core's WebSocket command. The only
stand-in is a client that gets no answer at all.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, contextmanager
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.recorder import Recorder
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_logs import LogTools

from ._command_faults import dropped_on, pooled_socket, unanswered
from ._log_tool_calls import call_kwargs

# Core closes a socket whose token it refused; the client's close reply then
# re-arms aiohttp's heartbeat timer on that closed server socket.
_REFUSED_SOCKET_TIMER = pytest.mark.parametrize("expected_lingering_timers", [True])

COMPONENT_COMMAND = "ha_mcp_tools/logbook_events"
CORE_COMMAND = "logbook/get_events"


@pytest.fixture(autouse=True)
def _recorder(recorder_mock: Recorder) -> None:
    """Core's recorder, requested before ``hass`` is built as it requires."""


@pytest.fixture
async def logbook(hass: HomeAssistant) -> None:
    """Core's logbook on its recorder, with a kitchen light that turned on."""
    assert await async_setup_component(hass, "logbook", {})
    attributes = {"friendly_name": "Kitchen Light"}
    hall = {"friendly_name": "Hall"}
    hass.states.async_set("light.kitchen", "off", attributes)
    hass.states.async_set("light.hall", "off", hall)
    await hass.async_block_till_done()
    hass.states.async_set("light.kitchen", "on", attributes)
    hass.states.async_set("light.hall", "on", hall)
    await async_wait_recording_done(hass)


@pytest.fixture
async def kitchen_device(
    hass: HomeAssistant,
    logbook: None,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> str:
    """The id of a registered device that owns ``light.kitchen``."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "kitchen")}
    )
    entity_registry.async_get_or_create(
        "light",
        "test",
        "kitchen",
        suggested_object_id="kitchen",
        device_id=device.id,
        config_entry=entry,
    )
    return device.id


@pytest.fixture
async def rejected_client(
    core_client: HomeAssistantClient,
) -> AsyncIterator[HomeAssistantClient]:
    """A client of the same Core whose token Core does not know."""
    async with AsyncExitStack() as stack:
        client = HomeAssistantClient(
            base_url=core_client.base_url, token="unknown-token", verify_ssl=False
        )
        stack.push_async_callback(client.close)
        yield client


@contextmanager
def rest_reads(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """Spy on the client's REST logbook read; Core still answers it."""
    with patch.object(client, "get_logbook", wraps=client.get_logbook) as spy:
        yield spy


def sent_types(sent: AsyncMock) -> list[str]:
    """The command types the client sent over its WebSocket bridge."""
    return [call.args[0]["type"] for call in sent.await_args_list]


def sent_command(sent: AsyncMock, command: str) -> dict[str, Any]:
    """The first message of this type the client sent."""
    return next(
        call.args[0] for call in sent.await_args_list if call.args[0]["type"] == command
    )


async def logbook_error(tools: LogTools, **kwargs: Any) -> dict[str, Any]:
    """The structured error ``ha_get_logs`` raised for these arguments."""
    with pytest.raises(ToolError) as exc_info:
        await tools.get_logs(**call_kwargs(source="logbook", **kwargs))
    payload: dict[str, Any] = json.loads(str(exc_info.value))
    error: dict[str, Any] = payload["error"]
    return error


def by_entity(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["entity_id"]: row for row in result["data"]["entries"]}


@pytest.mark.usefixtures("logbook")
async def test_component_rows_are_cores_own_and_skip_the_rest_view(
    component_core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """With the component the logbook is read in-process: names and ISO times
    come from Core and the REST view is never asked."""
    with rest_reads(component_core_client) as rest:
        result = await LogTools(component_core_client).get_logs(
            **call_kwargs(source="logbook")
        )

    command = sent_command(sent, COMPONENT_COMMAND)
    assert command["start_time"] < command["end_time"]
    rest.assert_not_awaited()
    kitchen = by_entity(result)["light.kitchen"]
    assert kitchen["name"] == "Kitchen Light"
    assert dt_util.parse_datetime(kitchen["when"]) is not None


async def test_component_receives_the_entity_and_device_filters(
    component_core_client: HomeAssistantClient, kitchen_device: str, sent: AsyncMock
) -> None:
    result = await LogTools(component_core_client).get_logs(
        **call_kwargs(
            source="logbook",
            entity_id="light.kitchen, light.hall",
            device_id=kitchen_device,
        )
    )

    command = sent_command(sent, COMPONENT_COMMAND)
    assert command["entity_ids"] == ["light.kitchen", "light.hall"]
    assert command["device_ids"] == [kitchen_device]
    assert set(by_entity(result)) == {"light.kitchen", "light.hall"}


@pytest.mark.usefixtures("logbook")
async def test_component_that_lost_the_command_falls_back_to_rest(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """An older component answers ``unknown_command``: the read still succeeds
    through Core's REST view."""
    tools = LogTools(component_core_client)
    await tools.get_logs(**call_kwargs(source="logbook"))
    assert COMPONENT_COMMAND in sent_types(sent)

    del hass.data[websocket_api.DOMAIN][COMPONENT_COMMAND]
    sent.reset_mock()
    with rest_reads(component_core_client) as rest:
        result = await tools.get_logs(**call_kwargs(source="logbook"))

    assert COMPONENT_COMMAND in sent_types(sent)
    rest.assert_awaited()
    assert by_entity(result)["light.kitchen"]["name"] == "Kitchen Light"


async def test_component_rejection_is_authoritative(
    component_core_client: HomeAssistantClient,
) -> None:
    """Core's logbook is not set up: the component's refusal reaches the caller
    and the REST view is not tried."""
    with rest_reads(component_core_client) as rest:
        error = await logbook_error(LogTools(component_core_client))

    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "logbook integration is not set up" in error["message"]
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_component_invalid_entity_id_is_a_validation_error(
    component_core_client: HomeAssistantClient,
) -> None:
    with rest_reads(component_core_client) as rest:
        error = await logbook_error(
            LogTools(component_core_client), entity_id="kitchen_light"
        )

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "invalid entity ID" in error["message"]
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_component_timeout_surfaces_without_a_rest_fallback(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorder query still running inside Core is not run a second time."""
    with rest_reads(component_core_client) as rest:
        async with unanswered(hass, COMPONENT_COMMAND, monkeypatch):
            error = await logbook_error(LogTools(component_core_client))

    assert error["code"] == "TIMEOUT_OPERATION"
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_component_dropped_connection_falls_back_to_rest(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    socket = await pooled_socket(component_core_client)
    with (
        rest_reads(component_core_client) as rest,
        dropped_on(hass, COMPONENT_COMMAND, socket),
    ):
        result = await LogTools(component_core_client).get_logs(
            **call_kwargs(source="logbook")
        )

    rest.assert_awaited()
    assert by_entity(result)["light.kitchen"]["name"] == "Kitchen Light"


@_REFUSED_SOCKET_TIMER
@pytest.mark.usefixtures("logbook", "component_core_client")
async def test_component_rejected_token_ends_in_an_auth_error(
    rejected_client: HomeAssistantClient,
) -> None:
    """The capability probe and the REST fallback both carry the token Core
    refused, so the caller is told about the token."""
    error = await logbook_error(LogTools(rejected_client))

    assert error["code"] == "AUTH_INVALID_TOKEN"


@pytest.mark.usefixtures("logbook")
async def test_without_the_component_rest_rows_carry_names(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    result = await LogTools(core_client).get_logs(**call_kwargs(source="logbook"))

    assert COMPONENT_COMMAND not in sent_types(sent)
    assert CORE_COMMAND not in sent_types(sent)
    assert by_entity(result)["light.kitchen"]["name"] == "Kitchen Light"


async def test_device_filter_reads_cores_websocket_rows_without_names(
    core_client: HomeAssistantClient, kitchen_device: str, sent: AsyncMock
) -> None:
    """Core's WebSocket rows have epoch times and no names: the tool converts
    the times and warns that names are missing."""
    with rest_reads(core_client) as rest:
        result = await LogTools(core_client).get_logs(
            **call_kwargs(
                source="logbook", device_id=kitchen_device, entity_id="light.kitchen"
            )
        )

    assert sent_command(sent, CORE_COMMAND)["device_ids"] == [kitchen_device]
    rest.assert_not_awaited()
    assert set(by_entity(result)) == {"light.kitchen"}
    kitchen = by_entity(result)["light.kitchen"]
    assert datetime.fromisoformat(kitchen["when"]).tzinfo is not None
    assert "name" not in kitchen
    assert any("carry no 'name'" in warning for warning in result["warnings"])
    assert "warnings" not in result["data"]


async def test_device_without_events_has_no_name_warning(
    core_client: HomeAssistantClient, kitchen_device: str
) -> None:
    result = await LogTools(core_client).get_logs(
        **call_kwargs(
            source="logbook", device_id=kitchen_device, entity_id="light.quiet"
        )
    )

    assert result["data"]["entries"] == []
    assert "warnings" not in result


async def test_device_rows_that_carry_a_name_have_no_name_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient, kitchen_device: str
) -> None:
    """A logbook entry (name and message, no state) needs no component names."""
    await hass.services.async_call(
        "logbook",
        "log",
        {"name": "Movie night", "message": "started", "entity_id": "light.porch"},
        blocking=True,
    )
    await async_wait_recording_done(hass)

    result = await LogTools(core_client).get_logs(
        **call_kwargs(
            source="logbook", device_id=kitchen_device, entity_id="light.porch"
        )
    )

    movie = [e for e in result["data"]["entries"] if e.get("message") == "started"]
    assert movie[0]["name"] == "Movie night"
    assert "warnings" not in result


@pytest.mark.usefixtures("logbook")
async def test_context_filter_reads_the_rest_view(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    context = Context()
    hass.states.async_set("light.porch", "off")
    hass.states.async_set("light.porch", "on", context=context)
    await async_wait_recording_done(hass)

    with rest_reads(core_client) as rest:
        result = await LogTools(core_client).get_logs(
            **call_kwargs(source="logbook", context_id=context.id)
        )

    assert result["data"]["context_filter"] == context.id
    assert set(by_entity(result)) == {"light.porch"}
    assert rest.await_args_list[-1].kwargs["context_id"] == context.id
    assert CORE_COMMAND not in sent_types(sent)


@pytest.mark.usefixtures("logbook")
async def test_device_filter_without_the_websocket_is_a_connection_error(
    core_client: HomeAssistantClient,
) -> None:
    """No REST transport has a device filter, so a dead WebSocket is surfaced
    instead of read as "no entries"."""
    with (
        rest_reads(core_client) as rest,
        patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("no WS")),
        ),
    ):
        error = await logbook_error(LogTools(core_client), device_id="dev1")

    assert error["code"] == "CONNECTION_FAILED"
    assert "Last error: no WS" in error["message"]
    rest.assert_not_awaited()


@_REFUSED_SOCKET_TIMER
@pytest.mark.usefixtures("logbook")
async def test_device_filter_with_a_rejected_token_is_an_auth_error(
    rejected_client: HomeAssistantClient,
) -> None:
    with rest_reads(rejected_client) as rest:
        error = await logbook_error(LogTools(rejected_client), device_id="dev1")

    assert error["code"] == "AUTH_INVALID_TOKEN"
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_device_query_timeout_is_not_reported_as_a_missing_websocket(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with rest_reads(core_client) as rest:
        async with unanswered(hass, CORE_COMMAND, monkeypatch):
            error = await logbook_error(LogTools(core_client), device_id="dev1")

    assert error["code"] == "TIMEOUT_OPERATION"
    rest.assert_not_awaited()


async def test_device_filter_when_core_has_no_logbook_is_not_an_empty_success(
    core_client: HomeAssistantClient,
) -> None:
    """Core answers ``unknown_command`` for ``logbook/get_events``."""
    with rest_reads(core_client) as rest:
        error = await logbook_error(LogTools(core_client), device_id="dev1")

    assert error["code"] == "CONNECTION_FAILED"
    assert "device_id" in error["message"]
    rest.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"), [("entity_id", ", "), ("device_id", " , ")]
)
async def test_blank_filter_is_rejected_before_any_read(
    core_client: HomeAssistantClient, sent: AsyncMock, field: str, value: str
) -> None:
    with rest_reads(core_client) as rest:
        error = await logbook_error(LogTools(core_client), **{field: value})

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert COMPONENT_COMMAND not in sent_types(sent)
    assert CORE_COMMAND not in sent_types(sent)
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_rest_read_restricts_to_the_listed_entities(
    core_client: HomeAssistantClient,
) -> None:
    """The positive twin of the blank-filter rejection: listed ids are read."""
    with rest_reads(core_client) as rest:
        result = await LogTools(core_client).get_logs(
            **call_kwargs(source="logbook", entity_id="light.kitchen, light.hall")
        )

    assert rest.await_args_list[-1].kwargs["entity_id"] == "light.kitchen,light.hall"
    assert set(by_entity(result)) == {"light.kitchen", "light.hall"}


@pytest.mark.usefixtures("logbook")
async def test_rest_read_of_a_malformed_entity_id_suggests_the_id_form(
    core_client: HomeAssistantClient,
) -> None:
    """Core's REST view fails on a malformed id with a 500: the hint names the
    input shape and the error log."""
    error = await logbook_error(LogTools(core_client), entity_id="kitchen_light")

    suggestions = error["suggestions"]
    assert any("domain.object_id" in s for s in suggestions)
    assert any("ha_get_logs(source='error_log')" in s for s in suggestions)


@pytest.mark.usefixtures("logbook")
async def test_rest_read_that_gets_no_answer_keeps_the_scope_hints(
    core_client: HomeAssistantClient,
) -> None:
    with patch.object(
        core_client,
        "get_logbook",
        AsyncMock(side_effect=HomeAssistantConnectionError("boom")),
    ):
        error = await logbook_error(LogTools(core_client))

    assert any("hours_back" in s for s in error["suggestions"])


@pytest.mark.parametrize("extra", [{"entity_id": "light.a"}, {"device_id": "dev1"}])
async def test_context_id_cannot_combine_with_other_filters(
    core_client: HomeAssistantClient, sent: AsyncMock, extra: dict[str, str]
) -> None:
    with rest_reads(core_client) as rest:
        error = await logbook_error(LogTools(core_client), context_id="ctx1", **extra)

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert CORE_COMMAND not in sent_types(sent)
    rest.assert_not_awaited()


@pytest.mark.usefixtures("logbook")
async def test_omitted_end_time_covers_the_requested_window_up_to_now(
    core_client: HomeAssistantClient,
) -> None:
    before = dt_util.utcnow()
    result = await LogTools(core_client).get_logs(
        **call_kwargs(source="logbook", hours_back=48)
    )

    start = datetime.fromisoformat(result["data"]["start_time"])
    end = datetime.fromisoformat(result["data"]["end_time"])
    assert end >= before
    assert end - start == timedelta(hours=48)


@pytest.mark.usefixtures("logbook")
async def test_explicit_end_time_honors_the_callers_window(
    core_client: HomeAssistantClient,
) -> None:
    """The rows are recorded now, so a window that ended earlier has none and
    one that ends later has them."""
    tools = LogTools(core_client)
    past = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    future = (dt_util.utcnow() + timedelta(hours=1)).isoformat()

    earlier = await tools.get_logs(
        **call_kwargs(source="logbook", hours_back=48, end_time=past)
    )
    later = await tools.get_logs(
        **call_kwargs(source="logbook", hours_back=48, end_time=future)
    )

    start = datetime.fromisoformat(earlier["data"]["start_time"])
    end = datetime.fromisoformat(earlier["data"]["end_time"])
    assert end - start == timedelta(hours=48)
    assert earlier["data"]["total_entries"] == 0
    assert "light.kitchen" in by_entity(later)


@pytest.mark.usefixtures("logbook")
async def test_omitted_and_explicit_end_time_return_the_same_rows(
    core_client: HomeAssistantClient,
) -> None:
    """Passing back the end the tool reported reads the same window (#2767)."""
    tools = LogTools(core_client)
    omitted = await tools.get_logs(**call_kwargs(source="logbook", hours_back=48))
    explicit = await tools.get_logs(
        **call_kwargs(
            source="logbook", hours_back=48, end_time=omitted["data"]["end_time"]
        )
    )

    assert explicit["data"]["start_time"] == omitted["data"]["start_time"]
    assert explicit["data"]["entries"] == omitted["data"]["entries"]
    assert omitted["data"]["total_entries"] >= 2


@pytest.mark.usefixtures("logbook")
async def test_pagination_hint_keeps_pages_in_the_same_window(
    core_client: HomeAssistantClient,
) -> None:
    result = await LogTools(core_client).get_logs(
        **call_kwargs(source="logbook", limit=1)
    )

    hint = result["data"]["pagination_hint"]
    assert f"end_time='{result['data']['end_time']}'" in hint
