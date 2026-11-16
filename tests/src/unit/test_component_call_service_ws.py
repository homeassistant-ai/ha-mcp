"""``ha_mcp_tools/call_service`` on a real Home Assistant.

The command fires one blocking service call and reports the real state
transition of its ``entity_ids``. Lights, the lock, the thermostat and the
media player are devices below Core's own integrations (``_switched_states``),
a response comes from a Core script and ``automation.trigger`` is Core's
automation integration, so the services and the ``state_changed`` events the
component waits on are Core's own.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_capture_events

from custom_components.ha_mcp_tools.websocket_api.constants import (
    CALL_SERVICE_MAX_TIMEOUT,
    WS_CALL_SERVICE,
)

from ._component_core import ws_call, ws_result
from ._switched_states import (
    RANG,
    Devices,
    Received,
    listeners_at_call,
    recording_service,
    set_up_devices,
    set_up_ringing_automation,
)

# Long enough for a handler's state change to arrive, short enough that a test
# expecting the wait to lapse stays fast.
_LAPSE = 0.2

# The target Core's light service needs, as the server sends it beside the
# confirmation's ``entity_ids``.
_LAMP = {"entity_id": "light.a"}

# Case, whitespace and NBSP variants of the reserved domain. Core resolves the
# case variants to the registered ``ha_mcp_tools`` service; the padded ones it
# would answer as not found, so the refusal must come from the component.
_HA_MCP_TOOLS_DOMAIN_VARIANTS = [
    "ha_mcp_tools",
    "HA_MCP_TOOLS",
    " ha_mcp_tools ",
    "ha_mcp_tools\n",
    "\tha_mcp_tools",
    " ha_mcp_tools ",
]


def _state_listeners(hass: HomeAssistant) -> int:
    return hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)


async def _lamp(hass: HomeAssistant, state: str = "off") -> Devices:
    """``light.a``, a device below Core's ``light`` integration."""
    return await set_up_devices(hass, {"light.a": state})


async def _answering_script(hass: HomeAssistant, reply: dict[str, Any]) -> None:
    """``script.answer``: a Core script that stops with ``reply`` as its response."""
    assert await async_setup_component(
        hass,
        "script",
        {
            "script": {
                "answer": {
                    "sequence": [
                        {"variables": {"reply": reply}},
                        {"stop": "answered", "response_variable": "reply"},
                    ]
                }
            }
        },
    )


async def _call(client: Any, **params: Any) -> dict[str, Any]:
    result: dict[str, Any] = await ws_result(client, WS_CALL_SERVICE, **params)
    return result


