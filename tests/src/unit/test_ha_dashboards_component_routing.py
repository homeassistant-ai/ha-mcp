"""The dashboard reads through the component's ``dashboards`` command, or Core's.

``ha_config_get_dashboard`` (list, get, search) and ``fetch_dashboards_list``
prefer one ``ha_mcp_tools/dashboards`` frame; without the component, or when
the frame fails, they read Core's ``lovelace/dashboards/list`` and
``lovelace/config``. A YAML dashboard's body is never searched without the
component and its matched values never shown, since Core resolves
``!secret`` when it loads one.

Core's lovelace integration holds a storage dashboard saved through
``lovelace/config/save`` and a YAML dashboard from its file; what each read
sent is what Core received.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import dump

from custom_components.ha_mcp_tools import websocket_api
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_config_dashboards
from ha_mcp.tools.component_api import get_component_caps, invalidate_caps
from ha_mcp.tools.tools_config_dashboards import (
    DashboardConfigTools,
    compute_config_hash,
    fetch_dashboards_list,
)

from ._command_faults import dropped_on, pooled_socket, received, unanswered
from ._config_dir_yaml import write_config_files
from ._tool_errors import error_of

DASHBOARDS = "ha_mcp_tools/dashboards"
LIST = "lovelace/dashboards/list"
CONFIG = "lovelace/config"
HOME = "dash-home"
YAML = "yaml-dash"
UNTAGGED = "old-dash"
HOME_BODY = {
    "title": "Home",
    "views": [
        {
            "title": "Living",
            "cards": [{"type": "entities", "entities": ["light.kitchen"]}],
        }
    ],
}
YAML_BODY = {
    "views": [{"cards": [{"type": "entities", "entities": ["light.kitchen"]}]}]
}


async def _save(client: HomeAssistantClient, url_path: str, body: Any) -> None:
    """A storage dashboard whose saved config is ``body``."""
    for message in (
        {
            "type": "lovelace/dashboards/create",
            "url_path": url_path,
            "title": url_path,
            "mode": "storage",
        },
        {"type": "lovelace/config/save", "url_path": url_path, "config": body},
    ):
        reply = await client.send_websocket_message(message)
        assert reply["success"], reply


async def _lovelace(
    hass: HomeAssistant, client: HomeAssistantClient
) -> HomeAssistantClient:
    await write_config_files(hass, {f"{YAML}.yaml": dump(YAML_BODY)})
    assert await async_setup_component(
        hass,
        "lovelace",
        {
            "lovelace": {
                "dashboards": {
                    YAML: {"mode": "yaml", "filename": f"{YAML}.yaml", "title": "Y"}
                }
            }
        },
    )
    await _save(client, HOME, HOME_BODY)
    return client


@pytest.fixture
async def component(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    return await _lovelace(hass, component_core_client)


@pytest.fixture
async def legacy(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    return await _lovelace(hass, core_client)


@pytest.fixture
def untagged(hass_storage: dict[str, Any]) -> None:
    """A storage dashboard Core stored before it tagged every row with ``mode``."""
    hass_storage["lovelace_dashboards"] = {
        "version": 1,
        "key": "lovelace_dashboards",
        "data": {
            "items": [
                {
                    "id": "old_dash",
                    "url_path": UNTAGGED,
                    "title": "Old",
                    "icon": None,
                    "show_in_sidebar": True,
                    "require_admin": False,
                }
            ]
        },
    }
    hass_storage["lovelace.old_dash"] = {
        "version": 1,
        "key": "lovelace.old_dash",
        "data": {"config": HOME_BODY},
    }


def _get(client: HomeAssistantClient) -> Any:
    return DashboardConfigTools(client).ha_config_get_dashboard


def _by_url(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["url_path"]: row for row in rows}


def _spy_invalidate() -> Any:
    return patch.object(
        tools_config_dashboards, "invalidate_caps", wraps=invalidate_caps
    )


# --- list ---------------------------------------------------------------------


async def test_the_component_list_keeps_the_yaml_rows_core_lists(
    hass: HomeAssistant, legacy: HomeAssistantClient
) -> None:
    """Listing metadata is safe (only bodies carry resolved ``!secret``), so
    the component lists the same rows as Core's own list."""
    cores = (await _get(legacy)(list_only=True))["dashboards"]
    websocket_api.async_register_commands(hass)
    invalidate_caps(legacy)

    with received(hass, DASHBOARDS, LIST) as seen:
        resp = await _get(legacy)(list_only=True)

    assert resp["dashboards"] == cores
    assert resp["count"] == len(cores)
    assert (_by_url(cores)[HOME]["mode"], _by_url(cores)[YAML]["mode"]) == (
        "storage",
        "yaml",
    )
    assert [{k: v for k, v in m.items() if k != "id"} for m in seen[DASHBOARDS]] == [
        {"type": DASHBOARDS, "mode": "list"}
    ]
    assert seen[LIST] == []


