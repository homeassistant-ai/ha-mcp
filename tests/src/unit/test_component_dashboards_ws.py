"""The component's ``ha_mcp_tools/dashboards`` command on a real ``hass``.

``list`` mirrors Core's ``lovelace/dashboards/list`` rows, ``get`` returns a
storage dashboard's body (never a YAML one's: Core resolves ``!secret`` when it
loads one), and ``search`` walks every storage dashboard's cards and whole
document. Dashboards are Core's lovelace integration's, created and saved
through its own ``lovelace/*`` commands; a broken one is a store Core loads.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import MockHAClientWebSocket

from ._component_core import ws_call, ws_result

_WS = "ha_mcp_tools/dashboards"
_HOME_BODY = {
    "title": "Home",
    "views": [
        {
            "title": "Living",
            "cards": [
                {"type": "entities", "entities": ["light.kitchen", "light.hall"]},
                {
                    "type": "vertical-stack",
                    "cards": [{"type": "camera", "camera_image": "camera.front_door"}],
                },
            ],
            "sections": [{"cards": [{"type": "markdown", "content": "kitchen notes"}]}],
        }
    ],
}


async def _lovelace(hass: HomeAssistant, **config: Any) -> None:
    assert await async_setup_component(hass, "lovelace", {"lovelace": config})


async def _dashboard(
    ws: MockHAClientWebSocket,
    url_path: str,
    *,
    title: str | None = None,
    body: Any = None,
    icon: str = "mdi:home",
) -> None:
    """A storage dashboard; ``body`` is its saved config, if any."""
    await ws_result(
        ws,
        "lovelace/dashboards/create",
        url_path=url_path,
        title=title or url_path,
        icon=icon,
        mode="storage",
    )
    if body is not None:
        await ws_result(ws, "lovelace/config/save", url_path=url_path, config=body)


def _yaml_dashboards() -> dict[str, Any]:
    return {"yaml-dash": {"mode": "yaml", "filename": "yaml-dash.yaml", "title": "Y"}}


async def test_the_new_commands_capabilities_are_advertised(
    component_ws: MockHAClientWebSocket,
) -> None:
    info = await ws_result(component_ws, "ha_mcp_tools/info")

    assert {
        "dashboards",
        "dashboards_doc_search",
        "services_list",
        "reference_data",
        "search_visibility",
        "search_visibility_allowlist_authorization",
        "server_entry",
    } <= set(info["capabilities"])


# --- list -------------------------------------------------------------------------


async def test_the_list_is_cores_list_with_every_row_tagged_with_its_mode(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The server excludes YAML dashboards by the tag; the default dashboard has
    no row, as in Core's list."""
    await _lovelace(hass, dashboards=_yaml_dashboards())
    await _dashboard(component_ws, "dash-home", title="Home")
    await _dashboard(component_ws, "dash-energy", title="Energy", icon="mdi:flash")
    await ws_result(component_ws, "lovelace/config/save", config=_HOME_BODY)

    cores = await ws_result(component_ws, "lovelace/dashboards/list")
    result = await ws_result(component_ws, _WS, mode="list")

    assert result["available"] is True
    assert result["dashboards"] == cores
    modes = {row["url_path"]: row["mode"] for row in result["dashboards"]}
    assert modes == {
        "yaml-dash": "yaml",
        "dash-home": "storage",
        "dash-energy": "storage",
    }


@pytest.mark.parametrize("mode", ["list", "get"])
async def test_without_lovelace_nothing_is_available(
    component_ws: MockHAClientWebSocket, mode: str
) -> None:
    result = await ws_result(component_ws, _WS, mode=mode)

    assert result["available"] is False
    assert result.get("dashboards", []) == []


