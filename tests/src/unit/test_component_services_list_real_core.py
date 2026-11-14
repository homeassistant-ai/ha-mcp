"""``ha_list_services`` answers the same with and without the component.

Without the component the tool reads Core's REST ``/api/services`` and
``frontend/get_translations``; with it, one ``ha_mcp_tools/services_list``
read. Both feed the same shaping, so for any filter the two answers must be
equal. The component is sent ``domain`` only: its catalog is not trimmed by
``query``, so ``domains`` still lists every domain the filter keeps. The
services and translations are Core's own, from ``counter`` and
``input_boolean`` set up in a real ``hass``.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import translation
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import websocket_api
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.tools.component_api import invalidate_caps
from ha_mcp.tools.tools_services import WS_SERVICES_LIST, ServiceDiscoveryTools

from ._client_spies import sent_commands
from ._mock_flow_integration import answer_translations


@pytest.fixture
async def services(hass: HomeAssistant) -> None:
    """Core's ``counter`` and ``input_boolean`` services and their translations."""
    for domain in ("counter", "input_boolean"):
        assert await async_setup_component(hass, domain, {})
    await hass.async_block_till_done()


async def _both_paths(
    hass: HomeAssistant, client: HomeAssistantClient, **arguments: Any
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """The tool's answer without the component, then with it, and what the
    component was sent."""
    tools = ServiceDiscoveryTools(client)
    answer_translations(hass)
    legacy = await tools.ha_list_services(**arguments)
    websocket_api.async_register_commands(hass)
    invalidate_caps(client)
    socket = await get_websocket_client(
        url=client.base_url, token=client.token, verify_ssl=client.verify_ssl
    )
    with (
        patch.object(
            client, "get_services", AsyncMock(wraps=client.get_services)
        ) as rest,
        sent_commands(socket) as sent,
    ):
        component = await tools.ha_list_services(**arguments)
    rest.assert_not_awaited()
    sent_to_component = [
        dict(c.kwargs) for c in sent.call_args_list if c.args[0] == WS_SERVICES_LIST
    ]
    return legacy, component, sent_to_component


async def test_the_unfiltered_catalog_is_the_same_on_both_paths(
    hass: HomeAssistant, core_client: HomeAssistantClient, services: None
) -> None:
    legacy, component, sent = await _both_paths(hass, core_client)

    assert component == legacy
    assert {"counter", "input_boolean"} <= set(component["domains"])
    assert sent == [{}]


async def test_a_query_is_not_sent_so_domains_stay_complete(
    hass: HomeAssistant, core_client: HomeAssistantClient, services: None
) -> None:
    """``query`` matches counter services only; ``domains`` still names
    input_boolean, as it does without the component."""
    legacy, component, sent = await _both_paths(
        hass, core_client, query="counter.increment"
    )

    assert component == legacy
    assert "input_boolean" in component["domains"]
    assert set(component["services"]) == {"counter.increment"}
    assert sent == [{}]


async def test_a_domain_filter_and_page_are_the_same_on_both_paths(
    hass: HomeAssistant, core_client: HomeAssistantClient, services: None
) -> None:
    legacy, component, sent = await _both_paths(
        hass, core_client, domain="counter", limit=1, offset=1
    )

    assert component == legacy
    assert component["total_count"] == len(hass.services.async_services()["counter"])
    assert component["count"] == 1
    assert sent == [{"domain": "counter"}]


async def test_unusable_translations_are_logged_and_the_catalog_still_listed(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    services: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A translation catalog that is not a mapping is dropped: the list comes
    back untranslated, and the log says why."""
    monkeypatch.setattr(
        translation,
        "async_get_translations",
        AsyncMock(return_value=["not", "a", "mapping"]),
    )

    with caplog.at_level(logging.WARNING):
        result = await ServiceDiscoveryTools(component_core_client).ha_list_services(
            domain="counter"
        )

    assert "counter.increment" in result["services"]
    assert any(
        record.levelno == logging.WARNING
        and "expected a Mapping, got list" in record.getMessage()
        for record in caplog.records
    )