class TestRefusedBeforeDispatch:
    @pytest.mark.parametrize("domain", _HA_MCP_TOOLS_DOMAIN_VARIANTS)
    async def test_reserved_domain_cannot_be_called_in_any_spelling(
        self, hass: HomeAssistant, component_ws: Any, domain: str
    ) -> None:
        """A caller must never reach the component's own admin services
        (``get_caller_token``, then every file/YAML service) through this
        command, whatever the domain's case or padding."""
        calls = recording_service(hass, "ha_mcp_tools", "get_caller_token")
        before = _state_listeners(hass)

        reply = await ws_call(
            component_ws, WS_CALL_SERVICE, domain=domain, service="get_caller_token"
        )

        assert not reply["success"]
        # Refused as the reserved domain, not answered as an unknown service.
        assert reply["error"].get("translation_key") != "service_not_found"
        assert calls == []
        assert _state_listeners(hass) == before

    async def test_unknown_service_is_a_clean_not_found(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """An unknown service is reported as not found and leaves nothing
        listening on the bus."""
        hass.states.async_set("light.a", "off")
        before = _state_listeners(hass)

        reply = await ws_call(
            component_ws,
            WS_CALL_SERVICE,
            domain="light",
            service="does_not_exist",
            entity_ids=["light.a"],
        )

        assert not reply["success"]
        assert reply["error"]["translation_key"] == "service_not_found"
        assert _state_listeners(hass) == before


class TestConfirmedTransition:
    async def test_reply_reports_the_real_transition(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """The reply carries the state change the call caused, confirmed, and
        the device receives the caller's data exactly once."""
        devices = await _lamp(hass)
        listeners = listeners_at_call(hass, devices)
        before = _state_listeners(hass)

        res = await _call(
            component_ws,
            domain="light",
            service="turn_on",
            entity_ids=["light.a"],
            service_data={"entity_id": "light.a", "brightness": 255},
        )

        assert res["dispatched"] is True
        assert res["confirmed"] is True
        assert res["partial"] is False
        assert "service_response" not in res
        (transition,) = res["transitions"]
        assert transition["entity_id"] == "light.a"
        assert transition["old_state"]["state"] == "off"
        assert transition["new_state"]["state"] == "on"
        assert transition["changed"] is True
        # A light reports its brightness and color mode only while on.
        assert transition["attributes_changed"] == ["brightness", "color_mode"]
        assert devices.calls == [Received("light.a", "turn_on", {"brightness": 255})]
        # The confirmation listener was on the bus before the call ran, and is gone.
        assert listeners == [before + 1]
        assert _state_listeners(hass) == before

    async def test_attribute_only_change_names_just_that_attribute(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """With no expected state, the first change confirms; the transition
        lists only the attributes that moved, not the unchanged ones."""
        await set_up_devices(hass, {"climate.a": "heat"})

        res = await _call(
            component_ws,
            domain="climate",
            service="set_temperature",
            service_data={"entity_id": "climate.a", "temperature": 21},
            entity_ids=["climate.a"],
            expected_state=None,
        )

        assert res["confirmed"] is True
        (transition,) = res["transitions"]
        assert transition["changed"] is False
        assert transition["attributes_changed"] == ["temperature"]

    async def test_reply_shows_the_state_the_service_left_behind(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """Without waiting for events, the reply still reflects the finished
        call: the dispatch blocks until the service handler returns."""
        await _lamp(hass)

        res = await _call(
            component_ws,
            domain="light",
            service="turn_on",
            service_data=_LAMP,
            entity_ids=["light.a"],
            wait=False,
        )

        (transition,) = res["transitions"]
        assert transition["new_state"]["state"] == "on"


class TestUnconfirmedWrite:
    async def test_lapsed_confirmation_is_partial_not_a_failure(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A call that landed but whose entity never reported is ``partial``,
        never an error the server would retry (a double write)."""
        devices = await _lamp(hass)
        devices.behaviour["light.a"] = "stay"
        before = _state_listeners(hass)

        res = await _call(
            component_ws,
            domain="light",
            service="turn_on",
            service_data=_LAMP,
            entity_ids=["light.a"],
            timeout=_LAPSE,
        )

        assert len(devices.calls) == 1
        assert res["dispatched"] is True
        assert res["confirmed"] is False
        assert res["partial"] is True
        (transition,) = res["transitions"]
        assert transition["new_state"]["state"] == "off"
        assert transition["changed"] is False
        assert _state_listeners(hass) == before

    async def test_entity_removed_during_the_call_is_not_confirmed(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """An entity that disappears mid-call did not reach a new state: the
        write must not be reported as confirmed."""
        devices = await _lamp(hass, "on")
        devices.behaviour["light.a"] = "remove"

        res = await _call(
            component_ws,
            domain="light",
            service="turn_off",
            service_data=_LAMP,
            entity_ids=["light.a"],
            timeout=_LAPSE,
        )

        assert res["dispatched"] is True
        assert res["confirmed"] is False
        assert res["partial"] is True

    async def test_service_error_reaches_the_caller_and_frees_the_listener(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A service that raises answers the caller with its error, and the
        confirmation listener registered for it does not stay on the bus."""
        devices = await _lamp(hass)
        devices.behaviour["light.a"] = "fail_once"
        listeners = listeners_at_call(hass, devices)
        before = _state_listeners(hass)

        reply = await ws_call(
            component_ws,
            WS_CALL_SERVICE,
            domain="light",
            service="turn_on",
            service_data=_LAMP,
            entity_ids=["light.a"],
        )

        assert not reply["success"]
        assert "light.a did not answer" in reply["error"]["message"]
        assert listeners == [before + 1]
        assert _state_listeners(hass) == before


class TestExpectedState:
    async def test_lock_reply_is_not_cut_short_at_locking(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A lock passes through ``locking``; with ``locked`` expected, the
        reply waits for ``locked`` and reports it, not the intermediate state."""
        await set_up_devices(hass, {"lock.front": "unlocked"})

        res = await _call(
            component_ws,
            domain="lock",
            service="lock",
            service_data={"entity_id": "lock.front"},
            entity_ids=["lock.front"],
            expected_state="locked",
        )
        await hass.async_block_till_done()

        assert res["confirmed"] is True
        assert res["partial"] is False
        (transition,) = res["transitions"]
        assert transition["old_state"]["state"] == "unlocked"
        assert transition["new_state"]["state"] == "locked"
        assert transition["changed"] is True

    async def test_attribute_tick_does_not_confirm_a_pause(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A position update while still ``playing`` is noise: with ``paused``
        expected, the reply reports the later real ``paused``."""
        await set_up_devices(hass, {"media_player.a": "playing"})

        res = await _call(
            component_ws,
            domain="media_player",
            service="media_pause",
            service_data={"entity_id": "media_player.a"},
            entity_ids=["media_player.a"],
            expected_state="paused",
        )
        await hass.async_block_till_done()

        assert res["confirmed"] is True
        assert res["transitions"][0]["new_state"]["state"] == "paused"

    async def test_turning_on_a_light_already_on_confirms_without_waiting(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """Core emits no event for a no-op, so a light already at the expected
        state must confirm at once instead of stalling to the timeout."""
        await _lamp(hass, "on")
        before = _state_listeners(hass)

        # A stall would run to the command's 30 s timeout; the test allows 5 s.
        async with asyncio.timeout(5):
            res = await _call(
                component_ws,
                domain="light",
                service="turn_on",
                service_data=_LAMP,
                entity_ids=["light.a"],
                expected_state="on",
                timeout=30,
            )

        assert res["dispatched"] is True
        assert res["confirmed"] is True
        assert res["partial"] is False
        (transition,) = res["transitions"]
        assert transition["new_state"]["state"] == "on"
        assert transition["changed"] is False
        assert _state_listeners(hass) == before


class TestServiceResponse:
    async def test_requested_response_is_returned(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A caller that asks for the service's response gets it."""
        reply = {"answer": 42}
        await _answering_script(hass, reply)

        res = await _call(
            component_ws, domain="script", service="answer", return_response=True
        )

        assert res["service_response"] == reply

    async def test_response_is_left_out_unless_requested(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A frame that does not ask for the response gets none, even from a
        service that has one."""
        await _answering_script(hass, {"answer": 42})

        res = await _call(component_ws, domain="script", service="answer")

        assert res["dispatched"] is True
        assert "service_response" not in res


class TestFrame:
    async def test_service_without_entities_reports_dispatch_only(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A frame with no ``entity_ids`` fires the call once and reports it
        dispatched, with nothing to confirm."""
        automation = await set_up_ringing_automation(hass)
        rang = async_capture_events(hass, RANG)

        res = await _call(
            component_ws,
            domain="automation",
            service="trigger",
            service_data={"entity_id": automation},
        )
        await hass.async_block_till_done()

        assert res["dispatched"] is True
        assert res["confirmed"] is False
        assert res["partial"] is False
        assert res["transitions"] == []
        assert len(rang) == 1

    @pytest.mark.parametrize(
        "frame",
        [
            {"service": "turn_on"},
            {"domain": "light"},
            {
                "domain": "light",
                "service": "turn_on",
                "timeout": CALL_SERVICE_MAX_TIMEOUT + 1,
            },
            {"domain": "light", "service": "turn_on", "entity_ids": "light.a"},
        ],
        ids=["no-domain", "no-service", "timeout-over-cap", "entity-ids-not-list"],
    )
    async def test_malformed_frame_is_refused_without_a_call(
        self, hass: HomeAssistant, component_ws: Any, frame: dict[str, Any]
    ) -> None:
        """A malformed frame is answered ``invalid_format`` and fires nothing."""
        devices = await _lamp(hass)

        reply = await ws_call(
            component_ws,
            WS_CALL_SERVICE,
            **frame,
            service_data=_LAMP,
        )

        assert not reply["success"]
        assert reply["error"]["code"] == "invalid_format"
        assert devices.calls == []

    async def test_timeout_at_the_cap_is_accepted(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """The largest allowed timeout is a valid frame."""
        devices = await _lamp(hass)

        res = await _call(
            component_ws,
            domain="light",
            service="turn_on",
            service_data=_LAMP,
            timeout=CALL_SERVICE_MAX_TIMEOUT,
        )

        assert res["dispatched"] is True
        assert devices.targets() == ["light.a"]