async def test_a_frame_without_a_mode_is_a_list(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _lovelace(hass)

    assert (await ws_result(component_ws, _WS))["mode"] == "list"


@pytest.mark.parametrize(
    "bad", [{"mode": "bogus"}, {"url_path": 5}], ids=["mode", "url_path"]
)
async def test_a_malformed_frame_is_refused(
    component_ws: MockHAClientWebSocket, bad: dict[str, Any]
) -> None:
    reply = await ws_call(component_ws, _WS, **bad)

    assert reply["error"]["code"] == "invalid_format"


# --- get --------------------------------------------------------------------------


@pytest.mark.parametrize("url_path", ["dash-home", None], ids=["named", "default"])
async def test_a_get_returns_the_stored_body(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, url_path: str | None
) -> None:
    """No url_path, or None, is the default dashboard."""
    await _lovelace(hass)
    if url_path is None:
        await ws_result(component_ws, "lovelace/config/save", config=_HOME_BODY)
    else:
        await _dashboard(component_ws, url_path, body=_HOME_BODY)

    result = await ws_result(component_ws, _WS, mode="get", url_path=url_path)

    assert (result["status"], result["url_path"], result["config"]) == (
        "ok",
        url_path,
        _HOME_BODY,
    )


async def test_a_yaml_dashboards_body_never_leaves_the_component(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _write(
        hass,
        "yaml-dash.yaml",
        "views:\n  - cards:\n      - type: markdown\n        content: secret\n",
    )
    await _lovelace(hass, dashboards=_yaml_dashboards())

    result = await ws_result(component_ws, _WS, mode="get", url_path="yaml-dash")

    assert (result["status"], result["config"]) == ("yaml_excluded", None)


async def _write(hass: HomeAssistant, name: str, text: str) -> None:
    path = hass.config.path(name)

    def write() -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    await hass.async_add_executor_job(write)


async def _broken(
    hass: HomeAssistant,
    ws: MockHAClientWebSocket,
    hass_storage: dict[str, Any],
    url_path: str,
) -> None:
    """A storage dashboard whose stored config Core cannot load."""
    await _dashboard(ws, url_path)
    key = f"lovelace.{url_path.replace('-', '_')}"
    hass_storage[key] = {"version": 1, "key": key, "data": {"views": []}}


@pytest.mark.parametrize(
    "url_path", ["dash-nope", "dash-broken"], ids=["missing", "unloadable"]
)
async def test_a_dashboard_it_cannot_load_is_not_found(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    hass_storage: dict[str, Any],
    url_path: str,
) -> None:
    await _lovelace(hass)
    await _broken(hass, component_ws, hass_storage, "dash-broken")

    result = await ws_result(component_ws, _WS, mode="get", url_path=url_path)

    assert (result["status"], result["config"]) == ("not_found", None)


# --- search -----------------------------------------------------------------------


async def _search(ws: MockHAClientWebSocket, query: str) -> dict[str, Any]:
    result: dict[str, Any] = await ws_result(ws, _WS, mode="search", query=query)
    return result


@pytest.fixture
async def home(hass: HomeAssistant, component_ws: MockHAClientWebSocket) -> None:
    await _lovelace(hass, dashboards=_yaml_dashboards())
    await _write(
        hass,
        "yaml-dash.yaml",
        "views:\n  - cards:\n      - type: entities\n        entities: [light.kitchen]\n",
    )
    await _dashboard(component_ws, "dash-home", title="Home", body=_HOME_BODY)


@pytest.mark.usefixtures("home")
async def test_a_search_names_the_card_its_view_and_its_path(
    component_ws: MockHAClientWebSocket,
) -> None:
    result = await _search(component_ws, "light.kitchen")

    assert result["truncated"] is False
    # Never the YAML dashboard, which names the same light.
    assert {m["url_path"] for m in result["matches"]} == {"dash-home"}
    match = next(m for m in result["matches"] if m["matched_value"] == "light.kitchen")
    assert (match["view_index"], match["view_title"]) == (0, "Living")
    assert (match["card_type"], match["matched_field"]) == ("entities", "entities")
    assert match["card_path"] == "views[0].cards[0]"


@pytest.mark.usefixtures("home")
@pytest.mark.parametrize(
    ("query", "field", "card_type", "card_path"),
    [
        ("camera.front_door", "camera_image", "camera", "views[0].cards[1].cards[0]"),
        ("kitchen notes", "content", "markdown", "views[0].sections[0].cards[0]"),
    ],
    ids=["nested-card", "section-card"],
)
async def test_a_nested_or_section_card_is_its_own_match(
    component_ws: MockHAClientWebSocket,
    query: str,
    field: str,
    card_type: str,
    card_path: str,
) -> None:
    result = await _search(component_ws, query)

    match = next(m for m in result["matches"] if m["matched_field"] == field)
    assert (match["card_type"], match["card_path"]) == (card_type, card_path)


async def test_view_badges_and_a_sections_header_card_are_searched(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The card walk never visits either; both name entities or text."""
    await _lovelace(hass)
    body = {
        "views": [
            {
                "title": "Living",
                "badges": [
                    "sensor.badge_temp",
                    {"type": "entity", "entity": "sensor.badge_hum"},
                ],
                "cards": [],
            },
            {
                "type": "sections",
                "header": {"card": {"type": "markdown", "content": "welcome banner"}},
                "sections": [],
            },
        ]
    }
    await _dashboard(component_ws, "dash-badges", body=body)

    bare = (await _search(component_ws, "sensor.badge_temp"))["matches"][0]
    entity = (await _search(component_ws, "sensor.badge_hum"))["matches"][0]
    header = (await _search(component_ws, "welcome banner"))["matches"][0]

    assert (bare["card_type"], bare["matched_field"], bare["card_path"]) == (
        "badge",
        "badges",
        "views[0].badges[0]",
    )
    assert bare["view_title"] == "Living"
    assert (entity["matched_field"], entity["card_path"]) == (
        "entity",
        "views[0].badges[1]",
    )
    assert (header["card_type"], header["card_path"], header["view_index"]) == (
        "markdown",
        "views[1].header.card",
        1,
    )


@pytest.mark.usefixtures("home")
async def test_an_empty_query_matches_nothing(
    component_ws: MockHAClientWebSocket,
) -> None:
    result = await _search(component_ws, "")

    assert (result["matches"], result["document_matches"], result["truncated"]) == (
        [],
        [],
        False,
    )


async def test_a_search_with_too_many_matches_says_it_was_cut(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _lovelace(hass)
    entities = [f"light.e{i}" for i in range(1000)]
    body = {"views": [{"cards": [{"type": "entities", "entities": entities}]}]}
    await _dashboard(component_ws, "dash-many", body=body)

    result = await _search(component_ws, "light.e")

    assert result["truncated"] is True
    assert 0 < len(result["matches"]) < len(entities)


# --- search over whole documents (the server's ha_search dashboard bucket) ----------


async def test_a_view_title_match_is_a_document_match_not_a_card_match(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The title is the dashboard's registry title, not its body's."""
    await _lovelace(hass)
    body = {"title": "Body Title", "views": [{"title": "solar_overview", "cards": []}]}
    await _dashboard(component_ws, "dash-solar", title="Solar", body=body)

    by_view = await _search(component_ws, "solar_overview")
    by_body_title = await _search(component_ws, "body title")

    assert by_view["matches"] == []
    for result in (by_view, by_body_title):
        assert result["document_matches"] == [
            {"url_path": "dash-solar", "title": "Solar"}
        ]
        assert (result["yaml_skipped"], result["load_failed"]) == (0, 0)


async def test_yaml_dashboards_the_default_included_are_counted_as_skipped(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A YAML default dashboard has no list row the server could count."""
    await _lovelace(hass, mode="yaml", dashboards=_yaml_dashboards())
    await _dashboard(component_ws, "dash-home", body={"views": []})

    result = await _search(component_ws, "anything")

    assert (result["yaml_skipped"], result["load_failed"]) == (2, 0)


async def test_dashboards_core_cannot_load_are_counted_not_silently_skipped(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    hass_storage: dict[str, Any],
) -> None:
    """A store Core cannot load and a config that is no mapping are gaps; a
    dashboard with nothing saved has nothing to search."""
    await _lovelace(hass)
    await _broken(hass, component_ws, hass_storage, "dash-broken")
    await _dashboard(component_ws, "dash-odd", body="not a dashboard")
    await _dashboard(component_ws, "dash-fresh")

    result = await _search(component_ws, "zzz")

    assert result["load_failed"] == 2
    assert result["document_matches"] == []
