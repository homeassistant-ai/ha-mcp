"""Degrading on a missing registry is fine; degrading silently is not.

Since #1947 ``send_websocket_message`` raises on a dead transport instead of
returning a failure envelope. The enrichment call sites deliberately keep
serving a partial answer rather than failing the whole call, but a thin
result must say what was skipped and why, otherwise "the area registry never
arrived" is indistinguishable from "there are no areas".

Every read is answered by Core. A read that gets no answer is one Core drops
or holds (``_command_faults``), or a client whose WebSocket send raises.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.components.frontend import websocket_get_translations
from homeassistant.components.websocket_api import async_register_command
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import dump

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import automation_registry_details
from ha_mcp.tools.config_write_helpers import (
    fetch_entity_category,
    fetch_registry_entry,
)
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools
from ha_mcp.tools.tools_search import SearchTools
from ha_mcp.tools.tools_service import ServiceTools
from ha_mcp.tools.tools_services import ServiceDiscoveryTools
from ha_mcp.tools.util_helpers import get_logger_levels

from ._command_faults import dropped_on, pooled_socket, unanswered
from ._config_dir_yaml import setup_from_configuration, write_config_files
from ._tool_errors import error_of

AREA_LIST = "config/area_registry/list"
FLOOR_LIST = "config/floor_registry/list"


@pytest.fixture
async def registries(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """Core's ``config`` commands, which answer the registry reads."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def smart(core_client: HomeAssistantClient) -> SmartSearchTools:
    return SmartSearchTools(client=core_client)


@pytest.fixture
def dead_socket(core_client: HomeAssistantClient) -> Iterator[None]:
    """Every WebSocket read of ``core_client`` gets no answer; REST still works."""
    with patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("ws gone")),
    ):
        yield


@pytest.fixture
def basement_area(registries: None, area_registry: ar.AreaRegistry) -> None:
    """An area whose name only close-spelling matching finds for 'sous-sol'."""
    area_registry.async_create("Couloir Sous-Sol")


class TestOverviewNamesSkippedRegistries:
    async def test_dropped_area_registry_warns_and_marks_partial(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, registries: None
    ) -> None:
        """An overview whose area stats are thin because the registry never
        arrived is incomplete, not merely annotated."""
        socket = await pooled_socket(core_client)

        with dropped_on(hass, AREA_LIST, socket):
            response = await SmartSearchTools(client=core_client).get_system_overview()

        assert response["success"] is True
        assert any(
            w.startswith("area registry unavailable: ") for w in response["warnings"]
        )
        assert response["partial"] is True

    async def test_registry_core_refuses_is_named_with_cores_reason(
        self, smart: SmartSearchTools
    ) -> None:
        """Without the ``config`` integration Core refuses the registry reads."""
        response = await smart.get_system_overview()

        [area_warning] = [
            w for w in response["warnings"] if w.startswith("area registry unavailable")
        ]
        assert "Unknown command" in area_warning
        assert response["partial"] is True

    async def test_answered_registries_add_no_warning(
        self, smart: SmartSearchTools, registries: None, area_registry: ar.AreaRegistry
    ) -> None:
        area_registry.async_create("Kitchen")

        response = await smart.get_system_overview()

        assert response["system_summary"]["total_areas"] == 1
        assert "partial" not in response
        assert not any("unavailable" in w for w in response.get("warnings", []))