async def test_without_the_component_the_list_is_cores(
    hass: HomeAssistant, legacy: HomeAssistantClient
) -> None:
    with received(hass, LIST) as seen:
        resp = await _get(legacy)(list_only=True)

    assert {HOME, YAML} <= set(_by_url(resp["dashboards"]))
    assert len(seen[LIST]) == 1


async def test_a_downgraded_component_is_dropped_and_cores_list_serves(
    hass: HomeAssistant, component: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await get_component_caps(component) is not None
    monkeypatch.delitem(hass.data["websocket_api"], DASHBOARDS)

    with _spy_invalidate() as drop, received(hass, LIST) as seen:
        resp = await _get(component)(list_only=True)

    assert HOME in _by_url(resp["dashboards"])
    assert len(seen[LIST]) == 1
    drop.assert_called_once_with(component)


async def test_a_list_core_never_answers_falls_back_and_keeps_the_component(
    hass: HomeAssistant, component: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _spy_invalidate() as drop, received(hass, LIST) as seen:
        async with unanswered(hass, DASHBOARDS, monkeypatch):
            resp = await _get(component)(list_only=True)

    assert HOME in _by_url(resp["dashboards"])
    assert len(seen[LIST]) == 1
    drop.assert_not_called()


async def test_a_dropped_socket_falls_back_and_keeps_the_component(
    hass: HomeAssistant, component: HomeAssistantClient
) -> None:
    """The legacy reads reconnect, and the auto-backup capture of a write that
    funnels through this list must not be blocked by an escaping error."""
    assert await get_component_caps(component) is not None
    socket = await pooled_socket(component)

    with (
        _spy_invalidate() as drop,
        dropped_on(hass, DASHBOARDS, socket),
        received(hass, LIST) as seen,
    ):
        rows = await fetch_dashboards_list(component)

    assert rows is not None and HOME in _by_url(rows)
    assert len(seen[LIST]) == 1
    drop.assert_not_called()


async def test_the_shared_list_comes_from_the_component(
    hass: HomeAssistant, component: HomeAssistantClient
) -> None:
    """The set and delete tools check existence against this list."""
    with received(hass, LIST) as seen:
        rows = await fetch_dashboards_list(component)

    assert rows is not None
    assert (_by_url(rows)[HOME]["mode"], _by_url(rows)[YAML]["mode"]) == (
        "storage",
        "yaml",
    )
    assert seen[LIST] == []


@pytest.mark.parametrize("url_path", ["lovelace", HOME])
async def test_a_write_finds_an_existing_dashboard_in_the_component_list(
    hass: HomeAssistant, component: HomeAssistantClient, url_path: str
) -> None:
    """The built-in default dashboard exists even where no list row names it."""
    create = "lovelace/dashboards/create"

    with received(hass, LIST, create) as seen:
        resp = await DashboardConfigTools(component).ha_config_set_dashboard(
            url_path=url_path, config={"views": []}
        )

    assert resp["dashboard_created"] is False
    assert seen == {LIST: [], create: []}


# --- get ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url_path", "sent"), [(HOME, {"url_path": HOME}), ("default", {})]
)
async def test_a_get_is_one_component_frame(
    hass: HomeAssistant,
    component: HomeAssistantClient,
    url_path: str,
    sent: dict[str, str],
) -> None:
    """ "default" is the component's default dashboard: no url_path."""
    reply = await component.send_websocket_message(
        {"type": "lovelace/config/save", "config": HOME_BODY}
    )
    assert reply["success"], reply

    with received(hass, DASHBOARDS, CONFIG) as seen:
        resp = await _get(component)(url_path=url_path)

    assert (resp["action"], resp["config"]) == ("get", HOME_BODY)
    assert resp["config_hash"] == compute_config_hash(HOME_BODY)
    assert [
        {k: v for k, v in m.items() if k not in ("id", "type")}
        for m in seen[DASHBOARDS]
    ] == [{"mode": "get", **sent}]
    assert seen[CONFIG] == []


async def test_a_yaml_dashboard_is_read_through_cores_config(
    hass: HomeAssistant, component: HomeAssistantClient
) -> None:
    """The component never hands out a YAML body; Core's own read does."""
    with received(hass, CONFIG) as seen:
        resp = await _get(component)(url_path=YAML)

    assert resp["config"] == YAML_BODY
    assert [m["url_path"] for m in seen[CONFIG]] == [YAML]


async def test_an_unknown_dashboard_is_not_found_by_cores_read(
    hass: HomeAssistant, component: HomeAssistantClient
) -> None:
    with received(hass, CONFIG) as seen, pytest.raises(ToolError) as exc:
        await _get(component)(url_path="ghost-dash")

    assert error_of(exc)["code"] == "RESOURCE_NOT_FOUND"
    assert [m["url_path"] for m in seen[CONFIG]] == ["ghost-dash"]


async def test_a_forced_reload_skips_the_component(
    hass: HomeAssistant, component: HomeAssistantClient
) -> None:
    """The component read has no force: only Core's read busts its cache."""
    with received(hass, DASHBOARDS, CONFIG) as seen:
        resp = await _get(component)(url_path=HOME, force_reload=True)

    assert resp["config"] == HOME_BODY
    assert seen[DASHBOARDS] == []
    assert [(m["url_path"], m.get("force")) for m in seen[CONFIG]] == [(HOME, True)]


# --- search across dashboards -------------------------------------------------


async def test_a_search_reads_every_storage_config_in_one_frame(
    hass: HomeAssistant, component: HomeAssistantClient
) -> None:
    with received(hass, DASHBOARDS, LIST, CONFIG) as seen:
        resp = await _get(component)(query="light.kitchen")

    assert (resp["action"], resp["match_count"], resp["truncated"]) == (
        "search",
        1,
        False,
    )
    match = resp["matches"][0]
    assert (match["url_path"], match["jq_path"]) == (HOME, ".views[0].cards[0]")
    assert match["python_path"] == "['views'][0]['cards'][0]"
    assert match["matched"] == [{"field": "entities", "value": "light.kitchen"}]
    assert match["config_hash"] == compute_config_hash(HOME_BODY)
    assert [m["mode"] for m in seen[DASHBOARDS]] == ["docs"]
    assert (seen[LIST], seen[CONFIG]) == ([], [])
    # The component read covers the default dashboard: nothing to disclose.
    assert "warnings" not in resp


async def test_a_search_names_how_many_dashboards_core_could_not_load(
    component: HomeAssistantClient,
) -> None:
    for url_path in ("dash-broken", "dash-bad"):
        # Core stores a string config as given; its load is no dashboard config.
        await _save(component, url_path, "boom")

    resp = await _get(component)(query="light.kitchen")

    unread = [w for w in resp["warnings"] if "2 storage dashboard(s)" in w]
    assert len(unread) == 1
    assert "log" not in unread[0]


async def test_without_the_component_a_search_reads_each_storage_dashboard(
    hass: HomeAssistant, legacy: HomeAssistantClient
) -> None:
    """Never the YAML dashboard's body; the default dashboard only the
    component reads, which the warnings say."""
    with received(hass, LIST, CONFIG) as seen:
        resp = await _get(legacy)(query="light.kitchen")

    assert (resp["action"], resp["match_count"]) == ("search", 1)
    match = resp["matches"][0]
    assert (match["url_path"], match["card_type"], match["jq_path"]) == (
        HOME,
        "entities",
        ".views[0].cards[0]",
    )
    assert match["matched"] == [{"field": "entities", "value": "light.kitchen"}]
    assert len(seen[LIST]) == 1
    assert [m["url_path"] for m in seen[CONFIG]] == [HOME]
    assert any("url_path='default'" in w for w in resp["warnings"])
    assert not any("Could not read" in w for w in resp["warnings"])


@pytest.mark.usefixtures("untagged")
async def test_a_search_never_reads_a_row_core_did_not_tag_storage(
    hass: HomeAssistant, legacy: HomeAssistantClient
) -> None:
    """An untagged row is not provably storage, and a YAML body resolves
    ``!secret``: its config is never requested."""
    with received(hass, CONFIG) as seen:
        resp = await _get(legacy)(query="light.kitchen")

    assert UNTAGGED not in [m["url_path"] for m in seen[CONFIG]]
    assert [m["url_path"] for m in resp["matches"]] == [HOME]


async def test_a_search_of_one_dashboard_reads_only_it(
    hass: HomeAssistant, legacy: HomeAssistantClient
) -> None:
    with received(hass, CONFIG) as seen:
        resp = await _get(legacy)(url_path=HOME, query="light.kitchen")

    assert resp["url_path"] == HOME
    assert resp["config_hash"] == compute_config_hash(HOME_BODY)
    assert resp["match_count"] == 1
    assert resp["matches"][0]["config_hash"] == resp["config_hash"]
    assert [m["url_path"] for m in seen[CONFIG]] == [HOME]


async def test_a_search_names_a_broken_dashboard_but_not_an_unconfigured_one(
    legacy: HomeAssistantClient,
) -> None:
    created = await legacy.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": "dash-fresh",
            "title": "Fresh",
            "mode": "storage",
        }
    )
    assert created["success"], created
    await _save(legacy, "dash-broken", "boom")

    resp = await _get(legacy)(query="light.kitchen")

    assert resp["match_count"] == 1
    (unread,) = [w for w in resp["warnings"] if "Could not read" in w]
    assert "dash-broken" in unread
    assert "dash-fresh" not in unread


