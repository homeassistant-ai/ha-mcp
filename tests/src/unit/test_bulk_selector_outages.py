"""A Home Assistant that cannot give the selector its topology.

These are infrastructure failures, not the caller's selector to fix, so they
must never be reported as an invalid selector.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.bulk_selector import BulkSelectorInfrastructureError
from ha_mcp.utils.data_paths import get_data_dir
from ha_mcp.visibility.persistence import VISIBILITY_FILENAME

from ._bulk_selector_core import add_entity, resolve
from ._bulk_selector_core import salon as salon


async def test_topology_fetch_failure_propagates_while_other_fetches_answer(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    salon: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The states read gets no answer while the four registry reads succeed:
    that one failure surfaces instead of vanishing behind the others."""
    add_entity(hass, "light.one", area_id=salon)
    monkeypatch.setattr(
        core_client,
        "get_states",
        AsyncMock(side_effect=HomeAssistantConnectionError("states read dropped")),
    )

    with pytest.raises(HomeAssistantConnectionError, match="states read dropped"):
        await resolve(core_client, {"domain": "light", "area_ids": [salon]})


async def test_topology_all_five_failures_are_each_logged_by_their_own_label(
    unreachable_client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
) -> None:
    """With Home Assistant unreachable every fetch fails, and each failure is
    logged under the name of what was being read, not only the one raised."""
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(HomeAssistantConnectionError),
    ):
        await resolve(unreachable_client, {"domain": "light", "area_ids": ["salon"]})

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    for label in (
        "states",
        "entity registry",
        "device registry",
        "area registry",
        "floor registry",
    ):
        assert any(f"bulk selector: {label} fetch failed" in m for m in warnings), (
            f"missing WARNING for {label!r}: {warnings}"
        )


async def test_unloadable_visibility_config_is_infrastructure_not_validation(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """A corrupt visibility config fails closed: nothing is selected, the
    error names the config, and its cause routes the user to the settings,
    not to their selector or their network."""
    (get_data_dir() / VISIBILITY_FILENAME).write_text("{not json", encoding="utf-8")
    add_entity(hass, "light.one", area_id=salon)

    with pytest.raises(
        BulkSelectorInfrastructureError,
        match="entity visibility config could not be loaded",
    ) as exc_info:
        await resolve(core_client, {"domain": "light", "area_ids": [salon]})
    assert exc_info.value.cause == "visibility_config"


async def test_registry_unavailable_defaults_to_connectivity_cause(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core without the ``config`` integration refuses the registry lists:
    the error's cause is connectivity, not the selector."""
    hass.states.async_set("light.one", "on")

    with pytest.raises(
        BulkSelectorInfrastructureError, match="entity registry is unavailable"
    ) as exc_info:
        await resolve(core_client, {"domain": "light", "area_ids": ["salon"]})
    assert exc_info.value.cause == "connectivity"
