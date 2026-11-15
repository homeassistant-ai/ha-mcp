"""``ha_mcp_tools/call_service`` on a real Home Assistant.

The command fires one blocking service call and reports the real state
transition of its ``entity_ids``. Each test registers the service it calls on
the real ``hass.services``; the handler changes states through
``hass.states``, so the ``state_changed`` events the component waits on come
from Core's own bus.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError

from custom_components.ha_mcp_tools.websocket_api.constants import (
    CALL_SERVICE_MAX_TIMEOUT,
    WS_CALL_SERVICE,
)

from ._component_core import ws_call, ws_result
from ._switched_states import set_later

# Long enough for a handler's state change to arrive, short enough that a test
# expecting the wait to lapse stays fast.
_LAPSE = 0.2

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


def _register(
    hass: HomeAssistant,
    domain: str,
    service: str,
    effect: Callable[[ServiceCall], ServiceResponse] | None = None,
    *,
    supports_response: SupportsResponse = SupportsResponse.NONE,
) -> list[dict[str, Any]]:
    """Register a service; return the calls it receives.

    Each call records its data and how many ``state_changed`` listeners were
    on the bus while it ran.
    """
    calls: list[dict[str, Any]] = []

    async def _handler(call: ServiceCall) -> ServiceResponse:
        calls.append({"data": dict(call.data), "listeners": _state_listeners(hass)})
        return effect(call) if effect is not None else None

    hass.services.async_register(
        domain, service, _handler, supports_response=supports_response
    )
    return calls


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
        calls = _register(hass, "ha_mcp_tools", "get_caller_token")
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
        the service receives the caller's data exactly once."""
        hass.states.async_set("light.a", "off", {"brightness": 100})

        def _turn_on(call: ServiceCall) -> None:
            hass.states.async_set("light.a", "on", dict(call.data))

        calls = _register(hass, "light", "turn_on", _turn_on)
        before = _state_listeners(hass)

        res = await _call(
            component_ws,
            domain="light",
            service="turn_on",
            entity_ids=["light.a"],
            service_data={"brightness": 255},
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
        assert transition["attributes_changed"] == ["brightness"]
        assert [c["data"] for c in calls] == [{"brightness": 255}]
        # The confirmation listener was on the bus before the call ran, and is gone.
        assert calls[0]["listeners"] == before + 1
        assert _state_listeners(hass) == before

    async def test_attribute_only_change_names_just_that_attribute(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """With no expected state, the first change confirms; the transition
        lists only the attributes that moved, not the unchanged ones."""
        hass.states.async_set("climate.a", "heat", {"temperature": 19, "hvac": "x"})

        def _set_temperature(call: ServiceCall) -> None:
            hass.states.async_set("climate.a", "heat", {"temperature": 21, "hvac": "x"})

        _register(hass, "climate", "set_temperature", _set_temperature)

        res = await _call(
            component_ws,
            domain="climate",
            service="set_temperature",
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
        hass.states.async_set("light.a", "off")

        async def _slow_turn_on(call: ServiceCall) -> None:
            await asyncio.sleep(0)
            hass.states.async_set("light.a", "on")

        hass.services.async_register("light", "turn_on", _slow_turn_on)

        res = await _call(
            component_ws,
            domain="light",
            service="turn_on",
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
        hass.states.async_set("light.a", "off")
        calls = _register(hass, "light", "turn_on")
        before = _state_listeners(hass)

        res = await _call(
            component_ws,
            domain="light",
            service="turn_on",
            entity_ids=["light.a"],
            timeout=_LAPSE,
        )

        assert len(calls) == 1
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
        hass.states.async_set("light.a", "on")

        def _remove(call: ServiceCall) -> None:
            hass.states.async_remove("light.a")

        _register(hass, "light", "turn_off", _remove)

        res = await _call(
            component_ws,
            domain="light",
            service="turn_off",
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
        hass.states.async_set("light.a", "off")

        def _fail(call: ServiceCall) -> None:
            raise HomeAssistantError("dispatch failed")

        calls = _register(hass, "light", "turn_on", _fail)
        before = _state_listeners(hass)

        reply = await ws_call(
            component_ws,
            WS_CALL_SERVICE,
            domain="light",
            service="turn_on",
            entity_ids=["light.a"],
        )

        assert not reply["success"]
        assert "dispatch failed" in reply["error"]["message"]
        assert len(calls) == 1
        assert calls[0]["listeners"] == before + 1
        assert _state_listeners(hass) == before


class TestExpectedState:
    async def test_lock_reply_is_not_cut_short_at_locking(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A lock passes through ``locking``; with ``locked`` expected, the
        reply waits for ``locked`` and reports it, not the intermediate state."""
        hass.states.async_set("lock.front", "unlocked")
        later: list[asyncio.Task[None]] = []

        def _lock(call: ServiceCall) -> None:
            hass.states.async_set("lock.front", "locking")
            later.append(set_later(hass, "lock.front", "locked"))

        _register(hass, "lock", "lock", _lock)

        res = await _call(
            component_ws,
            domain="lock",
            service="lock",
            entity_ids=["lock.front"],
            expected_state="locked",
        )
        await asyncio.gather(*later)

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
        hass.states.async_set("media_player.a", "playing", {"media_position": 0})
        later: list[asyncio.Task[None]] = []

        def _pause(call: ServiceCall) -> None:
            hass.states.async_set("media_player.a", "playing", {"media_position": 5})
            later.append(set_later(hass, "media_player.a", "paused"))

        _register(hass, "media_player", "media_pause", _pause)

        res = await _call(
            component_ws,
            domain="media_player",
            service="media_pause",
            entity_ids=["media_player.a"],
            expected_state="paused",
        )
        await asyncio.gather(*later)

        assert res["confirmed"] is True
        assert res["transitions"][0]["new_state"]["state"] == "paused"

    async def test_turning_on_a_light_already_on_confirms_without_waiting(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """Core emits no event for a no-op, so a light already at the expected
        state must confirm at once instead of stalling to the timeout."""
        hass.states.async_set("light.a", "on", {"brightness": 255})
        _register(hass, "light", "turn_on")
        before = _state_listeners(hass)

        # A stall would run to the command's 30 s timeout; the test allows 5 s.
        async with asyncio.timeout(5):
            res = await _call(
                component_ws,
                domain="light",
                service="turn_on",
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
        response: ServiceResponse = {"changed_states": [{"entity_id": "light.a"}]}
        hass.states.async_set("light.a", "off")

        def _turn_on(call: ServiceCall) -> ServiceResponse:
            hass.states.async_set("light.a", "on")
            return response

        _register(
            hass,
            "light",
            "turn_on",
            _turn_on,
            supports_response=SupportsResponse.OPTIONAL,
        )

        res = await _call(
            component_ws,
            domain="light",
            service="turn_on",
            entity_ids=["light.a"],
            return_response=True,
        )

        assert res["service_response"] == response

    async def test_response_is_left_out_unless_requested(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A frame that does not ask for the response gets none, even from a
        service that has one."""
        _register(
            hass,
            "light",
            "turn_on",
            lambda call: {"x": 1},
            supports_response=SupportsResponse.OPTIONAL,
        )

        res = await _call(component_ws, domain="light", service="turn_on")

        assert res["dispatched"] is True
        assert "service_response" not in res


class TestFrame:
    async def test_service_without_entities_reports_dispatch_only(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A frame with only domain and service fires the call once with no
        data and reports it dispatched, with nothing to confirm."""
        calls = _register(hass, "automation", "trigger")

        res = await _call(component_ws, domain="automation", service="trigger")

        assert res["dispatched"] is True
        assert res["confirmed"] is False
        assert res["partial"] is False
        assert res["transitions"] == []
        assert [c["data"] for c in calls] == [{}]

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
        calls = _register(hass, "light", "turn_on")

        reply = await ws_call(component_ws, WS_CALL_SERVICE, **frame)

        assert not reply["success"]
        assert reply["error"]["code"] == "invalid_format"
        assert calls == []

    async def test_timeout_at_the_cap_is_accepted(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """The largest allowed timeout is a valid frame."""
        calls = _register(hass, "light", "turn_on")

        res = await _call(
            component_ws,
            domain="light",
            service="turn_on",
            timeout=CALL_SERVICE_MAX_TIMEOUT,
        )

        assert res["dispatched"] is True
        assert len(calls) == 1
