"""``ha_get_state`` reads every id in one component frame, or falls back to REST.

With the component's ``states`` command, single and bulk reads resolve every id
from one ``ha_mcp_tools/states`` frame and never GET ``/api/states/<id>``.
Without it, or when the frame fails, each id is read over REST, silently: the
REST answer is the same state. States are Core's, set with
``hass.states.async_set``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import get_component_caps, invalidate_caps
from ha_mcp.tools.search import state as search_state
from ha_mcp.tools.tools_search import SearchTools

from ._command_faults import dropped_on, pooled_socket, received, unanswered
from ._search_tool import search_tools_on
from ._tool_errors import error_of

STATES = "ha_mcp_tools/states"


@pytest.fixture(autouse=True)
def _states(hass: HomeAssistant) -> None:
    hass.states.async_set("light.a", "on", {"friendly_name": "A"})
    hass.states.async_set("sensor.b", "21", {"friendly_name": "B"})


def _tools(
    client: HomeAssistantClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SearchTools:
    return search_tools_on(client, tmp_path, monkeypatch)


@contextmanager
def _rest_reads(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    with patch.object(client, "get_entity_state", wraps=client.get_entity_state) as spy:
        yield spy


async def test_a_bulk_read_is_one_component_frame_with_missing_ids_named(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tools(component_core_client, tmp_path, monkeypatch)

    with (
        received(hass, STATES) as seen,
        _rest_reads(component_core_client) as rest,
    ):
        resp = await tools.ha_get_state(
            entity_id=["light.a", "sensor.b", "light.ghost"]
        )

    data = resp["data"]
    assert set(data["states"]) == {"light.a", "sensor.b"}
    assert data["states"]["light.a"]["state"] == "on"
    assert [(e["entity_id"], e["error"]["code"]) for e in data["errors"]] == [
        ("light.ghost", "ENTITY_NOT_FOUND")
    ]
    assert any("ha_search()" in s for s in data["suggestions"])
    assert data["partial"] is True
    assert [msg["entity_ids"] for msg in seen[STATES]] == [
        ["light.a", "sensor.b", "light.ghost"]
    ]
    rest.assert_not_awaited()


async def test_a_single_read_is_the_same_component_frame(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tools(component_core_client, tmp_path, monkeypatch)

    with (
        received(hass, STATES) as seen,
        _rest_reads(component_core_client) as rest,
    ):
        resp = await tools.ha_get_state(entity_id="light.a")

    assert (resp["data"]["entity_id"], resp["data"]["state"]) == ("light.a", "on")
    assert len(seen[STATES]) == 1
    rest.assert_not_awaited()


async def test_a_single_id_the_component_reports_missing_is_not_found(
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same ENTITY_NOT_FOUND, with its ha_search hint, as a REST 404."""
    tools = _tools(component_core_client, tmp_path, monkeypatch)

    with _rest_reads(component_core_client) as rest, pytest.raises(ToolError) as exc:
        await tools.ha_get_state(entity_id="light.ghost")

    assert error_of(exc)["code"] == "ENTITY_NOT_FOUND"
    assert "ha_search" in str(exc.value)
    rest.assert_not_awaited()


async def test_more_than_100_ids_are_refused_before_any_read(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tools(component_core_client, tmp_path, monkeypatch)

    with (
        received(hass, STATES) as seen,
        _rest_reads(component_core_client) as rest,
        pytest.raises(ToolError, match="exceeds maximum of 100"),
    ):
        await tools.ha_get_state(entity_id=[f"light.l{i}" for i in range(101)])

    assert seen[STATES] == []
    rest.assert_not_awaited()


async def test_without_the_component_each_id_is_read_over_rest(
    core_client: HomeAssistantClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(core_client, tmp_path, monkeypatch)

    with _rest_reads(core_client) as rest:
        resp = await tools.ha_get_state(entity_id=["light.a", "sensor.b"])

    assert set(resp["data"]["states"]) == {"light.a", "sensor.b"}
    assert rest.await_count == 2
    assert "warnings" not in resp["data"]
    assert "warnings" not in resp


async def test_a_downgraded_component_falls_back_silently_and_is_dropped(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tools = _tools(component_core_client, tmp_path, monkeypatch)
    assert await get_component_caps(component_core_client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], STATES)

    with (
        patch.object(search_state, "invalidate_caps", wraps=invalidate_caps) as drop,
        _rest_reads(component_core_client) as rest,
    ):
        resp = await tools.ha_get_state(entity_id=["light.a"])

    assert resp["data"]["states"]["light.a"]["state"] == "on"
    assert rest.await_count == 1
    assert "warnings" not in resp["data"]
    assert "warnings" not in resp
    drop.assert_called_once_with(component_core_client)
    assert not any("states failed" in r.getMessage() for r in caplog.records)


async def test_a_frame_core_never_answers_falls_back_and_keeps_the_component(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A timeout is no downgrade: REST serves, the capabilities stay, the log
    says the frame failed."""
    tools = _tools(component_core_client, tmp_path, monkeypatch)

    with (
        patch.object(search_state, "invalidate_caps", wraps=invalidate_caps) as drop,
        _rest_reads(component_core_client) as rest,
    ):
        async with unanswered(hass, STATES, monkeypatch):
            resp = await tools.ha_get_state(entity_id=["light.a", "sensor.b"])

    assert set(resp["data"]["states"]) == {"light.a", "sensor.b"}
    assert rest.await_count == 2
    assert "warnings" not in resp
    drop.assert_not_called()
    assert any(
        r.levelno == logging.WARNING and "states failed" in r.getMessage()
        for r in caplog.records
    )


async def test_a_dropped_socket_falls_back_to_rest(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REST is a separate transport: an install whose WebSocket dropped still
    gets its states instead of a connection error."""
    tools = _tools(component_core_client, tmp_path, monkeypatch)
    assert await get_component_caps(component_core_client) is not None
    socket = await pooled_socket(component_core_client)

    with (
        dropped_on(hass, STATES, socket),
        _rest_reads(component_core_client) as rest,
    ):
        resp = await tools.ha_get_state(entity_id=["light.a", "sensor.b"])

    assert set(resp["data"]["states"]) == {"light.a", "sensor.b"}
    assert rest.await_count == 2
    assert "warnings" not in resp
    assert any(
        r.levelno == logging.WARNING and "connection error" in r.getMessage()
        for r in caplog.records
    )
