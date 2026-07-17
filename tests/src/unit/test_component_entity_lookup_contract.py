"""Cross-seam contract tests for the entity_lookup / reference_data seams.

Like the other ``test_component_*_contract`` files, these wire the REAL component
handlers (``_do_entity_lookup`` / ``_do_reference_data``, driven against fake hass
objects) underneath the mocked WS transport and then invoke the REAL server
consumers — so a vocabulary/shape drift on either side fails here rather than at
runtime. The two seams verified:

- ``_do_entity_lookup`` under ``ConfigSceneTools._resolve_scene_entity_id`` (the
  set/remove/post-write path). A multi-platform ``unique_id`` collision fixture —
  a scene and a light sharing one ``unique_id`` — pins that the resolver's
  ``domain="scene"`` narrowing picks the SCENE entity, not the colliding light.
- ``_do_entity_lookup`` under ``AutomationConfigTools._resolve_automation_entity_id``
  pins the load-bearing equivalence the automation routing relies on: an
  automation's registry ``unique_id`` IS its config ``id`` (the same value the
  legacy ``get_states()`` scan matches against ``attributes["id"]``), so the
  component path and the legacy path resolve the SAME entity_id.
- ``_do_reference_data`` under ``validate_config_references`` pins that the
  component-served service index + entity universe produce warnings BYTE-IDENTICAL
  to the legacy ``get_services()`` + ``get_states()`` path over the same fixture.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.tools import component_api, component_config_reads
from ha_mcp.tools.reference_validator import validate_config_references
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from .test_component_ws_search import (
    FakeHass,
    FakeRegEntry,
    FakeServices,
    FakeState,
    make_view,
    wsapi,
)


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _real_ws(hass: FakeHass) -> AsyncMock:
    """A WS mock whose commands are served by the REAL component functions.

    ``info`` returns the real ``_do_info()`` (so the caps probe sees the real
    capability list, entity_lookup / reference_data included), and each read
    command runs the real ``_do_*`` against ``hass`` — the seam under test is
    everything between that return value and the consumer's resolved answer.
    """
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info()}
        params = dict(kwargs)
        if command_type == "ha_mcp_tools/entity_lookup":
            return {"success": True, "result": wsapi._do_entity_lookup(hass, params)}
        if command_type == "ha_mcp_tools/reference_data":
            return {"success": True, "result": wsapi._do_reference_data(hass, params)}
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


class _CredClient:
    """Credentialed HA client: routes through the component. Its legacy fetches
    are spies that MUST stay untouched on the component path."""

    def __init__(self, *, states: list[dict[str, Any]] | None = None) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.ws_types: Counter[str] = Counter()
        self.get_states = AsyncMock(return_value=list(states or []))
        self.get_services = AsyncMock(return_value=[])

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.ws_types[msg.get("type", "")] += 1
        return {"success": True, "result": []}


class _LegacyClient:
    """No credentials → ``get_component_caps`` returns None without probing, so
    ``validate_config_references`` takes the legacy ``get_services`` + ``get_states``
    gather. Used to compute the reference-warning ground truth."""

    def __init__(
        self, services: list[dict[str, Any]], states: list[dict[str, Any]]
    ) -> None:
        self.get_services = AsyncMock(return_value=services)
        self.get_states = AsyncMock(return_value=states)


@contextlib.contextmanager
def _routed(ws: AsyncMock) -> Any:
    """Patch BOTH the caps-probe factory (component_api) and the read-command
    factory (component_config_reads) to the real-backed WS."""
    factory = AsyncMock(return_value=ws)
    with (
        patch.object(component_api, "get_websocket_client", factory),
        patch.object(component_config_reads, "get_websocket_client", factory),
    ):
        yield ws


# =============================================================================
# entity_lookup — scene resolver, multi-platform unique_id collision
# =============================================================================
class TestSceneResolverContract:
    @pytest.mark.asyncio
    async def test_collision_domain_narrowing_picks_scene(self, monkeypatch) -> None:
        """A scene and a light share the unique_id ``movie_night``. The real
        ``_do_entity_lookup`` under the real scene resolver's ``domain="scene"``
        narrowing returns ONLY the scene — the light is never picked."""
        view = make_view(
            entity={
                "scene.movie_night": FakeRegEntry(
                    "scene.movie_night",
                    unique_id="movie_night",
                    platform="homeassistant",
                ),
                # Same unique_id, different domain/platform — the collision.
                "light.movie_night": FakeRegEntry(
                    "light.movie_night", unique_id="movie_night", platform="hue"
                ),
            }
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: view)
        client = _CredClient()
        ws = _real_ws(FakeHass())
        with _routed(ws):
            entity_id = await ConfigSceneTools(client)._resolve_scene_entity_id(
                "movie_night", allow_component=True
            )
        assert entity_id == "scene.movie_night"
        # Served entirely by the component — the legacy registry dump never ran.
        assert client.ws_types["config/entity_registry/list"] == 0

    @pytest.mark.asyncio
    async def test_name_derived_entity_id_resolved(self, monkeypatch) -> None:
        """HA derives a scene's entity_id from its ``name`` slug, so the entity_id
        differs from the ``scene_id`` storage key. The real lookup still resolves
        it by ``unique_id`` — the whole point of the resolver."""
        view = make_view(
            entity={
                "scene.led_desk_strip_night_light": FakeRegEntry(
                    "scene.led_desk_strip_night_light",
                    unique_id="night_light_led_desk_strip",
                    platform="homeassistant",
                ),
            }
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: view)
        client = _CredClient()
        ws = _real_ws(FakeHass())
        with _routed(ws):
            entity_id = await ConfigSceneTools(client)._resolve_scene_entity_id(
                "night_light_led_desk_strip", allow_component=True
            )
        assert entity_id == "scene.led_desk_strip_night_light"


# =============================================================================
# entity_lookup — automation resolver: unique_id == config id equivalence
# =============================================================================
class TestAutomationResolverContract:
    @pytest.mark.asyncio
    async def test_unique_id_equals_config_id_component_and_legacy_agree(
        self, monkeypatch
    ) -> None:
        """The load-bearing equivalence for the automation routing: an
        automation's registry ``unique_id`` IS its config ``id``. The component
        path (matching ``unique_id``) and the legacy ``get_states()`` scan
        (matching ``attributes["id"]``) resolve the SAME entity_id from the same
        underlying automation."""
        config_id = "1699000000000"
        # Registry entry: unique_id == config id (the component's match key).
        view = make_view(
            entity={
                "automation.morning": FakeRegEntry(
                    "automation.morning",
                    unique_id=config_id,
                    platform="automation",
                ),
            }
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: view)
        # State: attributes["id"] == config id (the legacy scan's match key).
        legacy_states = [
            {"entity_id": "automation.morning", "attributes": {"id": config_id}}
        ]

        # Component path.
        component_client = _CredClient(states=legacy_states)
        ws = _real_ws(FakeHass())
        with _routed(ws):
            component_id = await AutomationConfigTools(
                component_client
            )._resolve_automation_entity_id(config_id, allow_component=True)

        # Legacy path (allow_component omitted → legacy get_states scan).
        legacy_client = _CredClient(states=legacy_states)
        legacy_id = await AutomationConfigTools(
            legacy_client
        )._resolve_automation_entity_id(config_id)

        assert component_id == legacy_id == "automation.morning"
        # The component path did not scan states; the legacy path did.
        assert component_client.get_states.await_count == 0
        assert legacy_client.get_states.await_count == 1


# =============================================================================
# reference_data — validator warnings byte-identical to the legacy path
# =============================================================================
# References one valid + one bogus service AND one valid + one bogus entity.
_REF_CONFIG = {
    "action": [
        {"service": "light.turn_on", "entity_id": "light.a"},
        {"service": "climate.bogus", "entity_id": "sensor.ghost"},
    ]
}


class TestReferenceDataContract:
    def _hass(self) -> FakeHass:
        services = FakeServices(
            {
                "light": {"turn_on": object(), "turn_off": object()},
                "climate": {"set_temperature": object()},
            }
        )
        states = [FakeState("light.a"), FakeState("sensor.b")]
        return FakeHass(states=states, services=services)

    @pytest.mark.asyncio
    async def test_component_warnings_identical_to_legacy(self) -> None:
        """The component-served reference_data and the legacy get_services +
        get_states gather build the same indexes, so ``validate_config_references``
        emits byte-identical warnings over the same fixture."""
        hass = self._hass()

        # Component path: real _do_reference_data over `hass`.
        component_client = _CredClient()
        ws = _real_ws(hass)
        with _routed(ws):
            component_result = await validate_config_references(
                component_client, _REF_CONFIG
            )

        # Legacy ground truth: derive the REST payloads from the SAME hass via the
        # real component projections, feed a credential-less (legacy-only) client.
        services_payload = wsapi._overview_services(hass)
        entity_ids = wsapi._do_reference_data(hass, {})["entity_ids"]
        states_payload = [{"entity_id": eid} for eid in entity_ids]
        legacy_result = await validate_config_references(
            _LegacyClient(services_payload, states_payload), _REF_CONFIG
        )

        assert component_result == legacy_result
        # Two warnings: the bogus service and the bogus entity.
        assert {(w["value"], w["kind"]) for w in component_result["warnings"]} == {
            ("climate.bogus", "service"),
            ("sensor.ghost", "entity"),
        }
        # The component path took no legacy REST fetch.
        assert component_client.get_services.await_count == 0
        assert component_client.get_states.await_count == 0
