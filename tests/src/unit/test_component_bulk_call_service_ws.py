"""``ha_mcp_tools/bulk_call_service`` on a real Home Assistant.

One frame fires a batch of service calls and reports each one's real state
transition. Lights, the switch and the lock are devices below Core's own
integrations (``_switched_states``) and ``automation.trigger`` is Core's
automation integration; only the ``probe.*`` services that show the dispatch
order are the test's own. Either way the ``state_changed`` events the
component waits on come from Core's own bus.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant.common import async_capture_events

from custom_components.ha_mcp_tools.websocket_api.constants import (
    CALL_SERVICE_MAX_TIMEOUT,
    WS_BULK_CALL_SERVICE,
)

from ._component_core import ws_call, ws_result
from ._switched_states import (
    RANG,
    Received,
    listeners_at_call,
    recording_service,
    set_up_devices,
    set_up_ringing_automation,
)

# Long enough for a handler's state change to arrive, short enough that a test
# expecting the shared wait to lapse stays fast.
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


def _turn_on(entity_id: str, service: str = "turn_on", **extra: Any) -> dict[str, Any]:
    """An op as the server sends it: Core's target in ``service_data``, the
    confirmation target in ``entity_ids``."""
    return {
        "domain": entity_id.split(".", 1)[0],
        "service": service,
        "service_data": {"entity_id": entity_id},
        "entity_ids": [entity_id],
        **extra,
    }


def _overlap_probe(hass: HomeAssistant) -> list[str]:
    """Register ``probe.first`` and ``probe.second``; return what they saw.

    ``first`` waits briefly for ``second`` to start, so the log shows whether
    the two calls ran at the same time. Each moves its own light to ``on``.
    """
    log: list[str] = []
    second_started = asyncio.Event()

    async def _first(call: ServiceCall) -> None:
        log.append("first started")
        try:
            await asyncio.wait_for(second_started.wait(), _LAPSE)
        except TimeoutError:
            log.append("first finished alone")
        else:
            log.append("first overlapped second")
        hass.states.async_set("light.first", "on")

    async def _second(call: ServiceCall) -> None:
        log.append("second started")
        second_started.set()
        hass.states.async_set("light.second", "on")

    hass.services.async_register("probe", "first", _first)
    hass.services.async_register("probe", "second", _second)
    return log


async def _bulk(client: Any, **params: Any) -> dict[str, Any]:
    result: dict[str, Any] = await ws_result(client, WS_BULK_CALL_SERVICE, **params)
    return result


class TestRefusedBeforeDispatch:
    @pytest.mark.parametrize("bad_domain", _HA_MCP_TOOLS_DOMAIN_VARIANTS)
    async def test_one_reserved_domain_op_stops_the_whole_batch(
        self, hass: HomeAssistant, component_ws: Any, bad_domain: str
    ) -> None:
        """A batch can never smuggle a call to the component's own admin
        services: one such op, in any spelling, refuses the frame before even
        the valid op ahead of it runs."""
        devices = await set_up_devices(hass, {"light.a": "off"})
        token_calls = recording_service(hass, "ha_mcp_tools", "get_caller_token")
        before = _state_listeners(hass)

        reply = await ws_call(
            component_ws,
            WS_BULK_CALL_SERVICE,
            operations=[
                _turn_on("light.a"),
                {"domain": bad_domain, "service": "get_caller_token"},
            ],
        )

        assert not reply["success"]
        # Refused as the reserved domain, not answered as an unknown service.
        assert reply["error"].get("translation_key") != "service_not_found"
        assert devices.calls == []
        assert token_calls == []
        assert _state_listeners(hass) == before

    async def test_one_unknown_service_stops_the_whole_batch(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """An unknown service anywhere in the batch is reported as not found
        and nothing in the batch runs."""
        devices = await set_up_devices(hass, {"light.a": "off"})
        before = _state_listeners(hass)

        reply = await ws_call(
            component_ws,
            WS_BULK_CALL_SERVICE,
            operations=[
                _turn_on("light.a"),
                {"domain": "light", "service": "does_not_exist"},
            ],
        )

        assert not reply["success"]
        assert reply["error"]["translation_key"] == "service_not_found"
        assert devices.calls == []
        assert _state_listeners(hass) == before


class TestConfirmedBatch:
    async def test_each_op_reports_its_real_transition_in_order(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """Every op is confirmed with its own transition, in the order the
        caller sent them, and each device gets its own data once."""
        devices = await set_up_devices(hass, {"light.a": "off", "switch.b": "off"})
        before = _state_listeners(hass)

        res = await _bulk(
            component_ws,
            operations=[
                _turn_on(
                    "light.a",
                    service_data={"entity_id": "light.a", "brightness": 255},
                ),
                _turn_on("switch.b"),
            ],
            parallel=True,
        )

        assert res["total"] == 2
        assert res["dispatched"] == 2
        assert res["failed"] == 0
        light_op, switch_op = res["operations"]
        assert light_op["domain"] == "light"
        assert switch_op["domain"] == "switch"
        for op in (light_op, switch_op):
            assert op["dispatched"] is True
            assert op["confirmed"] is True
            assert op["partial"] is False
            assert "error" not in op
        (transition,) = light_op["transitions"]
        assert transition["old_state"]["state"] == "off"
        assert transition["new_state"]["state"] == "on"
        assert transition["changed"] is True
        # A light reports its brightness and color mode only while on.
        assert transition["attributes_changed"] == ["brightness", "color_mode"]
        # Parallel ops reach their devices in either order.
        assert sorted(devices.calls, key=lambda call: call.entity_id) == [
            Received("light.a", "turn_on", {"brightness": 255}),
            Received("switch.b", "turn_on", {}),
        ]
        assert _state_listeners(hass) == before

    async def test_each_op_confirms_on_its_own_expected_state(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A lock op waits past ``locking`` for its expected ``locked`` while a
        light op in the same batch confirms on its own ``on``."""
        await set_up_devices(hass, {"lock.front": "unlocked", "light.a": "off"})

        res = await _bulk(
            component_ws,
            operations=[
                _turn_on("lock.front", "lock", expected_state="locked"),
                _turn_on("light.a", expected_state="on"),
            ],
            parallel=True,
        )
        await hass.async_block_till_done()

        lock_op, light_op = res["operations"]
        assert lock_op["confirmed"] is True
        assert lock_op["transitions"][0]["new_state"]["state"] == "locked"
        assert light_op["confirmed"] is True
        assert light_op["transitions"][0]["new_state"]["state"] == "on"
        assert res["failed"] == 0

    async def test_batch_of_no_ops_confirms_without_waiting(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """Core emits no event for a no-op, so a light already at its expected
        state confirms at once instead of stalling the batch to the timeout."""
        await set_up_devices(hass, {"light.a": "on"})

        # A stall would run to the batch's 30 s timeout; the test allows 5 s.
        async with asyncio.timeout(5):
            res = await _bulk(
                component_ws,
                operations=[_turn_on("light.a", expected_state="on")],
                timeout=30,
            )

        (op,) = res["operations"]
        assert op["dispatched"] is True
        assert op["confirmed"] is True
        assert op["partial"] is False


class TestDispatchOrder:
    async def test_sequential_batch_runs_one_call_at_a_time(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """With ``parallel`` off, each call finishes before the next starts,
        in the caller's order, and both still confirm."""
        hass.states.async_set("light.first", "off")
        hass.states.async_set("light.second", "off")
        log = _overlap_probe(hass)

        res = await _bulk(
            component_ws,
            operations=[
                {"domain": "probe", "service": "first", "entity_ids": ["light.first"]},
                {
                    "domain": "probe",
                    "service": "second",
                    "entity_ids": ["light.second"],
                },
            ],
            parallel=False,
        )

        assert log == ["first started", "first finished alone", "second started"]
        assert res["dispatched"] == 2
        assert all(op["confirmed"] is True for op in res["operations"])

    async def test_batch_runs_calls_together_by_default(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A frame with bare ops and no options runs its calls concurrently,
        with no data and nothing to confirm."""
        log = _overlap_probe(hass)

        res = await _bulk(
            component_ws,
            operations=[
                {"domain": "probe", "service": "first"},
                {"domain": "probe", "service": "second"},
            ],
        )

        assert "first overlapped second" in log
        for op in res["operations"]:
            assert op["dispatched"] is True
            assert op["partial"] is False
            assert op["transitions"] == []

    async def test_one_failing_op_leaves_the_others_untouched(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """A service that raises marks only its own op failed (not dispatched,
        neither confirmed nor partial); the other op still lands and confirms."""
        devices = await set_up_devices(hass, {"light.a": "off", "switch.b": "off"})
        devices.behaviour["light.a"] = "fail_once"
        before = _state_listeners(hass)

        res = await _bulk(
            component_ws,
            operations=[
                _turn_on("light.a"),
                _turn_on("switch.b"),
            ],
            parallel=True,
        )

        assert res["total"] == 2
        assert res["dispatched"] == 1
        assert res["failed"] == 1
        failed, ok = res["operations"]
        assert failed["domain"] == "light"
        assert failed["dispatched"] is False
        assert failed["confirmed"] is False
        assert failed["partial"] is False
        assert "light.a did not answer" in failed["error"]
        assert ok["domain"] == "switch"
        assert ok["dispatched"] is True
        assert ok["confirmed"] is True
        assert "error" not in ok
        assert _state_listeners(hass) == before


class TestUnconfirmedBatch:
    async def test_lapsed_shared_wait_is_partial_not_a_failure(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """Ops that landed but never reported are ``partial`` once the shared
        wait lapses, never failures the server would retry."""
        devices = await set_up_devices(
            hass, {"light.a": "off", "lock.front": "unlocked"}
        )
        devices.behaviour["light.a"] = "stay"
        devices.behaviour["lock.front"] = "stay"
        before = _state_listeners(hass)

        res = await _bulk(
            component_ws,
            operations=[_turn_on("light.a"), _turn_on("lock.front", "lock")],
            timeout=_LAPSE,
        )

        assert res["dispatched"] == 2
        assert res["failed"] == 0
        for op in res["operations"]:
            assert op["dispatched"] is True
            assert op["confirmed"] is False
            assert op["partial"] is True
        assert _state_listeners(hass) == before

    async def test_only_entity_ops_listen_and_every_listener_is_freed(
        self, hass: HomeAssistant, component_ws: Any
    ) -> None:
        """An op with entities listens for its confirmation from before the
        calls start; an op without entities adds no listener and is not
        ``partial``; nothing stays on the bus afterwards."""
        devices = await set_up_devices(hass, {"light.a": "off"})
        listeners = listeners_at_call(hass, devices)
        automation = await set_up_ringing_automation(hass)
        rang = async_capture_events(hass, RANG)
        before = _state_listeners(hass)

        res = await _bulk(
            component_ws,
            operations=[
                _turn_on("light.a", expected_state="on"),
                {
                    "domain": "automation",
                    "service": "trigger",
                    "service_data": {"entity_id": automation},
                    "entity_ids": [],
                },
            ],
        )
        await hass.async_block_till_done()

        assert len(rang) == 1

        assert listeners == [before + 1]
        assert _state_listeners(hass) == before
        light_op, automation_op = res["operations"]
        assert light_op["confirmed"] is True
        assert automation_op["dispatched"] is True
        assert automation_op["confirmed"] is False
        assert automation_op["partial"] is False
        assert automation_op["transitions"] == []


class TestFrame:
    @pytest.mark.parametrize(
        "frame",
        [
            {},
            {"operations": []},
            {
                "operations": [_turn_on("light.a")],
                "timeout": CALL_SERVICE_MAX_TIMEOUT + 1,
            },
            {"operations": [{"domain": "light"}]},
        ],
        ids=["no-operations", "empty-operations", "timeout-over-cap", "op-no-service"],
    )
    async def test_malformed_frame_is_refused_without_a_call(
        self, hass: HomeAssistant, component_ws: Any, frame: dict[str, Any]
    ) -> None:
        """A malformed frame is answered ``invalid_format`` and fires nothing."""
        devices = await set_up_devices(hass, {"light.a": "off"})

        reply = await ws_call(component_ws, WS_BULK_CALL_SERVICE, **frame)

        assert not reply["success"]
        assert reply["error"]["code"] == "invalid_format"
        assert devices.calls == []
