"""Degrading on a missing registry is fine; degrading silently is not.

Since #1947 ``send_websocket_message`` raises on a dead transport instead of
returning a failure envelope. The enrichment call sites deliberately keep
serving a partial answer rather than failing the whole call — but a thin
result must say what was skipped and why, otherwise "the area registry never
arrived" is indistinguishable from "there are no areas", which is the same
answers-while-blind shape the detector fix exists to prevent.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.smart_search._base import _SearchBase

_AREA_LIST = "config/area_registry/list"


def _make_client(*, failing: set[str], exc: Exception | None = None) -> MagicMock:
    """Client whose WS registry reads in ``failing`` raise (or return a failed
    envelope when ``exc`` is None), and succeed otherwise."""
    client = MagicMock()
    client.base_url = "http://ha.local:8123"
    client.token = "t"
    client.get_states = AsyncMock(return_value=[])
    client.get_services = AsyncMock(return_value={})

    async def _ws(message: dict[str, Any]) -> dict[str, Any]:
        msg_type = message.get("type")
        if msg_type in failing:
            if exc is not None:
                raise exc
            return {"success": False, "error": "unknown command"}
        return {"success": True, "result": []}

    client.send_websocket_message = AsyncMock(side_effect=_ws)
    return client


class TestExtractRegistryList:
    """The shared unwrap helper is where the silence used to live."""

    def test_exception_names_itself_in_warnings(self) -> None:
        warnings: list[str] = []
        result = _SearchBase._extract_registry_list(
            HomeAssistantConnectionError("ws gone"), "area registry", warnings
        )

        assert result == []
        assert warnings == ["area registry unavailable: ws gone"]

    def test_failed_envelope_names_itself_in_warnings(self) -> None:
        warnings: list[str] = []
        _SearchBase._extract_registry_list(
            {"success": False, "error": "unknown command"}, "entity registry", warnings
        )

        assert warnings == ["entity registry unavailable: unknown command"]

    def test_success_adds_no_warning(self) -> None:
        warnings: list[str] = []
        result = _SearchBase._extract_registry_list(
            {"success": True, "result": [{"area_id": "kitchen"}]},
            "area registry",
            warnings,
        )

        assert result == [{"area_id": "kitchen"}]
        assert warnings == []

    def test_caller_without_a_warnings_channel_still_degrades(self) -> None:
        """The parameter is optional: a caller that has nowhere to put the line
        must keep working rather than raise."""
        assert _SearchBase._extract_registry_list(ValueError("boom"), "x") == []


class TestOverviewReportsSkippedEnrichment:
    @pytest.mark.asyncio
    async def test_dead_area_registry_warns_and_marks_partial(self) -> None:
        tools = SmartSearchTools(
            client=_make_client(
                failing={_AREA_LIST}, exc=HomeAssistantConnectionError("ws gone")
            )
        )

        response = await tools.get_system_overview()

        assert response["success"] is True
        assert any("area registry unavailable" in w for w in response["warnings"]), (
            response.get("warnings")
        )
        # An overview whose area stats are thin because a registry never
        # arrived is genuinely incomplete, not merely annotated.
        assert response["partial"] is True

    @pytest.mark.asyncio
    async def test_healthy_registries_produce_no_registry_warning(self) -> None:
        tools = SmartSearchTools(client=_make_client(failing=set()))

        response = await tools.get_system_overview()

        assert "partial" not in response
        assert not any("unavailable" in w for w in response.get("warnings", [])), (
            response.get("warnings")
        )


class TestAreaSearchReportsSkippedEnrichment:
    @pytest.mark.asyncio
    async def test_dead_area_registry_warns_instead_of_bare_no_match(self) -> None:
        """ "No areas found" while the area registry is unreachable is the
        answers-while-blind shape: the caller would read it as "this area does
        not exist"."""
        tools = SmartSearchTools(
            client=_make_client(
                failing={_AREA_LIST}, exc=HomeAssistantConnectionError("ws gone")
            )
        )

        response = await tools.get_entities_by_area("kitchen")

        assert response["total_areas_found"] == 0
        assert any("area registry unavailable" in w for w in response["warnings"]), (
            response.get("warnings")
        )

    @pytest.mark.asyncio
    async def test_healthy_registries_produce_no_registry_warning(self) -> None:
        tools = SmartSearchTools(client=_make_client(failing=set()))

        response = await tools.get_entities_by_area("kitchen")

        assert not any("unavailable" in w for w in response.get("warnings", [])), (
            response.get("warnings")
        )