async def test_a_search_without_lovelace_says_the_list_could_not_be_read(
    component_core_client: HomeAssistantClient,
) -> None:
    """The component reports no dashboards to read; Core refuses its own list
    too. That is no clean "no matches"."""
    resp = await _get(component_core_client)(query="light.kitchen")

    assert resp["match_count"] == 0
    assert any("dashboards list could not be read" in w for w in resp["warnings"])


async def test_a_badge_cannot_be_searched_by_heading(
    hass: HomeAssistant, legacy: HomeAssistantClient
) -> None:
    """View badges have no heading."""
    with (
        received(hass, LIST) as seen,
        pytest.raises(ToolError, match="cannot be combined with heading"),
    ):
        await _get(legacy)(card_type="badge", heading="Lights")

    assert seen[LIST] == []


async def test_list_only_with_search_criteria_says_it_searched(
    legacy: HomeAssistantClient,
) -> None:
    resp = await _get(legacy)(list_only=True, query="light.kitchen")

    assert resp["action"] == "search"
    assert any("list_only was ignored" in w for w in resp["warnings"])


async def test_search_criteria_are_trimmed(legacy: HomeAssistantClient) -> None:
    resp = await _get(legacy)(
        url_path=HOME, query="  light.kitchen ", entity_id="light.kitchen "
    )

    assert resp["match_count"] == 1
    criteria = resp["search_criteria"]
    assert (criteria["query"], criteria["entity_id"]) == (
        "light.kitchen",
        "light.kitchen",
    )


