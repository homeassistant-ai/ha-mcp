"""Routing tests for the entity_lookup component consumer.

A set/remove/post-write consumer pays for a whole-collection fetch to answer
a single question, and routes through the ``ha_mcp_tools`` component when it
advertises the capability:

- ``ConfigSceneTools._resolve_scene_entity_id`` (unique_id -> entity_id) dumps the
  ENTIRE entity registry with a 0.2 s sleep + retry; the component path is one
  ``entity_lookup`` frame on a hit, and ONE recheck frame after a single sleep on
  an empty result (post-upsert registration lag still applies to an in-process
  read). An AUTHORITATIVE empty recheck falls to the naive fallback; a recheck
  that itself returns ``None`` (component went unavailable mid-retry) drops to the
  legacy registry list+retry instead of trusting the empty and guessing.

These pin the standard component-routing taxonomy: component
preferred, capability-miss -> legacy, ``unknown_command`` -> invalidate caps +
legacy, non-unknown command error -> legacy WITHOUT invalidating, AND a
transport failure -> legacy: both a
``HomeAssistantConnectionError`` off the frame AND the same error from
``get_websocket_client()`` failing to establish the socket fall back rather than
propagate. The scene resolver's legacy scan rides the ``send_websocket_message``
bridge (with a naive ``scene.{id}`` fallback), so an escaping error would abort a
landed write / a delete the legacy path would have completed. Plus the GET-path
invariant: with ``allow_component`` unset
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

# A component that answers ``info`` but does not advertise ``entity_lookup``
# (only the unrelated ``search``) — the caps-miss branch.
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
    async def test_connection_error_falls_back_to_legacy(self) -> None:
        """A WS-down error on the entity_lookup frame falls back to the legacy list.

        The resolver's legacy path rides the ``send_websocket_message``
        bridge (and, on an empty scan, the naive ``scene.{id}`` fallback), so it does
        NOT die identically on a pooled-WS drop. ``fetch_entity_lookup_via_component``
        catches the error and returns ``None`` so the legacy scan runs — an escaping
        error would report a landed scene write as failed.
        """
        ws = make_ws(
            "ha_mcp_tools/entity_lookup",
            info_result=_CAPS_ENTITY_LOOKUP,
            cmd_exc=HomeAssistantConnectionError("ws down"),
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
        # A transient connection error is not a downgrade — caps stay cached.
        assert client in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_ws_establish_failure_falls_back_to_legacy(self) -> None:
        """A failed establish in ``get_websocket_client()`` → legacy.

        After caps are cached, ``WebSocketManager`` raises
        ``HomeAssistantConnectionError`` when it cannot (re)establish the pooled
        socket for the read. The helper catches it → ``None`` so the resolver's
        legacy scan runs.
        """
        caps_ws = make_ws("ha_mcp_tools/entity_lookup", info_result=_CAPS_ENTITY_LOOKUP)
        client = RoutingClient(
            registry_list=[
                {"entity_id": "scene.movie_night", "unique_id": "movie_night"}
            ]
        )
        with patch_ws_establish_failure(
            caps_ws,
            component_config_reads,
            HomeAssistantConnectionError(
                "Failed to connect to Home Assistant WebSocket"
            ),
        ):
            entity_id = await ConfigSceneTools(client)._resolve_scene_entity_id(
                "movie_night", allow_component=True
            )
        assert entity_id == "scene.movie_night"
        assert client.ws_types["config/entity_registry/list"] == 1
        assert client in component_api._CAPS_CACHE
