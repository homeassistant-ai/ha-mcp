"""Routing tests for ``ha_get_overview`` over the ``ha_mcp_tools`` component gate.

When the component advertises the ``overview`` capability, ``ha_get_overview``
fetches the eight raw reads (states + services + the three registries + config +
notifications + repairs) in one ``ha_mcp_tools/overview`` WebSocket call and
feeds those slices into its unchanged assembly, skipping the legacy ~8-fetch
pipeline. These tests pin that fast path, the error-taxonomy fallbacks (silent
on ``unknown_command``; legacy + ``warnings[]`` on a command timeout, a dropped
connection or a degraded slice), the active-visibility routing (the filter is
applied server-side, so the component still serves), and parity between the
two serving paths over the same Core.

The tool runs on the server's own client against a real ``hass``; spies on
the client's legacy reads let a test assert none ran on the component path.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import websocket_api as component_ws_api
from custom_components.ha_mcp_tools.websocket_api import system as component_system
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config

from ._client_spies import sent_commands
from ._component_frames import drop_connection_on, held
from ._search_tool import _setup_visibility_disabled

_OVERVIEW = "ha_mcp_tools/overview"
_FELL_BACK = "served via legacy path"


@pytest.fixture(autouse=True)
def _quiet_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the wrapper's server-side tail: no settings sidecar or HTTP mount
    on the host can leak a ``settings_url`` into the assertions."""
    monkeypatch.setattr("ha_mcp.stdio_settings_sidecar.live_sidecar_url", lambda: None)
    monkeypatch.setattr("ha_mcp.settings_ui.get_http_settings_prefix", lambda: None)


