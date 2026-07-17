"""Routing tests for the entity_lookup / reference_data component consumers.

Three set/remove/post-write consumers pay for a whole-collection fetch to answer
a single question, and route through the ``ha_mcp_tools`` component when it
advertises the capability:

- ``ConfigSceneTools._resolve_scene_entity_id`` (unique_id -> entity_id) dumps the
  ENTIRE entity registry with a 0.2 s sleep + retry; the component path is one
  ``entity_lookup`` frame on a hit, and ONE recheck frame after a single sleep on
  an empty result (post-upsert registration lag still applies to an in-process
  read). An AUTHORITATIVE empty recheck falls to the naive fallback; a recheck
  that itself returns ``None`` (component went unavailable mid-retry) drops to the
  legacy registry list+retry instead of trusting the empty and guessing.
- ``AutomationConfigTools._resolve_automation_entity_id`` (config id -> entity_id)
  scans the whole ``get_states()`` machine; the component path is one
  ``entity_lookup`` frame.
- ``validate_config_references`` fetches ``get_services()`` + ``get_states()``; the
  component path is one ``reference_data`` frame.

These pin, per consumer, the standard component-routing taxonomy: component
preferred, capability-miss -> legacy, ``unknown_command`` -> invalidate caps +
legacy, non-unknown command error -> legacy WITHOUT invalidating. Transport
failure diverges by legacy path: a ``HomeAssistantConnectionError`` PROPAGATES for
the resolvers (their legacy list/state scan rides the same pooled WS), but the
validator's legacy path is REST, so it falls back to the REST
``get_services()``/``get_states()`` fetch on BOTH a ``HomeAssistantConnectionError``
off the frame AND a plain ``Exception`` from ``get_websocket_client()`` failing to
establish the socket. Plus the GET-path invariant: with ``allow_component`` unset
(the config-get default) the resolvers never touch the component even with the
capability advertised, and a component HIT takes NO ``asyncio.sleep`` (only an
empty scene result pays the one lag-absorbing recheck sleep).

The WS client is the shared ``make_ws`` dispatcher; the HA client is a spy that
tallies every legacy fetch so "the legacy dump never ran" is a real assertion.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, component_config_reads
from ha_mcp.tools.reference_validator import validate_config_references
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

_CAPS_ENTITY_LOOKUP = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["entity_lookup"],
    "limits": {},
}
_CAPS_REFERENCE_DATA = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["reference_data"],
    "limits": {},
}
# A component that answers ``info`` but advertises NEITHER capability (only the
# unrelated ``search``) — the caps-miss branch.
_CAPS_OTHER = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["search"],
    "limits": {},
}


class RoutingClient:
    """Credentialed HA client spy: tallies every legacy fetch the consumers make."""

    def __init__(
        self,
        *,
        registry_list: list[dict[str, Any]] | None = None,
        states: list[dict[str, Any]] | None = None,
        services: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self._registry_list = list(registry_list or [])
        self.ws_types: Counter[str] = Counter()
        self.get_states = AsyncMock(return_value=list(states or []))
        self.get_services = AsyncMock(return_value=list(services or []))

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type", "")
        self.ws_types[msg_type] += 1
        if msg_type == "config/entity_registry/list":
            return {"success": True, "result": list(self._registry_list)}
        return {"success": False}


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _lookup_calls(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/entity_lookup"
    ]


def _lookup_seq_ws(match_seq: list[list[dict[str, Any]]]) -> AsyncMock:
    """WS serving the caps ``info`` probe + a scripted ``entity_lookup`` sequence.

    The i-th ``entity_lookup`` frame returns ``{"matches": match_seq[i]}`` (the
    last element repeats if more frames arrive), so a lag scenario — first frame
    empty, recheck frame populated — can be pinned. ``make_ws`` serves a single
    fixed result and cannot express that.
    """
    ws = AsyncMock()
    idx = {"n": 0}

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": _CAPS_ENTITY_LOOKUP}
        if command_type == "ha_mcp_tools/entity_lookup":
            i = min(idx["n"], len(match_seq) - 1)
            idx["n"] += 1
            return {"success": True, "result": {"matches": match_seq[i]}}
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


# =============================================================================
# Scene resolver — _resolve_scene_entity_id
# =============================================================================
class TestSceneResolverRouting:
    @pytest.mark.asyncio
    async def test_component_preferred_no_dump_no_sleep(self, monkeypatch) -> None:
        """entity_lookup serves the resolve in one frame; the legacy registry dump
        never runs and NO ``asyncio.sleep`` is taken (in-process read is
        authoritative on return)."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_result={
                "matches": [
                    {"entity_id": "scene.led_desk_strip_night_light"},
                ]
            },
        )
        # A registry dump that WOULD resolve differently, to prove it is not read.
        client = RoutingClient(registry_list=[])
        with patch_ws(ws, component_config_reads):
            entity_id = await ConfigSceneTools(client)._resolve_scene_entity_id(
                "night_light_led_desk_strip", allow_component=True
            )
        assert entity_id == "scene.led_desk_strip_night_light"
        assert client.ws_types["config/entity_registry/list"] == 0
        assert len(_lookup_calls(ws)) == 1
        assert _lookup_calls(ws)[0].kwargs == {
            "unique_id": "night_light_led_desk_strip",
            "domain": "scene",
        }
        assert sleep_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_component_empty_then_populated_rechecks_once(
        self, monkeypatch
    ) -> None:
        """Post-upsert registration lag (#1168): the first entity_lookup is empty,
        the recheck after ONE ``_RESOLVE_RETRY_DELAY`` sleep sees the freshly
        registered scene → the real entity_id, not the naive fallback. Two frames,
        exactly one sleep, no legacy dump."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)
        ws = _lookup_seq_ws([[], [{"entity_id": "scene.led_desk_strip_night_light"}]])
        client = RoutingClient()
        with patch_ws(ws, component_config_reads):
            entity_id = await ConfigSceneTools(client)._resolve_scene_entity_id(
                "night_light_led_desk_strip", allow_component=True
            )
        assert entity_id == "scene.led_desk_strip_night_light"
        assert len(_lookup_calls(ws)) == 2
        assert sleep_mock.await_count == 1
        assert sleep_mock.await_args.args == (ConfigSceneTools._RESOLVE_RETRY_DELAY,)
        assert client.ws_types["config/entity_registry/list"] == 0

    @pytest.mark.asyncio
    async def test_component_empty_twice_falls_back_to_naive(self, monkeypatch) -> None:
        """Empty on BOTH the first lookup and the recheck ⇒ the authoritative
        naive ``scene.{scene_id}`` fallback, after exactly one recheck sleep and
        still no legacy registry dump."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_result={"matches": []},
        )
        client = RoutingClient()
        with patch_ws(ws, component_config_reads):
            entity_id = await ConfigSceneTools(client)._resolve_scene_entity_id(
                "movie_night", allow_component=True
            )
        assert entity_id == "scene.movie_night"
        assert client.ws_types["config/entity_registry/list"] == 0
        assert len(_lookup_calls(ws)) == 2
        assert sleep_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_component_empty_then_recheck_none_falls_back_to_legacy(
        self, monkeypatch
    ) -> None:
        """First lookup empty, then the recheck itself comes back unavailable
        (``None`` — the component errored/downgraded mid-retry) ⇒ the first empty
        is NOT authoritative, so resolution drops to the legacy registry list+retry
        (which resolves the scene) rather than the naive ``scene.{scene_id}``
        guess. Two component frames, one recheck sleep, and the legacy dump runs."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)

        idx = {"n": 0}

        async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
            if command_type == "ha_mcp_tools/info":
                return {"success": True, "result": _CAPS_ENTITY_LOOKUP}
            if command_type == "ha_mcp_tools/entity_lookup":
                idx["n"] += 1
                if idx["n"] == 1:
                    return {"success": True, "result": {"matches": []}}
                # The recheck frame fails — component went unavailable mid-retry,
                # so fetch_entity_lookup_via_component returns None.
                raise HomeAssistantCommandTimeout("recheck timeout")
            raise AssertionError(f"unexpected command {command_type!r}")

        ws = AsyncMock()
        ws.send_command = AsyncMock(side_effect=_send)

        client = RoutingClient(
            registry_list=[
                {"entity_id": "scene.movie_night", "unique_id": "movie_night"}
            ]
        )
        with patch_ws(ws, component_config_reads):
            entity_id = await ConfigSceneTools(client)._resolve_scene_entity_id(
                "movie_night", allow_component=True
            )
        # Resolved from the legacy registry dump, not the naive guess.
        assert entity_id == "scene.movie_night"
        assert client.ws_types["config/entity_registry/list"] == 1
        assert len(_lookup_calls(ws)) == 2
        assert sleep_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_get_path_never_touches_component(self) -> None:
        """With ``allow_component`` unset (the config-get default), the resolver
        never probes caps nor sends a frame even though the component advertises
        entity_lookup — it serves from the legacy registry list. Guards the
        TestConfigGetSeam invariant at the resolver level."""
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_result={"matches": [{"entity_id": "scene.wrong"}]},
        )
        client = RoutingClient(
            registry_list=[
                {"entity_id": "scene.movie_night", "unique_id": "movie_night"}
            ]
        )
        with patch_ws(ws, component_config_reads):
            entity_id = await ConfigSceneTools(client)._resolve_scene_entity_id(
                "movie_night"
            )
        assert entity_id == "scene.movie_night"
        # The component WS was never touched — not even the caps probe.
        assert ws.send_command.await_count == 0
        assert client.ws_types["config/entity_registry/list"] == 1

    @pytest.mark.asyncio
    async def test_caps_miss_falls_back_to_legacy(self) -> None:
        """A component that answers ``info`` but lacks entity_lookup → legacy list."""
        ws = make_ws("ha_mcp_tools/entity_lookup", info_result=_CAPS_OTHER)
        client = RoutingClient(
            registry_list=[
                {"entity_id": "scene.movie_night", "unique_id": "movie_night"}
            ]
        )
        with patch_ws(ws, component_config_reads):
            entity_id = await ConfigSceneTools(client)._resolve_scene_entity_id(
                "movie_night", allow_component=True
            )
        assert entity_id == "scene.movie_night"
        assert client.ws_types["config/entity_registry/list"] == 1
        assert not _lookup_calls(ws)

    @pytest.mark.asyncio
    async def test_unknown_command_invalidates_and_falls_back(self) -> None:
        """unknown_command on entity_lookup → invalidate caps + legacy list."""
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
        )
        client = RoutingClient(
            registry_list=[
                {"entity_id": "scene.movie_night", "unique_id": "movie_night"}
            ]
        )
        with patch_ws(ws, component_config_reads):
            entity_id = await ConfigSceneTools(client)._resolve_scene_entity_id(
                "movie_night", allow_component=True
            )
        assert entity_id == "scene.movie_night"
        assert client.ws_types["config/entity_registry/list"] == 1
        assert client not in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_command_timeout_falls_back_without_invalidating(self) -> None:
        """A non-unknown command error (timeout) → legacy list WITHOUT dropping the
        still-advertised capability."""
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_exc=HomeAssistantCommandTimeout("timeout"),
        )
        client = RoutingClient(
            registry_list=[
                {"entity_id": "scene.movie_night", "unique_id": "movie_night"}
            ]
        )
        with patch_ws(ws, component_config_reads):
            entity_id = await ConfigSceneTools(client)._resolve_scene_entity_id(
                "movie_night", allow_component=True
            )
        assert entity_id == "scene.movie_night"
        assert client.ws_types["config/entity_registry/list"] == 1
        assert client in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_connection_error_propagates(self) -> None:
        """A WS-down error on the entity_lookup frame propagates (the legacy path
        shares the socket and would fail identically) — not caught here."""
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_exc=HomeAssistantConnectionError("ws down"),
        )
        client = RoutingClient()
        with (
            patch_ws(ws, component_config_reads),
            pytest.raises(HomeAssistantConnectionError),
        ):
            await ConfigSceneTools(client)._resolve_scene_entity_id(
                "movie_night", allow_component=True
            )
        # The legacy dump was never reached.
        assert client.ws_types["config/entity_registry/list"] == 0


# =============================================================================
# Automation resolver — _resolve_automation_entity_id
# =============================================================================
class TestAutomationResolverRouting:
    def _states(self) -> list[dict[str, Any]]:
        return [
            {"entity_id": "automation.morning", "attributes": {"id": "uid-1"}},
        ]

    @pytest.mark.asyncio
    async def test_component_preferred_no_states_scan(self) -> None:
        """entity_lookup serves the resolve; the whole ``get_states()`` scan never
        runs."""
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_result={"matches": [{"entity_id": "automation.morning"}]},
        )
        client = RoutingClient(states=self._states())
        with patch_ws(ws, component_config_reads):
            entity_id = await AutomationConfigTools(
                client
            )._resolve_automation_entity_id("uid-1", allow_component=True)
        assert entity_id == "automation.morning"
        assert client.get_states.await_count == 0
        assert _lookup_calls(ws)[0].kwargs == {
            "unique_id": "uid-1",
            "domain": "automation",
        }

    @pytest.mark.asyncio
    async def test_already_entity_id_short_circuits(self) -> None:
        """An ``automation.`` identifier is returned as-is — no component, no
        legacy fetch (even with allow_component)."""
        ws = make_ws("ha_mcp_tools/entity_lookup", info_result=_CAPS_ENTITY_LOOKUP)
        client = RoutingClient()
        with patch_ws(ws, component_config_reads):
            entity_id = await AutomationConfigTools(
                client
            )._resolve_automation_entity_id("automation.morning", allow_component=True)
        assert entity_id == "automation.morning"
        assert ws.send_command.await_count == 0
        assert client.get_states.await_count == 0

    @pytest.mark.asyncio
    async def test_component_miss_returns_none(self) -> None:
        """An authoritative empty ``matches`` returns ``None`` — parity with the
        legacy no-match — without scanning states."""
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_result={"matches": []},
        )
        client = RoutingClient(states=self._states())
        with patch_ws(ws, component_config_reads):
            entity_id = await AutomationConfigTools(
                client
            )._resolve_automation_entity_id("uid-1", allow_component=True)
        assert entity_id is None
        assert client.get_states.await_count == 0

    @pytest.mark.asyncio
    async def test_get_path_never_touches_component(self) -> None:
        """``allow_component`` unset → the legacy state scan serves it, the
        component WS is never touched (config-get invariant at resolver level)."""
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_result={"matches": [{"entity_id": "automation.wrong"}]},
        )
        client = RoutingClient(states=self._states())
        with patch_ws(ws, component_config_reads):
            entity_id = await AutomationConfigTools(
                client
            )._resolve_automation_entity_id("uid-1")
        assert entity_id == "automation.morning"
        assert ws.send_command.await_count == 0
        assert client.get_states.await_count == 1

    @pytest.mark.asyncio
    async def test_caps_miss_falls_back_to_states(self) -> None:
        ws = make_ws("ha_mcp_tools/entity_lookup", info_result=_CAPS_OTHER)
        client = RoutingClient(states=self._states())
        with patch_ws(ws, component_config_reads):
            entity_id = await AutomationConfigTools(
                client
            )._resolve_automation_entity_id("uid-1", allow_component=True)
        assert entity_id == "automation.morning"
        assert client.get_states.await_count == 1
        assert not _lookup_calls(ws)

    @pytest.mark.asyncio
    async def test_unknown_command_invalidates_and_falls_back(self) -> None:
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
        )
        client = RoutingClient(states=self._states())
        with patch_ws(ws, component_config_reads):
            entity_id = await AutomationConfigTools(
                client
            )._resolve_automation_entity_id("uid-1", allow_component=True)
        assert entity_id == "automation.morning"
        assert client.get_states.await_count == 1
        assert client not in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_command_timeout_falls_back_without_invalidating(self) -> None:
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_exc=HomeAssistantCommandTimeout("timeout"),
        )
        client = RoutingClient(states=self._states())
        with patch_ws(ws, component_config_reads):
            entity_id = await AutomationConfigTools(
                client
            )._resolve_automation_entity_id("uid-1", allow_component=True)
        assert entity_id == "automation.morning"
        assert client.get_states.await_count == 1
        assert client in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_connection_error_propagates(self) -> None:
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_exc=HomeAssistantConnectionError("ws down"),
        )
        client = RoutingClient(states=self._states())
        with (
            patch_ws(ws, component_config_reads),
            pytest.raises(HomeAssistantConnectionError),
        ):
            await AutomationConfigTools(client)._resolve_automation_entity_id(
                "uid-1", allow_component=True
            )
        assert client.get_states.await_count == 0


# =============================================================================
# Reference validator — validate_config_references
# =============================================================================
# A config with one valid + one bogus service AND one valid + one bogus entity,
# so both paths must produce the same two warnings from the same registry data.
_CONFIG = {
    "action": [
        {"service": "light.turn_on", "entity_id": "light.a"},
        {"service": "light.bogus", "entity_id": "light.ghost"},
    ]
}
_SERVICES = [{"domain": "light", "services": {"turn_on": {}}}]
_ENTITY_IDS = ["light.a"]


def _warning_set(result: dict[str, Any]) -> set[tuple[str, str]]:
    return {(w["value"], w["kind"]) for w in result["warnings"]}


class TestReferenceDataRouting:
    @pytest.mark.asyncio
    async def test_component_preferred_no_rest_fetches(self) -> None:
        """reference_data serves both indexes in one frame; neither
        ``get_services`` nor ``get_states`` is called, and the warnings are the
        expected two."""
        ws = make_ws(
            "ha_mcp_tools/reference_data",
            info_result=_CAPS_REFERENCE_DATA,
            cmd_result={"services": _SERVICES, "entity_ids": _ENTITY_IDS},
        )
        client = RoutingClient()
        with patch_ws(ws, component_config_reads):
            result = await validate_config_references(client, _CONFIG)
        assert _warning_set(result) == {
            ("light.bogus", "service"),
            ("light.ghost", "entity"),
        }
        assert client.get_services.await_count == 0
        assert client.get_states.await_count == 0

    @pytest.mark.asyncio
    async def test_caps_miss_uses_legacy_gather(self) -> None:
        ws = make_ws("ha_mcp_tools/reference_data", info_result=_CAPS_OTHER)
        client = RoutingClient(
            services=_SERVICES, states=[{"entity_id": e} for e in _ENTITY_IDS]
        )
        with patch_ws(ws, component_config_reads):
            result = await validate_config_references(client, _CONFIG)
        assert _warning_set(result) == {
            ("light.bogus", "service"),
            ("light.ghost", "entity"),
        }
        assert client.get_services.await_count == 1
        assert client.get_states.await_count == 1

    @pytest.mark.asyncio
    async def test_unknown_command_invalidates_and_gathers(self) -> None:
        ws = make_ws(
            "ha_mcp_tools/reference_data",
            info_result=_CAPS_REFERENCE_DATA,
            cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
        )
        client = RoutingClient(
            services=_SERVICES, states=[{"entity_id": e} for e in _ENTITY_IDS]
        )
        with patch_ws(ws, component_config_reads):
            result = await validate_config_references(client, _CONFIG)
        assert _warning_set(result) == {
            ("light.bogus", "service"),
            ("light.ghost", "entity"),
        }
        assert client.get_services.await_count == 1
        assert client.get_states.await_count == 1
        assert client not in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_command_timeout_gathers_without_invalidating(self) -> None:
        ws = make_ws(
            "ha_mcp_tools/reference_data",
            info_result=_CAPS_REFERENCE_DATA,
            cmd_exc=HomeAssistantCommandTimeout("timeout"),
        )
        client = RoutingClient(
            services=_SERVICES, states=[{"entity_id": e} for e in _ENTITY_IDS]
        )
        with patch_ws(ws, component_config_reads):
            result = await validate_config_references(client, _CONFIG)
        assert _warning_set(result) == {
            ("light.bogus", "service"),
            ("light.ghost", "entity"),
        }
        assert client.get_services.await_count == 1
        assert client in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_connection_error_falls_back_to_rest(self) -> None:
        """A WS-down error on the reference_data frame falls back to REST.

        Unlike the pooled-WS resolvers, the validator's legacy path is the REST
        ``get_services()`` / ``get_states()`` pair, so a
        ``HomeAssistantConnectionError`` on the component frame must NOT escape into
        the validator's swallow-all guard (which would skip every reference
        warning). ``fetch_reference_data_via_component`` catches it and returns
        ``None``, so the REST fetch runs and the expected two warnings still surface.
        """
        ws = make_ws(
            "ha_mcp_tools/reference_data",
            info_result=_CAPS_REFERENCE_DATA,
            cmd_exc=HomeAssistantConnectionError("ws down"),
        )
        client = RoutingClient(
            services=_SERVICES, states=[{"entity_id": e} for e in _ENTITY_IDS]
        )
        with patch_ws(ws, component_config_reads):
            result = await validate_config_references(client, _CONFIG)
        assert _warning_set(result) == {
            ("light.bogus", "service"),
            ("light.ghost", "entity"),
        }
        # The REST legacy fetch served both indexes.
        assert client.get_services.await_count == 1
        assert client.get_states.await_count == 1
        # A transient connection error is not a downgrade — caps stay cached.
        assert client in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_ws_establish_failure_falls_back_to_rest(self) -> None:
        """``get_websocket_client()`` raising a plain ``Exception`` falls back to REST.

        After caps are cached, ``WebSocketManager`` can raise a plain ``Exception``
        (not ``HomeAssistantConnectionError``) when it cannot (re)establish the
        pooled socket. ``fetch_reference_data_via_component`` catches it broadly and
        returns ``None`` so the validator's REST legacy fetch still produces the
        reference warnings.
        """
        caps_ws = make_ws(
            "ha_mcp_tools/reference_data", info_result=_CAPS_REFERENCE_DATA
        )
        client = RoutingClient(
            services=_SERVICES, states=[{"entity_id": e} for e in _ENTITY_IDS]
        )
        with patch_ws_establish_failure(
            caps_ws,
            component_config_reads,
            Exception("Failed to connect to Home Assistant WebSocket"),
        ):
            result = await validate_config_references(client, _CONFIG)
        assert _warning_set(result) == {
            ("light.bogus", "service"),
            ("light.ghost", "entity"),
        }
        assert client.get_services.await_count == 1
        assert client.get_states.await_count == 1
        assert client in component_api._CAPS_CACHE
