"""``ha_call_service`` through the component's ``call_service``, or Core's REST API.

A confirmable single call goes to the component as one frame that fires one
``async_call`` and returns the real transition. Everything else, and every
case where the frame provably never reached the component, is one REST POST
followed by the state-change wait. A frame that was sent but never answered is
reported partial and never sent again (at most once).

The services are handlers in Core's ``hass.services`` that switch entity
states as a device would (``_switched_states``); the states, events and
transitions are Core's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools import tools_service
from ha_mcp.tools.component_api import get_component_caps, invalidate_caps
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.tools_service import ServiceTools
from ha_mcp.tools.ws_waiters import wait_for_state_change

from ._command_faults import dropped_on, pooled_socket, received
from ._switched_states import SwitchedStates
from ._tool_errors import error_of

CALL_SERVICE = "ha_mcp_tools/call_service"
NOT_PROOF = "does NOT prove"
UNVERIFIED = "state change could not be verified"


@pytest.fixture(autouse=True)
def _short_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A target that never confirms gives up quickly; one that does confirms at once."""
    monkeypatch.setattr(tools_service, "CONFIRMATION_TIMEOUT_S", 0.5)


@pytest.fixture
def devices(hass: HomeAssistant) -> SwitchedStates:
    hass.states.async_set("light.a", "off")
    hass.states.async_set("light.b", "off")
    hass.states.async_set("number.a", "0")
    return SwitchedStates(hass)


def _tools(client: HomeAssistantClient) -> ServiceTools:
    return ServiceTools(client, DeviceControlTools(client))


@contextmanager
def _rest(client: HomeAssistantClient) -> Iterator[tuple[AsyncMock, AsyncMock]]:
    """Spies on the legacy REST POST and the REST state reads."""
    with (
        patch.object(client, "call_service", wraps=client.call_service) as post,
        patch.object(client, "get_entity_state", wraps=client.get_entity_state) as read,
    ):
        yield post, read


def _warnings(resp: dict[str, Any]) -> list[str]:
    return list(resp.get("warnings", []))


# --- the component serves a confirmable single call -------------------------------


