"""Degrading on a missing registry is fine; degrading silently is not.

Since #1947 ``send_websocket_message`` raises on a dead transport instead of
returning a failure envelope. The enrichment call sites deliberately keep
serving a partial answer rather than failing the whole call — but a thin
result must say what was skipped and why, otherwise "the area registry never
arrived" is indistinguishable from "there are no areas", which is the same
answers-while-blind shape the detector fix exists to prevent.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.smart_search._base import _SearchBase
from ha_mcp.tools.tools_registry import _get_single_device_result

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


class TestIdentifierResolversFailLoud:
    """``_resolve_script_id`` / ``resolve_scene_id`` map an entity_id to its
    storage key, which a UI rename makes differ from the bare id. Their
    fallback to the bare id is right when the registry answers "no such
    entry", and wrong when nothing answered at all: on a write path it would
    create a new object under the guessed key instead of updating the renamed
    one. A dead transport therefore propagates (#1947).
    """

    @pytest.fixture
    def client(self) -> HomeAssistantClient:
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            c = HomeAssistantClient()
        c.base_url = "http://ha.local:8123"
        c.token = "tok"
        c.verify_ssl = True
        return c

    @pytest.mark.parametrize(
        "method, identifier",
        [("_resolve_script_id", "script.morning"), ("resolve_scene_id", "scene.movie")],
    )
    @pytest.mark.asyncio
    async def test_dead_transport_propagates(
        self, client: HomeAssistantClient, method: str, identifier: str
    ) -> None:
        client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantConnectionError("ws gone")
        )

        with pytest.raises(HomeAssistantConnectionError):
            await getattr(client, method)(identifier)

    @pytest.mark.parametrize(
        "method, identifier, expected",
        [
            ("_resolve_script_id", "script.morning", "morning"),
            ("resolve_scene_id", "scene.movie", "movie"),
        ],
    )
    @pytest.mark.asyncio
    async def test_answered_lookup_failure_still_falls_back(
        self, client: HomeAssistantClient, method: str, identifier: str, expected: str
    ) -> None:
        """An entry HA genuinely does not have is the case the fallback exists
        for, and it keeps working."""
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "not found"}
        )

        assert await getattr(client, method)(identifier) == expected


class TestDeviceEnrichmentReportsSkips:
    """Radio enrichment is best-effort, but a device answered without
    ``radio_metrics`` because the transport died must not look like a device
    whose radio reports none. The skip is named at the top level, not nested
    under ``device`` where a caller reading the payload would miss it.
    """

    @staticmethod
    def _zha_device() -> dict[str, Any]:
        return {
            "id": "dev-1",
            "name": "Kitchen sensor",
            "identifiers": [["zha", "aa:bb"]],
            "connections": [],
        }

    @pytest.mark.asyncio
    async def test_dead_transport_names_the_skipped_enrichment(self) -> None:
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantConnectionError("ws gone")
        )

        with patch(
            "ha_mcp.tools.tools_registry._get_device_info",
            return_value={
                "device_id": "dev-1",
                "integration_type": "zha",
                "ieee_address": "aa:bb",
            },
        ):
            result = await _get_single_device_result(
                client, "dev-1", None, [self._zha_device()], {}
            )

        assert result["success"] is True
        assert any("ZHA radio metrics unavailable" in w for w in result["warnings"])
        # Contract: top-level list[str], never nested under ``device``.
        assert isinstance(result["warnings"], list)
        assert "warnings" not in result["device"]

    @pytest.mark.asyncio
    async def test_healthy_enrichment_adds_no_warning(self) -> None:
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": [{"ieee": "aa:bb", "lqi": 200}]}
        )

        with patch(
            "ha_mcp.tools.tools_registry._get_device_info",
            return_value={
                "device_id": "dev-1",
                "integration_type": "zha",
                "ieee_address": "aa:bb",
            },
        ):
            result = await _get_single_device_result(
                client, "dev-1", None, [self._zha_device()], {}
            )

        assert "warnings" not in result
        assert result["device"]["radio_metrics"]["lqi"] == 200
