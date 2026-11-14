"""The dashboard reads served by the component's ``dashboards`` command.

Each read runs on the server's own client against a real ``hass`` with Core's
lovelace integration: a YAML dashboard configured from a file and storage
dashboards created and saved through Core's ``lovelace/*`` commands. A read is
first made without the component, which sends Core's ``lovelace/*`` reads, then
again once the component's commands are registered: the two answers must match,
so a drift on either side fails here. A YAML dashboard's body is never served
by the component, since Core resolves its ``!secret`` values.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools import websocket_api
from ha_mcp.backup_dashboards import fetch_dashboard
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.tools.component_api import invalidate_caps
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools

from ._component_core import ws_result

_YAML = "yaml-dash"
_HOME = "dash-home"
_OFFICE = "dash-office"
_LIST = "lovelace/dashboards/list"
_CONFIG = "lovelace/config"
_COMPONENT = "ha_mcp_tools/dashboards"

_YAML_BODY = {
    "views": [
        {
            "cards": [
                {"type": "tile", "entity": "light.both"},
                {"type": "markdown", "content": "yaml secret"},
            ]
        }
    ]
}
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
_OFFICE_BODY = {
    "title": "Office",
    "views": [{"title": "Desk", "cards": [{"type": "light", "entity": "light.desk"}]}],
}
# Each entity sits in one container only: a bare-string badge, a dict badge
# and a sections-view header card, none of which the plain card walk visits.
_BADGE_HEADER_BODY = {
    "title": "Sensors",
    "views": [
        {
            "title": "Overview",
            "type": "sections",
            "badges": [
                "sensor.badge_only",
                {"type": "entity", "entity": "sensor.badge_dict"},
            ],
            "header": {
                "card": {"type": "markdown", "content": "sensor.header_only status"}
            },
            "sections": [
                {"cards": [{"type": "entities", "entities": ["light.in_section"]}]}
            ],
        }
    ],
}


@pytest.fixture
async def lovelace(hass: HomeAssistant) -> None:
    """Lovelace with a storage default and the YAML dashboard ``yaml-dash``."""
    path = Path(hass.config.path(f"{_YAML}.yaml"))
    await hass.async_add_executor_job(path.write_text, json.dumps(_YAML_BODY), "utf-8")
    yaml_dashboard = {
        "mode": "yaml",
        "filename": f"{_YAML}.yaml",
        "title": "YAML",
        "icon": "mdi:file",
        "show_in_sidebar": True,
        "require_admin": False,
    }
    assert await async_setup_component(
        hass, "lovelace", {"lovelace": {"dashboards": {_YAML: yaml_dashboard}}}
    )


@pytest.fixture
async def client(
    lovelace: None, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The server's client; the component is not there until ``_install``."""
    return core_client


