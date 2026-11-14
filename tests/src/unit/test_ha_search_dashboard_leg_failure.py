"""``ha_search`` split route: failure taxonomy of the component leg.

The component ``search`` leg failing falls back to the legacy path exactly as
it did before the dashboards leg existed; the split must not change that.

The server's client runs on a real ``hass`` with the component's commands. Core
answers the capability probe, the dashboards reads and the legacy reads. The
component's search fails with Core's own error reply, and where a leg has to
stall, Core holds that leg's own command (``_held_command.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools.websocket_api import system as component_system
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_search
from ha_mcp.tools.component_api import DEVICE_REGISTRY_CHILD_SEMANTICS, ComponentCaps
from ha_mcp.tools.search import component as search_component
from ha_mcp.tools.tools_config_dashboards import WS_DASHBOARDS

from ._held_command import held_command
from ._search_tool import _build_ha_search, _setup_visibility_disabled

_DASHBOARD_PATH = "energy-board"
_COMPONENT_SEARCH = "ha_mcp_tools/search"
_QUERY: dict[str, Any] = {
    "query": "kitchen",
    "search_types": ["automation", "dashboard"],
}


def _fail_component_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the component's config-surface search raise, so Core replies with an error."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise HomeAssistantError("boom")

    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.websocket_api.search._search_config_surface",
        _boom,
    )


async def _store_energy_dashboard(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """Save a storage dashboard whose config mentions "kitchen", through Core."""
    assert await async_setup_component(hass, "lovelace", {})
    created = await client.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": _DASHBOARD_PATH,
            "title": "Energy",
        }
    )
    assert created["success"], created
    saved = await client.send_websocket_message(
        {
            "type": "lovelace/config/save",
            "url_path": _DASHBOARD_PATH,
            "config": {"views": [{"title": "Kitchen", "cards": []}]},
        }
    )
    assert saved["success"], saved


async def _store_second_dashboard(client: HomeAssistantClient) -> None:
    created = await client.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": "kitchen-board",
            "title": "K",
        }
    )
    assert created["success"], created


def _component_without_doc_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """A component from before the dashboards document search: the dashboards
    leg walks each dashboard's config itself, one ``lovelace/config`` read per
    dashboard, side by side."""
    monkeypatch.setattr(
        component_system,
        "CAPABILITIES",
        [c for c in component_system.CAPABILITIES if c != "dashboards_doc_search"],
    )


def _dashboard_legs() -> list[asyncio.Task[Any]]:
    """The dashboards leg tasks still running."""
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done()
        and "_search_dashboards_leg" in getattr(task.get_coro(), "__qualname__", "")
    ]


def _dashboard_leg_pending() -> bool:
    return bool(_dashboard_legs())


async def _until_the_leg_is_being_cancelled() -> None:
    """Yield until the call has cancelled its dashboards leg, which then needs
    several loop steps to unwind its side-by-side dashboard reads."""
    for _ in range(1000):
        if any(task.cancelling() for task in _dashboard_legs()):
            return
        await asyncio.sleep(0)
    raise AssertionError("the dashboards leg was never cancelled")


class TestDashboardSplitComponentLegFailure:
    """The component leg's failure taxonomy is unchanged by the split."""

    async def test_command_error_falls_back_once(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing component search serves the WHOLE call from legacy — which
        searches dashboards itself, so the split's own dashboard records must
        not be merged on top of it."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        await _store_energy_dashboard(hass, component_core_client)
        _fail_component_search(monkeypatch)
        monkeypatch.setattr(
            component_core_client,
            "get_states",
            AsyncMock(wraps=component_core_client.get_states),
        )
        ha_search = _build_ha_search(component_core_client)

        resp = await ha_search(**_QUERY)

        assert resp["success"] is True
        assert any("served via legacy path" in w for w in resp["warnings"])
        assert cast("AsyncMock", component_core_client.get_states).await_count == 1
        # Exactly one dashboard record: the legacy path produced it, and the
        # split leg's copy was dropped rather than merged on top.
        assert [d["url_path"] for d in resp["dashboards"]] == [_DASHBOARD_PATH]

    async def test_component_error_cancels_slow_dashboard_leg(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed component leg cancels the dashboards leg instead of
        waiting for it: the legacy fallback searches dashboards itself, so a
        slow leg would only delay the fallback and duplicate its I/O."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        await _store_energy_dashboard(hass, component_core_client)
        _fail_component_search(monkeypatch)
        ha_search = _build_ha_search(component_core_client)

        # Only the leg's request is held; the legacy fallback's own
        # dashboards read is answered at once.
        async with (
            held_command(hass, WS_DASHBOARDS, first_only=True) as held,
            asyncio.timeout(10),
        ):
            resp = await ha_search(**_QUERY)
            assert held.arrived.is_set()

        assert resp["success"] is True
        assert any("served via legacy path" in w for w in resp["warnings"])
        assert [d["url_path"] for d in resp["dashboards"]] == [_DASHBOARD_PATH]

    async def test_parent_cancellation_settles_the_dashboard_leg(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancelling ha_search while both legs wait settles the dashboards
        leg BEFORE the call completes: the leg task must not outlive the
        parent with an unobserved cancellation."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        _component_without_doc_search(monkeypatch)
        await _store_energy_dashboard(hass, component_core_client)
        await _store_second_dashboard(component_core_client)
        ha_search = _build_ha_search(component_core_client)

        async with (
            held_command(hass, "lovelace/config") as dashboards,
            held_command(hass, _COMPONENT_SEARCH) as search,
        ):
            call = asyncio.ensure_future(ha_search(**_QUERY))
            await dashboards.arrived.wait()
            await search.arrived.wait()
            assert _dashboard_leg_pending()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert not _dashboard_leg_pending()

    async def test_parent_cancellation_during_fallback_settle_propagates(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cancellation delivered while the failed-component branch settles
        the leg must propagate — not be consumed right before the legacy
        fallback runs uncancellably (human review on #2291)."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        _component_without_doc_search(monkeypatch)
        await _store_energy_dashboard(hass, component_core_client)
        await _store_second_dashboard(component_core_client)
        _fail_component_search(monkeypatch)
        ha_search = _build_ha_search(component_core_client)

        async with (
            held_command(hass, "lovelace/config") as dashboards,
            held_command(hass, _COMPONENT_SEARCH) as search,
        ):
            call = asyncio.ensure_future(ha_search(**_QUERY))
            await dashboards.arrived.wait()
            # The leg is reading dashboards; now let the component search fail.
            search.release.set()
            await _until_the_leg_is_being_cancelled()
            call.cancel()
            async with asyncio.timeout(10):
                with pytest.raises(asyncio.CancelledError):
                    await call

    async def test_unknown_command_falls_back_silently(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Capabilities cached as serving search, but Core, without the
        component, answers ``ha_mcp_tools/search`` unknown_command: the stale
        capabilities are dropped and the route is a silent legacy one, with no
        warning naming a failed component."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        assert await async_setup_component(hass, "config", {})
        served = ComponentCaps(
            1, "2.2.2", frozenset({"search", DEVICE_REGISTRY_CHILD_SEMANTICS}), {}
        )
        monkeypatch.setattr(
            tools_search, "get_component_caps", AsyncMock(return_value=served)
        )
        invalidated = MagicMock(wraps=search_component.invalidate_caps)
        monkeypatch.setattr(search_component, "invalidate_caps", invalidated)
        monkeypatch.setattr(
            core_client, "get_states", AsyncMock(wraps=core_client.get_states)
        )
        ha_search = _build_ha_search(core_client)

        resp = await ha_search(**_QUERY)

        assert resp["success"] is True
        invalidated.assert_called_once_with(core_client)
        assert cast("AsyncMock", core_client.get_states).await_count == 1
        assert not any("served via legacy path" in w for w in resp["warnings"])