@pytest.fixture
async def client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` on a ``hass`` that answers every legacy overview read,
    with a light and a temperature sensor in a Kitchen area."""
    for domain in ("config", "persistent_notification", "repairs"):
        assert await async_setup_component(hass, domain, {})
    kitchen = ar.async_get(hass).async_create("Kitchen")
    registry = er.async_get(hass)
    for domain, object_id in (("light", "kitchen"), ("sensor", "kitchen_temp")):
        entry = registry.async_get_or_create(
            domain, "test", object_id, suggested_object_id=object_id
        )
        registry.async_update_entity(entry.entity_id, area_id=kitchen.id)
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen"})
    hass.states.async_set(
        "sensor.kitchen_temp",
        "21",
        {"friendly_name": "Kitchen Temp", "device_class": "temperature"},
    )
    return core_client


@pytest.fixture
def component(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The same client with the component's commands registered."""
    return component_core_client


@pytest.fixture
async def socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


@pytest.fixture
def hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An active visibility filter denying ``light.kitchen``."""
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True, exclude_categories=[], deny_entity_ids=["light.kitchen"]
        ),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)


@pytest.fixture
def unfiltered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_visibility_disabled(tmp_path, monkeypatch)


def _tools(client: HomeAssistantClient) -> SearchTools:
    return SearchTools(client, SmartSearchTools(client=client))


@contextmanager
def _legacy_reads(client: HomeAssistantClient) -> Iterator[list[AsyncMock]]:
    """Spies on every legacy overview read; Core still answers each."""
    with (
        patch.object(client, "get_states", wraps=client.get_states) as states,
        patch.object(client, "get_services", wraps=client.get_services) as services,
        patch.object(client, "get_config", wraps=client.get_config) as config,
        patch.object(
            client, "send_websocket_message", wraps=client.send_websocket_message
        ) as bridge,
    ):
        yield [states, services, config, bridge]


def _total(spies: list[AsyncMock]) -> int:
    return sum(spy.await_count for spy in spies)


def _overview_frames(spy: AsyncMock) -> int:
    return sum(1 for c in spy.call_args_list if c.args[0] == _OVERVIEW)


async def _overview(
    client: HomeAssistantClient,
) -> tuple[dict[str, Any], list[AsyncMock]]:
    with _legacy_reads(client) as spies:
        resp: dict[str, Any] = await _tools(client).ha_get_overview(
            detail_level="standard"
        )
    assert resp["success"] is True
    return resp, spies


def _fell_back(resp: dict[str, Any], reason: str) -> bool:
    return any(reason in w and _FELL_BACK in w for w in resp.get("warnings", []))


@pytest.mark.usefixtures("unfiltered")
class TestRouting:
    async def test_component_fast_path_skips_legacy_fetches(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """When the component serves overview, none of the legacy fetches run."""
        with sent_commands(socket) as sent:
            resp, spies = await _overview(client)

        assert resp["system_summary"]["total_entities"] == len(hass.states.async_all())
        assert resp["system_info"]["version"] == hass.config.as_dict()["version"]
        assert _total(spies) == 0
        assert _overview_frames(sent) == 1

    async def test_a_component_without_child_device_semantics_uses_legacy(
        self,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A component predating the child-device fix advertises ``overview``
        but cannot authorize its device-registry slices."""
        monkeypatch.setattr(
            component_system,
            "CAPABILITIES",
            [
                c
                for c in component_system.CAPABILITIES
                if c != "device_registry_child_semantics"
            ],
        )

        with sent_commands(socket) as sent:
            _, spies = await _overview(client)

        assert _total(spies) > 0
        assert _overview_frames(sent) == 0

    async def test_caps_probed_once_across_overviews(
        self,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """The info probe is cached: two overviews, one ha_mcp_tools/info call."""
        with sent_commands(socket) as sent:
            await _overview(client)
            await _overview(client)

        assert [c.args[0] for c in sent.call_args_list].count("ha_mcp_tools/info") == 1

    async def test_without_the_component_uses_legacy(
        self, client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
    ) -> None:
        """No component (``info`` is ``unknown_command``) → the legacy path, and
        the overview command is never attempted."""
        with sent_commands(socket) as sent:
            _, spies = await _overview(client)

        states = spies[0]
        states.assert_awaited_once()
        assert _overview_frames(sent) == 0


@pytest.mark.usefixtures("unfiltered")
class TestFallbacks:
    async def test_unknown_command_falls_back_silently(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A component downgraded since the probe no longer registers the
        command; Core answers ``unknown_command`` → legacy, no warning."""
        monkeypatch.delitem(hass.data["websocket_api"], _OVERVIEW)

        resp, (states, _, config, bridge) = await _overview(client)

        states.assert_awaited_once()
        config.assert_awaited_once()
        sent = [c.args[0]["type"] for c in bridge.await_args_list]
        assert sent.count("config/entity_registry/list") == 1
        assert sent.count("repairs/list_issues") == 1
        assert not any(_FELL_BACK in w for w in resp.get("warnings", []))

    async def test_command_timeout_falls_back_with_warning(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The component's overview answering too late → legacy + a warning.
        A command error other than ``unknown_command`` takes the same branch."""
        async with held(hass, _OVERVIEW, monkeypatch, client=client):
            resp, (states, *_) = await _overview(client)

        states.assert_awaited_once()
        assert _fell_back(resp, "component overview path failed")

    async def test_connection_drop_falls_back_with_warning(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """A dropped connection on the overview command → legacy + a warning,
        not a propagated error: the legacy overview reads REST states/services
        and the registry bridge, which reconnects."""
        drop_connection_on(hass, _OVERVIEW, socket)

        resp, (states, *_) = await _overview(client)

        states.assert_awaited_once()
        assert _fell_back(resp, "component overview connection error")

    async def test_a_degraded_slice_falls_back_with_warning(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A slice the component names in ``slice_errors`` → legacy + a warning,
        so a partial snapshot never serves a silently degraded overview. Core
        failing to compose an entity's name degrades the component's
        ``entity_registry`` slice; Core's registry list does not compose it."""
        monkeypatch.setattr(
            er,
            "async_get_full_entity_name",
            MagicMock(side_effect=RuntimeError("name unreadable")),
        )

        resp, (states, *_) = await _overview(client)

        states.assert_awaited_once()
        assert _fell_back(resp, "malformed slices")


class TestVisibility:
    @pytest.mark.usefixtures("hidden")
    async def test_visibility_active_still_routes_through_component(
        self,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """Unlike ``ha_search``, the overview applies the visibility filter
        server-side over the component's slices, so a visibility-enabled install
        still takes the one-round-trip component path, and the denied entity is
        excluded from the counts all the same."""
        with sent_commands(socket) as sent:
            resp, spies = await _overview(client)

        assert _overview_frames(sent) == 1
        assert _total(spies) == 0
        assert resp["system_summary"]["total_entities"] == 1
        assert "light" not in resp["domain_stats"]

    async def test_enabled_but_no_active_dimension_still_uses_component(
        self,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """enabled=True with every dimension cleared hides nothing → component."""
        save_visibility_config(
            tmp_path, VisibilityConfig(enabled=True, exclude_categories=[])
        )
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

        with sent_commands(socket) as sent:
            _, spies = await _overview(client)

        assert _overview_frames(sent) == 1
        assert _total(spies) == 0


@pytest.mark.parametrize(
    "visibility", ["unfiltered", "hidden"], ids=["no-filter", "filter-active"]
)
async def test_component_and_legacy_responses_match(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    second_client: HomeAssistantClient,
    request: pytest.FixtureRequest,
    visibility: str,
) -> None:
    """The component path and the legacy path give the same overview of the
    same Core: same assembly, only the data source differs, with or without an
    active visibility filter."""
    request.getfixturevalue(visibility)
    # Probed before the component is registered: this client stays legacy.
    legacy, _ = await _overview(client)
    component_ws_api.async_register_commands(hass)

    component, spies = await _overview(second_client)

    assert _total(spies) == 0
    assert component == legacy


@pytest.mark.usefixtures("unfiltered")
async def test_over_nested_domains_string_is_a_validation_error(
    client: HomeAssistantClient, component: HomeAssistantClient
) -> None:
    """``domains`` was the one list parameter parsed outside a ``ValueError``
    handler, so a string nested past the recursion limit escaped the tool as a
    bare exception and FastMCP reported generic tool text (Codex, PR #2375).
    It is refused as a structured validation error naming the parameter, and
    nothing is fetched."""
    with _legacy_reads(client) as spies, pytest.raises(ToolError) as exc:
        await _tools(client).ha_get_overview(domains="[" * 20_000)

    payload = json.loads(str(exc.value))
    assert payload["error"]["code"].startswith("VALIDATION")
    assert payload.get("parameter") == "domains" or "domains" in str(payload)
    assert _total(spies) == 0
