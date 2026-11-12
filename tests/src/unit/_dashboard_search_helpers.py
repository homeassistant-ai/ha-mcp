"""Search a dashboard config the way an agent does, on a real ``hass``.

The config is saved as a storage dashboard in Core and searched with
``ha_config_get_dashboard``, which reads it back through Core's own
``lovelace/config``.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools

# Core requires a hyphen in a dashboard's url_path.
SEARCH_URL_PATH = "card-search"


async def store_dashboard(
    hass: HomeAssistant, client: HomeAssistantClient, config: dict[str, Any]
) -> None:
    """Save ``config`` as the storage dashboard ``SEARCH_URL_PATH``."""
    assert await async_setup_component(hass, "lovelace", {})
    listed = await client.send_websocket_message({"type": "lovelace/dashboards/list"})
    if not any(row["url_path"] == SEARCH_URL_PATH for row in listed["result"]):
        created = await client.send_websocket_message(
            {
                "type": "lovelace/dashboards/create",
                "url_path": SEARCH_URL_PATH,
                "title": "Card search",
            }
        )
        assert created["success"], created
    saved = await client.send_websocket_message(
        {"type": "lovelace/config/save", "url_path": SEARCH_URL_PATH, "config": config}
    )
    assert saved["success"], saved


async def search_cards(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    config: dict[str, Any],
    *,
    entity_id: str | None = None,
    card_type: str | None = None,
    heading: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Store ``config`` and search it; each match carries its card config."""
    await store_dashboard(hass, client, config)
    result = await DashboardConfigTools(client).ha_config_get_dashboard(
        url_path=SEARCH_URL_PATH,
        include_config=True,
        entity_id=entity_id,
        card_type=card_type,
        heading=heading,
        query=query,
    )
    assert isinstance(result, dict), result
    return result
