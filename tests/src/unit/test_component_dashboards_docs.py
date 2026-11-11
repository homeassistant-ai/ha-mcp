"""The component's ``dashboards`` ``docs`` mode and its card walk's reach.

``docs`` hands the server every storage dashboard's config for the card search
(issue #2694). Servers predating the ``dashboards_docs`` capability still read
``search`` matches, so that walk must reach cards nested under a custom card's
own keys too.

The command runs on a real ``hass`` with Core's lovelace integration; the
dashboards are created and saved through Core's own ``lovelace/*`` commands.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import call, patch

from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import MockHAClientWebSocket

from ._component_core import ws_result

_WS = "ha_mcp_tools/dashboards"
_HOME = "dash-home"
_BODY = {"views": [{"cards": [{"type": "tile", "entity": "light.a"}]}]}
# Deeper than the walk's node and card bounds, and within what Core's JSON
# storage writes (orjson refuses nesting deeper than 254).
_DEEP = 200


async def _lovelace(hass: HomeAssistant, **config: Any) -> None:
    assert await async_setup_component(hass, "lovelace", {"lovelace": config})


async def _storage_dash(ws: MockHAClientWebSocket, url_path: str, body: Any) -> None:
    """A storage dashboard whose saved config is ``body``."""
    await ws_result(
        ws,
        "lovelace/dashboards/create",
        url_path=url_path,
        title=url_path,
        mode="storage",
    )
    await ws_result(ws, "lovelace/config/save", url_path=url_path, config=body)


async def test_docs_returns_storage_configs_only(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """YAML bodies (including a YAML default dashboard) never leave the component."""
    # ``mode: yaml`` makes the default "lovelace" dashboard a YAML one.
    await _lovelace(
        hass,
        mode="yaml",
        dashboards={
            "yaml-dash": {"mode": "yaml", "filename": "yaml-dash.yaml", "title": "Y"}
        },
    )
    await _storage_dash(component_ws, _HOME, _BODY)

    result = await ws_result(component_ws, _WS, mode="docs")

    assert result["docs"] == [{"url_path": _HOME, "config": _BODY}]
    assert result["load_failed"] == 0


async def test_docs_counts_dashboards_that_fail_to_load(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _lovelace(hass)
    await _storage_dash(component_ws, _HOME, _BODY)
    # Core stores a string config as given; its load is not a dashboard config.
    await _storage_dash(component_ws, "dash-broken", "boom")

    result = await ws_result(component_ws, _WS, mode="docs")

    assert [doc["url_path"] for doc in result["docs"]] == [_HOME]
    assert result["load_failed"] == 1


async def test_docs_without_lovelace_is_unavailable(
    component_ws: MockHAClientWebSocket,
) -> None:
    result = await ws_result(component_ws, _WS, mode="docs")

    assert result == {"mode": "docs", "available": False, "docs": []}


async def test_search_reaches_cards_under_a_custom_cards_own_keys(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """``search`` matches (read by older servers) reach nested custom-card cards."""
    body = {
        "views": [
            {
                "cards": [
                    {
                        "type": "custom:astra-board-page",
                        "groups": [
                            {"cards": [{"card": {"type": "tile", "entity": "light.x"}}]}
                        ],
                    },
                    {
                        "type": "custom:button-card",
                        "custom_fields": {
                            "my-field": {"type": "tile", "entity": "light.x"}
                        },
                    },
                    {
                        "type": "conditional",
                        "card": [{"type": "tile", "entity": "light.x"}],
                    },
                ]
            }
        ]
    }
    await _lovelace(hass)
    await _storage_dash(component_ws, _HOME, body)

    result = await ws_result(component_ws, _WS, mode="search", query="light.x")

    assert [(m["card_path"], m["card_type"]) for m in result["matches"]] == [
        ("views[0].cards[0].groups[0].cards[0].card", "tile"),
        ('views[0].cards[1].custom_fields["my-field"]', "tile"),
        ("views[0].cards[2].card[0]", "tile"),
    ]


async def test_docs_reads_fresh_config_not_the_cache(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The server's search promises fresh config, so ``docs`` bypasses the cache."""
    await _lovelace(hass)
    await _storage_dash(component_ws, _HOME, _BODY)
    dash = hass.data[LOVELACE_DATA].dashboards[_HOME]

    with patch.object(dash, "async_load", wraps=dash.async_load) as load:
        await ws_result(component_ws, _WS, mode="docs")

    assert load.call_args_list == [call(True)]


async def test_search_survives_deeply_nested_card_options(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    options: dict[str, Any] = {"entity": "light.deep"}
    for _ in range(_DEEP):
        options = {"n": options}
    body = {
        "views": [
            {
                "cards": [
                    {"type": "custom:x", "options": options},
                    {"type": "tile", "entity": "light.a"},
                ]
            }
        ]
    }
    await _lovelace(hass)
    await _storage_dash(component_ws, _HOME, body)

    result = await ws_result(component_ws, _WS, mode="search", query="light.a")
    deep = await ws_result(component_ws, _WS, mode="search", query="light.deep")

    assert [m["card_path"] for m in result["matches"]] == ["views[0].cards[1]"]
    # The walk stops at its bound, so the entity below it is not read.
    assert deep["matches"] == []


async def test_search_survives_a_deep_chain_of_nested_cards(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    card: dict[str, Any] = {"type": "tile", "entity": "light.deep"}
    for _ in range(_DEEP):
        card = {"type": "custom:wrap", "card": card}
    body = {"views": [{"cards": [card, {"type": "tile", "entity": "light.a"}]}]}
    await _lovelace(hass)
    await _storage_dash(component_ws, _HOME, body)

    result = await ws_result(component_ws, _WS, mode="search", query="light.a")
    deep = await ws_result(component_ws, _WS, mode="search", query="light.deep")

    assert [m["card_path"] for m in result["matches"]] == ["views[0].cards[1]"]
    # The walk stops at its bound, so the entity below it is not read.
    assert deep["matches"] == []


async def test_search_query_matches_number_options(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    body = {"views": [{"cards": [{"type": "grid", "columns": 3}]}]}
    await _lovelace(hass)
    await _storage_dash(component_ws, _HOME, body)

    result = await ws_result(component_ws, _WS, mode="search", query="3")

    assert [(m["card_path"], m["matched_field"]) for m in result["matches"]] == [
        ("views[0].cards[0]", "columns")
    ]


async def test_docs_capability_is_advertised(
    component_ws: MockHAClientWebSocket,
) -> None:
    """The component advertises ``dashboards_docs``, which gates the server's ``docs`` read."""
    info = await ws_result(component_ws, "ha_mcp_tools/info")
    assert "dashboards_docs" in info["capabilities"]