async def _dashboard(
    client: HomeAssistantClient,
    url_path: str | None,
    body: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    """Create storage dashboard ``url_path`` (``None``: the default) and save ``body``."""
    if url_path is not None:
        created = await client.send_websocket_message(
            {
                "type": "lovelace/dashboards/create",
                "url_path": url_path,
                "title": url_path,
                **fields,
            }
        )
        assert created["success"] is True, created
    if body is not None:
        save: dict[str, Any] = {"type": "lovelace/config/save", "config": body}
        if url_path is not None:
            save["url_path"] = url_path
        saved = await client.send_websocket_message(save)
        assert saved["success"] is True, saved


def _install(hass: HomeAssistant, client: HomeAssistantClient) -> None:
    """Register the component's commands; the client probes for them again."""
    websocket_api.async_register_commands(hass)
    invalidate_caps(client)


@contextmanager
def _lovelace_reads(client: HomeAssistantClient) -> Iterator[list[str]]:
    """The ``lovelace/*`` reads the client sends meanwhile, as ``type`` or
    ``type:url_path``."""
    reads: list[str] = []
    with patch.object(
        client, "send_websocket_message", wraps=client.send_websocket_message
    ) as spy:
        yield reads
    for call in spy.call_args_list:
        message = call.args[0]
        if message["type"] == _LIST:
            reads.append(_LIST)
        elif message["type"] == _CONFIG:
            reads.append(f"{_CONFIG}:{message.get('url_path')}")


async def _both_paths[T](
    hass: HomeAssistant,
    client: HomeAssistantClient,
    read: Callable[[HomeAssistantClient], Awaitable[T]],
) -> tuple[T, list[str], T, list[str]]:
    """``read`` without, then with the component: each answer and its
    ``lovelace/*`` reads."""
    with _lovelace_reads(client) as legacy_reads:
        legacy = await read(client)
    _install(hass, client)
    with _lovelace_reads(client) as component_reads:
        component = await read(client)
    return legacy, legacy_reads, component, component_reads


def _get(**kwargs: Any) -> Callable[[HomeAssistantClient], Awaitable[dict[str, Any]]]:
    async def read(client: HomeAssistantClient) -> dict[str, Any]:
        return await DashboardConfigTools(client).ha_config_get_dashboard(**kwargs)

    return read


# --- list ----------------------------------------------------------------------


async def test_the_list_keeps_the_yaml_dashboard_on_both_paths(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """Listing metadata is safe (only a body carries resolved ``!secret``), and
    Core lists YAML dashboards, so installing the component must not hide one."""
    await _dashboard(client, _HOME, icon="mdi:home")

    legacy, legacy_reads, component, component_reads = await _both_paths(
        hass, client, _get(list_only=True)
    )

    assert component["dashboards"] == legacy["dashboards"]
    assert {(d["url_path"], d["mode"]) for d in component["dashboards"]} == {
        (_HOME, "storage"),
        (_YAML, "yaml"),
    }
    assert legacy_reads == [_LIST]
    assert component_reads == []


async def test_a_listed_dashboard_is_cores_stored_item(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """A hand-kept key list would add ``icon: None`` for an icon never set."""
    await _dashboard(client, _HOME)

    legacy, _, component, _ = await _both_paths(hass, client, _get(list_only=True))

    (row,) = [d for d in component["dashboards"] if d["url_path"] == _HOME]
    assert "icon" not in row
    assert component["dashboards"] == legacy["dashboards"]


async def test_the_list_carries_the_fields_core_accepts_on_update(
    lovelace: None,
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring settings needs Core's update schema, not a copy of it."""
    fields = {vol.Optional(k): None for k in ("title", "icon", "sidebar_order")}
    monkeypatch.setattr(
        "homeassistant.components.lovelace.const.STORAGE_DASHBOARD_UPDATE_FIELDS",
        fields,
    )

    result = await ws_result(component_ws, _COMPONENT, mode="list")

    assert result["update_fields"] == ["title", "icon", "sidebar_order"]


# --- get -----------------------------------------------------------------------


async def test_a_storage_body_is_the_same_on_both_paths(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    await _dashboard(client, _HOME, _HOME_BODY)

    legacy, legacy_reads, component, component_reads = await _both_paths(
        hass, client, _get(url_path=_HOME)
    )

    assert component["config"] == legacy["config"] == _HOME_BODY
    assert component["config_hash"] == legacy["config_hash"]
    assert f"{_CONFIG}:{_HOME}" in legacy_reads
    assert component_reads == []


async def test_the_default_dashboard_is_read_without_a_url_path(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    body = {"views": [{"title": "Overview", "cards": []}]}
    await _dashboard(client, None, body)

    legacy, legacy_reads, component, component_reads = await _both_paths(
        hass, client, _get()
    )

    assert component["config"] == legacy["config"] == body
    assert any(read.startswith(_CONFIG) for read in legacy_reads)
    assert component_reads == []


async def test_a_yaml_body_is_read_through_core_not_the_component(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    _install(hass, client)
    ws = await get_websocket_client(
        url=client.base_url, token=client.token, verify_ssl=client.verify_ssl
    )

    with (
        _lovelace_reads(client) as reads,
        patch.object(ws, "send_command", wraps=ws.send_command) as frames,
    ):
        resp = await DashboardConfigTools(client).ha_config_get_dashboard(
            url_path=_YAML
        )

    assert resp["config"] == _YAML_BODY
    assert f"{_CONFIG}:{_YAML}" in reads
    # The component was asked first, and refused the body.
    assert [c.args[0] for c in frames.await_args_list].count(_COMPONENT) == 1


# --- search across dashboards --------------------------------------------------


async def test_search_finds_the_same_cards_on_both_paths(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The component hands every storage body over in one frame; without it the
    server lists the dashboards and reads each one."""
    await _dashboard(client, _HOME, _HOME_BODY)
    await _dashboard(client, _OFFICE, _OFFICE_BODY)

    legacy, legacy_reads, component, component_reads = await _both_paths(
        hass, client, _get(query="light")
    )

    assert component["matches"]
    assert component["matches"] == legacy["matches"]
    assert component["truncated"] == legacy["truncated"]
    assert sorted(legacy_reads) == sorted(
        [_LIST, f"{_CONFIG}:{_HOME}", f"{_CONFIG}:{_OFFICE}"]
    )
    assert component_reads == []


async def test_an_uppercase_query_matches_on_both_paths(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    await _dashboard(client, _HOME, _HOME_BODY)
    await _dashboard(client, _OFFICE, _OFFICE_BODY)

    legacy, _, component, _ = await _both_paths(hass, client, _get(query="LIGHT"))

    assert component["matches"]
    assert component["matches"] == legacy["matches"]


async def test_badges_and_header_cards_are_searched_on_both_paths(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """An entity used only as a badge or in a header card must be found, or a
    rename would read as "no dashboard uses it"."""
    await _dashboard(client, "dash-sensors", _BADGE_HEADER_BODY)
    queries = ("sensor.badge_only", "sensor.badge_dict", "sensor.header_only")

    async def search(on: HomeAssistantClient) -> list[Any]:
        tools = DashboardConfigTools(on)
        return [
            (await tools.ha_config_get_dashboard(query=query))["matches"]
            for query in queries
        ]

    legacy, _, component, _ = await _both_paths(hass, client, search)

    assert component == legacy
    (bare,), (as_dict,), (header,) = component
    assert ".badges[" in bare["jq_path"]
    assert bare["card_type"] == "badge"
    assert bare["matched"] == [{"field": "badges", "value": "sensor.badge_only"}]
    assert ".badges[" in as_dict["jq_path"]
    assert as_dict["card_type"] == "badge"
    assert as_dict["matched"] == [{"field": "entity", "value": "sensor.badge_dict"}]
    assert header["jq_path"].endswith(".header.card")
    assert header["card_type"] == "markdown"


async def test_too_many_matches_truncate_alike_on_both_paths(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    cards = [{"type": "tile", "entity": f"light.e{i}"} for i in range(500)]
    await _dashboard(client, _HOME, {"views": [{"cards": cards}]})

    legacy, _, component, _ = await _both_paths(hass, client, _get(query="light.e"))

    assert component["truncated"] is legacy["truncated"] is True
    assert 0 < len(component["matches"]) < len(cards)
    assert component["matches"] == legacy["matches"]


async def test_only_the_component_search_reaches_the_default_dashboard(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """Documented asymmetry (see the search caveat in the tool's docstring):
    Core's list never holds the default dashboard, so only the component's
    frame carries its body."""
    body = {
        "views": [{"cards": [{"type": "entities", "entities": ["light.default_only"]}]}]
    }
    await _dashboard(client, None, body)
    await _dashboard(client, _HOME, _HOME_BODY)

    legacy, _, component, _ = await _both_paths(
        hass, client, _get(query="light.default_only")
    )

    assert [m["url_path"] for m in component["matches"]] == ["default"]
    assert legacy["matches"] == []


async def test_search_never_returns_a_yaml_body(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """With include_config, card bodies come only from storage dashboards."""
    body = {"views": [{"cards": [{"type": "tile", "entity": "light.both"}]}]}
    await _dashboard(client, _HOME, body)
    _install(hass, client)

    resp = await DashboardConfigTools(client).ha_config_get_dashboard(
        query="light.both", include_config=True
    )

    assert [m["url_path"] for m in resp["matches"]] == [_HOME]
    assert resp["matches"][0]["card_config"] == body["views"][0]["cards"][0]


# --- set -----------------------------------------------------------------------


async def test_set_tells_an_existing_dashboard_from_a_new_one_by_the_component_list(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    await _dashboard(client, _HOME)
    _install(hass, client)
    tools = DashboardConfigTools(client)

    with _lovelace_reads(client) as reads:
        existing = await tools.ha_config_set_dashboard(url_path=_HOME, title="Den")
        new = await tools.ha_config_set_dashboard(url_path="dash-ghost", title="Ghost")
        default = await tools.ha_config_set_dashboard(
            url_path="lovelace", config={"views": [{"cards": []}]}
        )

    assert (existing["action"], existing["metadata_updated"]) == ("update", True)
    assert (new["action"], new["dashboard_created"]) == ("create", True)
    # Core's list never holds the built-in default, yet it always exists.
    assert default["action"] == "update"
    assert _LIST not in reads


# --- auto-backup capture -------------------------------------------------------


async def test_a_backup_captures_a_storage_dashboard_through_the_component(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    await _dashboard(client, _HOME, _HOME_BODY, icon="mdi:home")
    listed = await client.send_websocket_message({"type": _LIST})
    (row,) = [d for d in listed["result"] if d["url_path"] == _HOME]
    _install(hass, client)

    with _lovelace_reads(client) as reads:
        captured = await fetch_dashboard(client, _HOME)

    assert captured == {"dashboard": row, "config": _HOME_BODY}
    assert reads == []


async def test_a_backup_captures_a_yaml_dashboard_through_core(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The component refuses the body; the capture has no storage row for it."""
    _install(hass, client)

    with _lovelace_reads(client) as reads:
        captured = await fetch_dashboard(client, _YAML)

    assert captured == {"dashboard": None, "config": _YAML_BODY}
    assert f"{_CONFIG}:{_YAML}" in reads
