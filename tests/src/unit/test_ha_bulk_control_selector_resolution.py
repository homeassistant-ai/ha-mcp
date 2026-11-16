"""``ha_bulk_control`` selector mode on a real ``hass``.

The selector is resolved from Core's own area and entity registries and states
into a frozen set of leaf lights, which is then switched through the device
tools, or only previewed with ``dry_run``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.bulk_selector import MAX_SELECTOR_ENTITIES, InfrastructureErrorCause
from ha_mcp.tools.component_api import get_component_caps
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.tools_service import INFRASTRUCTURE_ERROR_SUGGESTIONS, ServiceTools
from ha_mcp.utils.data_paths import get_data_dir

from ._command_faults import dropped_on, pooled_socket
from ._ha_bulk_control_selector_core import (
    SALON,
    SALON_BUT_VITRINE,
    bulk_tools,
    furnish_salon,
    switched,
)
from ._switched_states import SwitchedStates


@pytest.fixture
async def salon(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> SwitchedStates:
    return await furnish_salon(hass, area_registry, entity_registry)


async def test_a_dry_run_previews_the_resolved_lights_without_switching_any(
    core_client: HomeAssistantClient, salon: SwitchedStates
) -> None:
    result = await bulk_tools(core_client).ha_bulk_control(
        selector=SALON_BUT_VITRINE, action="off", dry_run=True
    )

    assert (result["dry_run"], result["dispatched"]) == (True, False)
    assert result["resolution"]["resolved_entity_ids"] == ["light.sofa"]
    assert salon.calls == []


async def test_a_hidden_light_is_reported_in_the_top_level_warnings(
    core_client: HomeAssistantClient, salon: SwitchedStates
) -> None:
    """``warnings`` is top-level (.gemini/styleguide.md): a caller reading only
    that key must still learn a matching light was left out."""
    result = await bulk_tools(core_client).ha_bulk_control(
        selector=SALON, action="off", dry_run=True
    )

    (warning,) = result["warnings"]
    assert "1 matching entity was hidden" in warning
    assert "warnings" not in result["resolution"]


async def test_dispatch_warnings_are_kept_beside_the_resolution_warnings(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    salon: SwitchedStates,
) -> None:
    """The socket drops once the component has the batch: the dispatch's own
    warning and the hidden-light one both reach the caller."""
    assert await get_component_caps(component_core_client) is not None
    socket = await pooled_socket(component_core_client)

    with dropped_on(hass, "ha_mcp_tools/bulk_call_service", socket):
        result = await bulk_tools(component_core_client).ha_bulk_control(
            selector=SALON_BUT_VITRINE, action="off"
        )

    dispatch_warning, resolution_warning = result["warnings"]
    assert "no usable confirmation arrived" in dispatch_warning
    assert "1 matching entity was hidden" in resolution_warning
    assert "warnings" not in result["resolution"]


async def test_only_the_resolved_leaves_are_switched(
    core_client: HomeAssistantClient, salon: SwitchedStates
) -> None:
    """The group, the excluded light and the hidden one are never switched."""
    device_tools = DeviceControlTools(core_client)
    tools = ServiceTools(core_client, device_tools)

    with patch.object(
        device_tools, "bulk_device_control", wraps=device_tools.bulk_device_control
    ) as bulk:
        result = await tools.ha_bulk_control(
            selector=SALON_BUT_VITRINE, action="off", parallel=False
        )

    assert switched(salon) == [("turn_off", "light.sofa")]
    bulk.assert_awaited_once_with(
        operations=[
            {"entity_id": "light.sofa", "action": "off", "validate_first": True}
        ],
        parallel=False,
        ctx=None,
    )
    assert result["resolution"]["excluded_entity_ids"] == ["light.vitrine"]


async def test_a_batch_the_device_tools_refuse_keeps_their_own_error(
    core_client: HomeAssistantClient, salon: SwitchedStates
) -> None:
    """A negative timeout (the tool schema refuses it; a Python caller can pass
    it) fails every row: the device tools' error is not re-classified into a
    generic one that loses its message and per-row details."""
    with pytest.raises(ToolError) as exc_info:
        await bulk_tools(core_client).ha_bulk_control(
            selector=SALON_BUT_VITRINE, action="off", timeout_seconds=-1.0
        )

    body = json.loads(str(exc_info.value))
    assert "failed validation; nothing was dispatched" in body["error"]["message"]
    assert "skipped_details" in body
    assert salon.calls == []


@pytest.mark.usefixtures("salon")
async def test_selector_mode_without_an_action_names_the_missing_action(
    core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError, match="Selector mode requires action"):
        await bulk_tools(core_client).ha_bulk_control(selector=SALON)


def test_docstring_entity_limit_matches_the_constant() -> None:
    """The docstring states the limit in prose (a docstring cannot be an
    f-string), so nothing else keeps it in step with the constant."""
    assert ServiceTools.ha_bulk_control.__doc__ is not None
    assert (
        f"more than {MAX_SELECTOR_ENTITIES} entities"
        in ServiceTools.ha_bulk_control.__doc__
    )


async def test_home_assistant_out_of_reach_is_a_connection_failure(
    unreachable_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError, match="CONNECTION_FAILED"):
        await bulk_tools(unreachable_client).ha_bulk_control(
            selector=SALON, action="off"
        )


async def test_registries_core_cannot_serve_do_not_blame_the_selector(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without Core's ``config`` integration the registry commands are unknown:
    an agent told "selector is invalid" would rewrite a selector that was fine."""
    hass.states.async_set("light.sofa", "on")

    with pytest.raises(ToolError) as exc_info:
        await bulk_tools(core_client).ha_bulk_control(selector=SALON, action="off")

    body = str(exc_info.value)
    assert "CONNECTION_FAILED" in body
    assert "VALIDATION_FAILED" not in body


async def test_a_corrupt_visibility_config_points_at_the_settings_not_the_network(
    core_client: HomeAssistantClient, salon: SwitchedStates
) -> None:
    (get_data_dir() / "entity_visibility.json").write_text("{", encoding="utf-8")

    with pytest.raises(ToolError) as exc_info:
        await bulk_tools(core_client).ha_bulk_control(selector=SALON, action="off")

    body = str(exc_info.value)
    assert "entity visibility" in body.lower()
    assert "HOMEASSISTANT_URL" not in body
    assert "network connectivity" not in body.lower()
    assert salon.calls == []


def test_every_infrastructure_cause_has_its_own_suggestions() -> None:
    """A cause missing from the table would silently fall back to the
    connectivity suggestions."""
    assert set(INFRASTRUCTURE_ERROR_SUGGESTIONS) == set(InfrastructureErrorCause)
