"""Update discovery must not offer Install when Core does not support it."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.update import UpdateEntityFeature

from ha_mcp import update_check
from ha_mcp.tools.tools_updates import UpdateTools

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ha_mcp.client.rest_client import HomeAssistantClient


@pytest.mark.unit
@pytest.mark.parametrize(
    ("features", "in_progress", "expected"),
    [
        (
            UpdateEntityFeature.RELEASE_NOTES | UpdateEntityFeature.PROGRESS,
            False,
            False,
        ),
        (UpdateEntityFeature.INSTALL, False, True),
        (UpdateEntityFeature.INSTALL, True, False),
    ],
)
async def test_list_and_details_respect_native_install_capability(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    features: UpdateEntityFeature,
    in_progress: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr(update_check, "get_update_field", AsyncMock(return_value=None))
    entity_id = "update.example_source"
    hass.states.async_set(
        entity_id,
        "on",
        {
            "installed_version": "1.0.0",
            "latest_version": "1.0.1",
            "supported_features": int(features),
            "in_progress": in_progress,
        },
    )
    tools = UpdateTools(core_client)
    details = await tools.ha_manage_updates(action="get", entity_ids=entity_id)
    listing = await tools.ha_manage_updates(action="list")
    listed = next(item for item in listing["updates"] if item["entity_id"] == entity_id)
    assert details["can_install"] is expected
    assert listed["can_install"] is expected