class TestAreaSearchNamesSkippedRegistries:
    async def test_dropped_area_registry_warns_instead_of_bare_no_match(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, basement_area: None
    ) -> None:
        """'No areas found' while the area registry is unreachable reads as
        "this area does not exist"."""
        socket = await pooled_socket(core_client)

        with dropped_on(hass, AREA_LIST, socket):
            response = await SmartSearchTools(client=core_client).get_entities_by_area(
                "Couloir Sous-Sol"
            )

        assert response["total_areas_found"] == 0
        assert any(
            w.startswith("area registry unavailable: ") for w in response["warnings"]
        )

    async def test_unanswered_floor_registry_does_not_fuzzy_match_an_area(
        self,
        hass: HomeAssistant,
        smart: SmartSearchTools,
        basement_area: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A floor-registry outage must not restore floor-to-area
        misresolution, and the answer says what the outage cost the query."""
        async with unanswered(hass, FLOOR_LIST, monkeypatch):
            response = await smart.get_entities_by_area("sous-sol")

        assert response["total_areas_found"] == 0
        assert [a["area_id"] for a in response["available_areas"]] == [
            "couloir_sous_sol"
        ]
        assert any(
            w.startswith("floor registry unavailable: ") for w in response["warnings"]
        )
        assert any(
            "close-spelling matching was skipped" in w and "'sous-sol'" in w
            for w in response["warnings"]
        ), response["warnings"]

    async def test_answered_floor_registry_lets_close_spelling_find_the_area(
        self, smart: SmartSearchTools, basement_area: None
    ) -> None:
        response = await smart.get_entities_by_area("sous-sol")

        assert list(response["areas"]) == ["couloir_sous_sol"]
        assert not any("unavailable" in w for w in response.get("warnings", []))


class TestIdentifierResolversFailLoud:
    """A script or scene entity_id maps to its storage key through the entity
    registry, and a UI rename makes the two differ. Falling back to the bare id
    is right when Core answers "no such entry", and wrong when nothing answered:
    on a write path it would create a new object under the guessed key instead
    of updating the renamed one. A dead transport therefore propagates (#1947).
    """

    @pytest.mark.parametrize(
        ("method", "identifier"),
        [("get_script_config", "script.morning"), ("resolve_scene_id", "scene.movie")],
    )
    async def test_dead_transport_propagates(
        self,
        core_client: HomeAssistantClient,
        dead_socket: None,
        method: str,
        identifier: str,
    ) -> None:
        with pytest.raises(HomeAssistantConnectionError):
            await getattr(core_client, method)(identifier)

    async def test_scene_core_does_not_know_resolves_to_its_bare_id(
        self, core_client: HomeAssistantClient, registries: None
    ) -> None:
        assert await core_client.resolve_scene_id("scene.movie") == "movie"

    async def test_script_core_does_not_know_is_read_under_its_bare_id(
        self, core_client: HomeAssistantClient, registries: None
    ) -> None:
        with pytest.raises(HomeAssistantAPIError) as exc:
            await core_client.get_script_config("script.morning")

        assert exc.value.status_code == 404
        assert "resolved storage key: morning" in str(exc.value)


class TestEmptyResultVersusUnaskedQuestion:
    """Helpers whose "nothing found" value is indistinguishable from "could not
    ask" record the difference, so a caller can stop reporting a value it
    never retrieved."""

    async def test_logger_levels_records_the_failed_fetch(
        self, core_client: HomeAssistantClient, dead_socket: None
    ) -> None:
        warnings: list[str] = []

        assert await get_logger_levels(core_client, warnings) == {}
        assert warnings == ["log levels unavailable: ws gone"]

    async def test_logger_levels_stays_quiet_when_core_answers(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        assert await async_setup_component(hass, "logger", {})
        warnings: list[str] = []

        await get_logger_levels(core_client, warnings)

        assert warnings == []

    async def test_dropped_notifications_read_is_named_in_the_overview(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        smart: SmartSearchTools,
    ) -> None:
        """The count is pre-seeded to 0, so a failed read would otherwise
        assert "0 notifications" about something nobody could read."""
        assert await async_setup_component(hass, persistent_notification.DOMAIN, {})
        persistent_notification.async_create(hass, "Disk almost full")
        socket = await pooled_socket(core_client)

        with dropped_on(hass, "persistent_notification/get", socket):
            result = await SearchTools(core_client, smart).ha_get_overview(
                fields=["notification_count"]
            )

        assert result["notification_count"] == 0
        assert [w.split(":")[0] for w in result["warnings"]] == [
            "notifications unavailable"
        ]


class TestEnrichmentAbsenceIsNotAnAnswer:
    """Both enrich a payload whose primary data comes over REST, so a live
    REST plus a dead socket returns a complete-looking response that is
    quietly missing a field."""

    async def test_category_fetch_failure_is_recorded(
        self, core_client: HomeAssistantClient, dead_socket: None
    ) -> None:
        """``None`` is also what "no category assigned" looks like."""
        warnings: list[str] = []

        assert (
            await fetch_entity_category(
                core_client, "script.morning", "script", warnings
            )
            is None
        )
        assert warnings == ["category unavailable for script.morning: ws gone"]

    async def test_category_absent_without_failure_stays_quiet(
        self,
        core_client: HomeAssistantClient,
        registries: None,
        entity_registry: er.EntityRegistry,
    ) -> None:
        entry = entity_registry.async_get_or_create("script", "script", "morning")
        warnings: list[str] = []

        assert (
            await fetch_entity_category(
                core_client, entry.entity_id, "script", warnings
            )
            is None
        )
        assert warnings == []

    async def test_dropped_translations_read_is_named_in_the_service_list(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """An empty translation map is also what an instance with no
        translations returns; the catalog itself still arrives over REST."""
        assert await async_setup_component(hass, "counter", {})
        # The frontend's web assets are not installed; its command is Core's.
        async_register_command(hass, websocket_get_translations)
        socket = await pooled_socket(core_client)

        with dropped_on(hass, "frontend/get_translations", socket):
            out = await ServiceDiscoveryTools(core_client).ha_list_services(
                domain="counter"
            )

        assert "counter.set_value" in out["services"]
        assert any(
            w.startswith("service descriptions unavailable: ") for w in out["warnings"]
        ), out.get("warnings")

    async def test_script_get_surfaces_a_dropped_category_read(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At the seam: the tool response, not the helper. The connection
        drops as the registry row the category comes from is read, after the
        config arrived."""
        body = {"alias": "Morning", "sequence": [{"delay": {"seconds": 1}}]}
        await write_config_files(
            hass,
            {
                "configuration.yaml": "script: !include scripts.yaml\n",
                "scripts.yaml": dump({"morning": body}),
            },
        )
        await setup_from_configuration(hass, "script")
        socket = await pooled_socket(core_client)

        async def registry_read_dropped(*args: Any) -> dict[str, Any] | None:
            with dropped_on(hass, "config/entity_registry/get", socket):
                return await fetch_registry_entry(*args)

        monkeypatch.setattr(
            automation_registry_details, "fetch_registry_entry", registry_read_dropped
        )

        result = await ConfigScriptTools(core_client).ha_config_get_script(
            script_id="morning"
        )

        assert result["config"] == body
        assert "category" not in result["config"]
        assert "registry entry unavailable for script.morning" in [
            w.split(":")[0] for w in result["warnings"]
        ]


async def test_ws_command_without_an_answer_is_a_structured_connection_error(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """``ha_call_service(ws_command=...)`` returns before the service path's
    own error handling, so the dropped connection needs a handler of its own
    or it escapes the tool unstructured."""
    assert await async_setup_component(hass, "repairs", {})
    socket = await pooled_socket(core_client)
    tools = ServiceTools(core_client, DeviceControlTools(core_client))

    with (
        dropped_on(hass, "repairs/list_issues", socket),
        pytest.raises(ToolError) as exc,
    ):
        await tools.ha_call_service(ws_command="repairs/list_issues")

    assert error_of(exc)["code"] == "CONNECTION_FAILED"