@pytest.mark.usefixtures("devices")
async def test_a_confirmable_call_is_one_frame_with_the_real_transition(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    with (
        received(hass, CALL_SERVICE) as seen,
        _rest(component_core_client) as (
            post,
            read,
        ),
    ):
        resp = await _tools(component_core_client).ha_call_service(
            domain="light",
            service="turn_on",
            entity_id="light.a",
            data={"brightness": 255},
        )

    assert (resp["success"], resp["verified_state"]) == (True, "on")
    assert "partial" not in resp
    assert "service_response" not in resp
    (record,) = resp["result"]
    assert (record["entity_id"], record["state"]) == ("light.a", "on")
    assert record["attributes"]["brightness"] == 255
    post.assert_not_awaited()
    read.assert_not_awaited()
    (frame,) = seen[CALL_SERVICE]
    assert frame["service_data"] == {"brightness": 255, "entity_id": "light.a"}
    assert frame["entity_ids"] == ["light.a"]
    assert (frame["wait"], frame["return_response"]) == (True, False)
    assert frame["expected_state"] == "on"


@pytest.mark.usefixtures("devices")
async def test_a_service_with_no_known_end_state_confirms_on_any_change(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    with received(hass, CALL_SERVICE) as seen:
        resp = await _tools(component_core_client).ha_call_service(
            domain="number",
            service="set_value",
            entity_id="number.a",
            data={"value": 21},
        )

    assert resp["verified_state"] == "21"
    assert [frame["expected_state"] for frame in seen[CALL_SERVICE]] == [None]


@pytest.mark.usefixtures("devices")
async def test_a_call_that_confirms_nothing_returns_cores_changed_states(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A scene's own state never becomes "on", so the call is not confirmed and
    the component would answer it with no states at all; the REST POST
    returns what the call changed."""
    assert await async_setup_component(
        hass, "scene", {"scene": [{"name": "Movie", "entities": {"light.a": "on"}}]}
    )

    with (
        received(hass, CALL_SERVICE) as seen,
        _rest(component_core_client) as (
            post,
            _,
        ),
    ):
        resp = await _tools(component_core_client).ha_call_service(
            domain="scene", service="turn_on", entity_id="scene.movie"
        )

    assert resp["success"] is True
    assert "verified_state" not in resp
    assert "partial" not in resp
    assert "scene.movie" in [r["entity_id"] for r in resp["result"]]
    lamp = hass.states.get("light.a")
    assert lamp is not None and lamp.state == "on"
    post.assert_awaited_once()
    assert seen[CALL_SERVICE] == []


@pytest.mark.usefixtures("devices")
@pytest.mark.parametrize(
    ("entity_id", "verbose"),
    [("light.a", True), ("light.a,light.b", False)],
    ids=["verbose", "comma-separated-targets"],
)
async def test_calls_the_component_cannot_confirm_are_one_rest_post(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entity_id: str,
    verbose: bool,
) -> None:
    """Verbose wants every changed state; a comma list is no single entity the
    component could wait for, and Core's states endpoint 404s it."""
    with (
        received(hass, CALL_SERVICE) as seen,
        _rest(component_core_client) as (
            post,
            _,
        ),
    ):
        resp = await _tools(component_core_client).ha_call_service(
            domain="light", service="turn_on", entity_id=entity_id, verbose=verbose
        )

    assert resp["success"] is True
    post.assert_awaited_once()
    assert seen[CALL_SERVICE] == []


@pytest.mark.usefixtures("devices")
async def test_a_service_response_comes_back_through_the_frame(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    with (
        received(hass, CALL_SERVICE) as seen,
        _rest(component_core_client) as (
            post,
            _,
        ),
    ):
        resp = await _tools(component_core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a", return_response=True
        )

    assert resp["service_response"] == {"changed": ["light.a"]}
    assert not any(NOT_PROOF in w for w in _warnings(resp))
    assert [frame["return_response"] for frame in seen[CALL_SERVICE]] == [True]
    post.assert_not_awaited()


# --- what the component's transition says about the target --------------------------


@pytest.mark.usefixtures("devices")
async def test_a_target_core_does_not_have_is_not_found_at_once(
    component_core_client: HomeAssistantClient,
) -> None:
    with _rest(component_core_client) as (post, read), pytest.raises(ToolError) as exc:
        await _tools(component_core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.cuisine"
        )

    assert error_of(exc)["code"] == "ENTITY_NOT_FOUND"
    assert "light.cuisine" in str(exc.value)
    post.assert_not_awaited()
    read.assert_not_awaited()


async def test_a_target_that_stays_unavailable_is_unavailable_after_dispatch(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    devices: SwitchedStates,
) -> None:
    """The error says the call went out, so a caller does not repeat it."""
    hass.states.async_set("light.a", "unavailable")
    devices.behaviour["light.a"] = "stay"

    with _rest(component_core_client) as (post, _), pytest.raises(ToolError) as exc:
        await _tools(component_core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert error_of(exc)["code"] == "ENTITY_UNAVAILABLE"
    assert '"dispatched": true' in str(exc.value)
    post.assert_not_awaited()


async def test_a_target_removed_during_the_call_is_not_found_not_unavailable(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    devices: SwitchedStates,
) -> None:
    hass.states.async_set("light.a", "unavailable")
    devices.behaviour["light.a"] = "remove"

    with pytest.raises(ToolError) as exc:
        await _tools(component_core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert error_of(exc)["code"] == "ENTITY_NOT_FOUND"


@pytest.mark.usefixtures("devices")
async def test_the_broadcast_target_all_is_never_not_found(
    component_core_client: HomeAssistantClient,
) -> None:
    """``all`` is no entity in the state machine, but the broadcast may well
    have landed: an unverified partial, not ENTITY_NOT_FOUND."""
    resp = await _tools(component_core_client).ha_call_service(
        domain="light", service="turn_off", entity_id="all"
    )

    assert (resp["success"], resp["partial"]) == (True, True)
    assert any(UNVERIFIED in w for w in _warnings(resp))


async def test_a_dispatched_call_that_never_confirms_is_partial_and_not_sent_again(
    component_core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    devices.behaviour["light.a"] = "stay"

    with _rest(component_core_client) as (post, _):
        resp = await _tools(component_core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert (resp["success"], resp["partial"]) == (True, True)
    assert "verified_state" not in resp
    assert len(devices.calls) == 1
    post.assert_not_awaited()


# --- the frame never reached the component: one REST POST ------------------------------


@pytest.mark.usefixtures("devices")
async def test_without_the_component_the_call_is_one_rest_post(
    core_client: HomeAssistantClient,
) -> None:
    with _rest(core_client) as (post, _):
        resp = await _tools(core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert (resp["success"], resp["verified_state"]) == (True, "on")
    post.assert_awaited_once()


async def test_a_downgraded_component_is_dropped_and_the_call_posted_once(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    devices: SwitchedStates,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await get_component_caps(component_core_client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], CALL_SERVICE)

    with (
        patch.object(tools_service, "invalidate_caps", wraps=invalidate_caps) as drop,
        _rest(component_core_client) as (post, _),
    ):
        resp = await _tools(component_core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    post.assert_awaited_once()
    drop.assert_called_once_with(component_core_client)
    assert len(devices.calls) == 1


async def test_a_call_core_refuses_through_the_frame_is_posted_once(
    component_core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    """The service raised inside the component, before it changed anything:
    the REST POST is the call's first and only landing."""
    devices.behaviour["light.a"] = "fail_once"

    with _rest(component_core_client) as (post, _):
        resp = await _tools(component_core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert (resp["success"], resp["verified_state"]) == (True, "on")
    post.assert_awaited_once()
    assert len(devices.calls) == 2


async def test_a_socket_that_cannot_connect_sends_one_rest_post(
    component_core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    assert await get_component_caps(component_core_client) is not None
    await websocket_manager.disconnect()

    with (
        patch.object(
            HomeAssistantWebSocketClient,
            "connect",
            AsyncMock(side_effect=HomeAssistantConnectionError("refused")),
        ),
        _rest(component_core_client) as (post, _),
    ):
        resp = await _tools(component_core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    post.assert_awaited_once()
    assert len(devices.calls) == 1


async def test_a_frame_never_sent_falls_to_one_rest_post(
    component_core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    assert await get_component_caps(component_core_client) is not None
    socket = await pooled_socket(component_core_client)

    with (
        patch.object(
            socket,
            "send_command",
            side_effect=HomeAssistantCommandNotSent("WebSocket not authenticated"),
        ),
        _rest(component_core_client) as (post, _),
    ):
        resp = await _tools(component_core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    post.assert_awaited_once()
    assert len(devices.calls) == 1


async def test_a_socket_that_drops_after_the_send_is_partial_and_never_posted(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    devices: SwitchedStates,
) -> None:
    """The frame went out, so the call may have landed: re-posting it could apply
    it twice. Its answer is lost too, and the null response says so."""
    assert await get_component_caps(component_core_client) is not None
    socket = await pooled_socket(component_core_client)

    with (
        dropped_on(hass, CALL_SERVICE, socket),
        _rest(component_core_client) as (post, _),
    ):
        resp = await _tools(component_core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a", return_response=True
        )

    assert (resp["success"], resp["partial"]) == (True, True)
    assert resp["service_response"] is None
    assert any(NOT_PROOF in w for w in _warnings(resp))
    post.assert_not_awaited()


# --- the REST path's own checks around the POST ---------------------------------------


@pytest.mark.usefixtures("devices")
async def test_a_rest_404_is_not_found_before_anything_is_posted(
    core_client: HomeAssistantClient,
) -> None:
    """A target that cannot exist is not waited for."""
    with (
        _rest(core_client) as (post, _),
        patch.object(
            tools_service, "wait_for_state_change", wraps=wait_for_state_change
        ) as wait,
        pytest.raises(ToolError) as exc,
    ):
        await _tools(core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.cuisine"
        )

    assert error_of(exc)["code"] == "ENTITY_NOT_FOUND"
    assert "light.cuisine" in str(exc.value)
    post.assert_not_awaited()
    wait.assert_not_called()


async def test_an_unavailable_target_is_posted_and_unavailable_after_the_wait(
    hass: HomeAssistant, core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    """The call may be what reconnects the device, so it goes out; only a
    re-read after the wait that is still unavailable says so."""
    hass.states.async_set("light.a", "unavailable")
    devices.behaviour["light.a"] = "stay"

    with (
        _rest(core_client) as (post, _),
        patch.object(
            tools_service, "wait_for_state_change", wraps=wait_for_state_change
        ) as wait,
        pytest.raises(ToolError) as exc,
    ):
        await _tools(core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert error_of(exc)["code"] == "ENTITY_UNAVAILABLE"
    assert '"dispatched": true' in str(exc.value)
    post.assert_awaited_once()
    wait.assert_called_once()


@pytest.mark.usefixtures("devices")
async def test_a_rest_404_for_the_broadcast_target_all_is_still_posted(
    core_client: HomeAssistantClient,
) -> None:
    with _rest(core_client) as (post, _):
        resp = await _tools(core_client).ha_call_service(
            domain="light", service="turn_off", entity_id="all"
        )

    assert resp["success"] is True
    post.assert_awaited_once()


@pytest.mark.parametrize(
    ("behaviour", "code"),
    [("off", None), ("remove", "ENTITY_NOT_FOUND")],
    ids=["reconnected-to-another-state", "removed"],
)
async def test_an_unavailable_target_that_changed_is_not_reported_unavailable(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    devices: SwitchedStates,
    behaviour: str,
    code: str | None,
) -> None:
    """Reconnected to a state other than the one asked for: an unverified
    success. Removed meanwhile: not found."""
    hass.states.async_set("light.a", "unavailable")
    devices.behaviour["light.a"] = behaviour
    call = _tools(core_client).ha_call_service(
        domain="light", service="turn_on", entity_id="light.a"
    )

    if code is None:
        resp = await call
        assert resp["success"] is True
        assert any(UNVERIFIED in w for w in _warnings(resp))
    else:
        with pytest.raises(ToolError) as exc:
            await asyncio.gather(call)
        assert error_of(exc)["code"] == code


async def test_an_unanswered_re_read_after_the_wait_is_not_called_unavailable(
    hass: HomeAssistant, core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    """The re-read after the wait gets no answer: that proves nothing either way."""
    hass.states.async_set("light.a", "unavailable")
    devices.behaviour["light.a"] = "stay"

    with ExitStack() as stack:

        async def wait_then_lose_rest(*args: Any, **kwargs: Any) -> Any:
            result = await wait_for_state_change(*args, **kwargs)
            stack.enter_context(
                patch.object(
                    core_client,
                    "get_entity_state",
                    AsyncMock(side_effect=HomeAssistantConnectionError("network blip")),
                )
            )
            return result

        stack.enter_context(
            patch.object(tools_service, "wait_for_state_change", wait_then_lose_rest)
        )
        resp = await _tools(core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert any(UNVERIFIED in w for w in _warnings(resp))
    assert not any("unavailable" in w.lower() for w in _warnings(resp))


async def test_an_unanswered_read_before_the_post_still_posts(
    core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    with (
        patch.object(
            core_client,
            "get_entity_state",
            AsyncMock(side_effect=HomeAssistantConnectionError("network blip")),
        ),
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as post,
    ):
        resp = await _tools(core_client).ha_call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    post.assert_awaited_once()
    assert len(devices.calls) == 1