@pytest.mark.parametrize("criterion", ["query", "entity_id", "card_type", "heading"])
async def test_blank_search_criteria_are_refused_not_searched(
    hass: HomeAssistant, component: HomeAssistantClient, criterion: str
) -> None:
    with (
        received(hass, DASHBOARDS, LIST, CONFIG) as seen,
        pytest.raises(ToolError, match="Search needs a non-blank"),
    ):
        await _get(component)(**{criterion: "   "})

    assert seen == {DASHBOARDS: [], LIST: [], CONFIG: []}


# --- matched-card config of one dashboard -----------------------------------------
# With include_config the matched cards' bodies come back, so they are shown
# only for a dashboard its list row proves storage: a YAML body can carry
# resolved !secret values.


async def _matched_cards(
    client: HomeAssistantClient, url_path: str, *, include_config: bool = True
) -> dict[str, Any]:
    resp: dict[str, Any] = await _get(client)(
        url_path=url_path, entity_id="light.kitchen", include_config=include_config
    )
    assert resp["match_count"] == 1
    return resp


async def test_a_storage_dashboards_matched_cards_come_back(
    hass: HomeAssistant, legacy: HomeAssistantClient
) -> None:
    with received(hass, LIST) as seen:
        resp = await _matched_cards(legacy, HOME)

    assert "card_config" in resp["matches"][0]
    assert "warnings" not in resp
    assert len(seen[LIST]) == 1


@pytest.mark.parametrize("url_path", [YAML, UNTAGGED], ids=["yaml", "untagged"])
@pytest.mark.usefixtures("untagged")
async def test_a_dashboard_not_proven_storage_withholds_its_matched_cards(
    legacy: HomeAssistantClient, url_path: str
) -> None:
    """The match location is still reported, with a note."""
    resp = await _matched_cards(legacy, url_path)

    assert "card_config" not in resp["matches"][0]
    assert any("withheld" in w for w in resp["warnings"])


async def test_a_query_on_a_yaml_dashboard_names_fields_not_values(
    legacy: HomeAssistantClient,
) -> None:
    resp = await _get(legacy)(url_path=YAML, query="kitchen")

    assert resp["matches"][0]["matched"] == [{"field": "entities"}]
    assert any("withheld" in w for w in resp["warnings"])


async def test_without_matched_cards_the_storage_check_is_skipped(
    hass: HomeAssistant, legacy: HomeAssistantClient
) -> None:
    """Nothing to withhold, so the list is not read for it."""
    with received(hass, LIST) as seen:
        resp = await _matched_cards(legacy, YAML, include_config=False)

    assert "card_config" not in resp["matches"][0]
    assert "warnings" not in resp
    assert seen[LIST] == []
