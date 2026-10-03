"""``ha_search`` split route: failure taxonomy of the component leg.

The component ``search`` leg failing falls back to the legacy path exactly as
it did before the dashboards leg existed; the split must not change that.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools.search import component as search_component
from ha_mcp.tools.smart_search import SmartSearchTools

from ._component_routing_helpers import patch_ws
from .test_ha_search_component_routing import (
    DashboardRoutingClient,
    _build_ha_search,
    _setup_visibility_disabled,
)
from .test_ha_search_dashboard_split import (
    _CAPS_SPLIT,
    _dashboards_result,
    _patch_dashboards_ws,
    _search_result,
    _split_ws,
)


class TestDashboardSplitComponentLegFailure:
    """The component leg's failure taxonomy is unchanged by the split."""

    @pytest.mark.asyncio
    async def test_command_error_falls_back_once(self, tmp_path, monkeypatch) -> None:
        """A failing component search serves the WHOLE call from legacy — which
        searches dashboards itself, so the split's own dashboard records must
        not be merged on top of it."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_exc=HomeAssistantCommandError("Command failed: boom", "internal"),
            dashboards_result=_dashboards_result(
                [{"url_path": "energy", "title": "Energy"}]
            ),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, search_component), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"]
            )

        assert resp["success"] is True
        assert any("served via legacy path" in w for w in resp["warnings"])
        assert client.get_states_calls == 1
        # Exactly one "energy" record: the legacy path produced it, and the
        # split leg's copy was dropped rather than merged on top.
        assert [d["url_path"] for d in resp["dashboards"]] == ["energy"]

    @pytest.mark.asyncio
    async def test_component_error_cancels_slow_dashboard_leg(
        self, tmp_path, monkeypatch
    ) -> None:
        """A failed component leg cancels the dashboards leg instead of
        waiting for it: the legacy fallback searches dashboards itself, so a
        slow leg would only delay the fallback and duplicate its I/O."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        state = {"calls": 0, "cancelled": False}

        async def hang_then_serve(
            self: SmartSearchTools,
            query_lower: str,
            exact_match: bool,
            semaphore: asyncio.Semaphore,
            *,
            include_config: bool,
        ) -> tuple[list[dict[str, Any]], int]:
            state["calls"] += 1
            if state["calls"] == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    state["cancelled"] = True
                    raise
            return [], 0

        monkeypatch.setattr(
            SmartSearchTools, "_search_dashboards_surface", hang_then_serve
        )
        ws = _split_ws(
            search_exc=HomeAssistantCommandError("Command failed: boom", "internal"),
            dashboards_result=_dashboards_result([]),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, search_component), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"]
            )

        assert resp["success"] is True
        assert any("served via legacy path" in w for w in resp["warnings"])
        assert state["cancelled"] is True

    @pytest.mark.asyncio
    async def test_parent_cancellation_during_fallback_settle_propagates(
        self, tmp_path, monkeypatch
    ) -> None:
        """A cancellation delivered while the failed-component branch settles
        the leg must propagate — not be consumed right before the legacy
        fallback runs uncancellably (human review on #2291)."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        cancel_seen = asyncio.Event()
        release = asyncio.Event()

        async def stubborn_leg(
            self: SmartSearchTools,
            query_lower: str,
            exact_match: bool,
            semaphore: asyncio.Semaphore,
            *,
            include_config: bool,
        ) -> tuple[list[dict[str, Any]], int]:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_seen.set()
                # Hold through the first cancellation so the caller is
                # provably parked in its settle wait when the test cancels it.
                await release.wait()
                raise
            return [], 0

        monkeypatch.setattr(
            SmartSearchTools, "_search_dashboards_surface", stubborn_leg
        )
        ws = _split_ws(
            search_exc=HomeAssistantCommandError("Command failed: boom", "internal"),
            dashboards_result=_dashboards_result([]),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, search_component), _patch_dashboards_ws(ws):
            call = asyncio.ensure_future(
                ha_search(query="kitchen", search_types=["automation", "dashboard"])
            )
            # The component leg fails fast; once the leg has received its
            # cancel, the call is parked in the settle wait.
            await cancel_seen.wait()
            call.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await call

    @pytest.mark.asyncio
    async def test_parent_cancellation_settles_the_dashboard_leg(
        self, tmp_path, monkeypatch
    ) -> None:
        """Cancelling ha_search settles the dashboards leg BEFORE the call
        completes: the leg task must not outlive the parent with an
        unobserved cancellation."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        search_started = asyncio.Event()
        state = {"cancelled": False}

        async def hang_forever(
            self: SmartSearchTools,
            query_lower: str,
            exact_match: bool,
            semaphore: asyncio.Semaphore,
            *,
            include_config: bool,
        ) -> tuple[list[dict[str, Any]], int]:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            return [], 0

        monkeypatch.setattr(
            SmartSearchTools, "_search_dashboards_surface", hang_forever
        )

        async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
            if command_type == "ha_mcp_tools/info":
                return {"success": True, "result": _CAPS_SPLIT}
            if command_type == "ha_mcp_tools/search":
                search_started.set()
                await asyncio.Event().wait()
            raise AssertionError(f"unexpected command {command_type!r}")

        ws = AsyncMock()
        ws.send_command = AsyncMock(side_effect=_send)
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, search_component), _patch_dashboards_ws(ws):
            call = asyncio.ensure_future(
                ha_search(query="kitchen", search_types=["automation", "dashboard"])
            )
            await search_started.wait()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call

        # The leg settled before the parent call completed — without the
        # settle-await, its CancelledError delivery would still be pending
        # here and the flag unset.
        assert state["cancelled"] is True

    @pytest.mark.asyncio
    async def test_parent_cancellation_at_the_second_await_settles_the_leg(
        self, tmp_path, monkeypatch
    ) -> None:
        """The same invariant at the OTHER await: the component leg has already
        returned, so the call is parked on ``await dashboard_task`` when the
        cancellation lands. Only the second ``except BaseException`` covers
        that point — without it the leg task outlives the call with its
        cancellation still undelivered."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        leg_parked = asyncio.Event()
        component_returned = asyncio.Event()
        state = {"cancelled": False}

        async def hang_forever(
            self: SmartSearchTools,
            query_lower: str,
            exact_match: bool,
            semaphore: asyncio.Semaphore,
            *,
            include_config: bool,
        ) -> tuple[list[dict[str, Any]], int]:
            leg_parked.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            return [], 0

        monkeypatch.setattr(
            SmartSearchTools, "_search_dashboards_surface", hang_forever
        )

        async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
            if command_type == "ha_mcp_tools/info":
                return {"success": True, "result": _CAPS_SPLIT}
            if command_type == "ha_mcp_tools/search":
                # Answer only once the dashboards leg is parked, so the leg is
                # provably still pending when the component leg completes.
                await leg_parked.wait()
                component_returned.set()
                return {"success": True, "result": _search_result()}
            raise AssertionError(f"unexpected command {command_type!r}")

        ws = AsyncMock()
        ws.send_command = AsyncMock(side_effect=_send)
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, search_component), _patch_dashboards_ws(ws):
            call = asyncio.ensure_future(
                ha_search(query="kitchen", search_types=["automation", "dashboard"])
            )
            await component_returned.wait()
            # Let the call resume from ``await component_task`` and park on
            # ``await dashboard_task``; cancelling before that would land on
            # the first await instead, which the test above already covers.
            for _ in range(2):
                await asyncio.sleep(0)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call

        assert state["cancelled"] is True

    @pytest.mark.asyncio
    async def test_unknown_command_falls_back_silently(
        self, tmp_path, monkeypatch
    ) -> None:
        """A component downgraded mid-session is still a silent legacy route."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_exc=HomeAssistantCommandError(
                "Command failed: nope", "unknown_command"
            ),
            dashboards_result=_dashboards_result([]),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, search_component), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"]
            )

        assert resp["success"] is True
        assert client.get_states_calls == 1
        assert not any("served via legacy path" in w for w in resp["warnings"])
