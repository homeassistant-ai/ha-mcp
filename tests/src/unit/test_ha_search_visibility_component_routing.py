"""Routing of ``ha_search`` under the ``search_visibility`` capability.

An active entity-visibility filter used to force the legacy path (a plain
``search`` component filters nothing, so it would leak hidden entities). A
component that advertises ``search_visibility`` takes the serialized hide config
as its ``visibility`` param and excludes hidden entities itself:

- filter active + ``search_visibility``: the component, sent ``visibility`` with
  the nine hide dimensions and nothing else (an older component's strict schema
  rejects an unknown key);
- filter active + ``search_visibility_allowlist_authorization``: ``visibility``
  also carries ``allowlist_authorization: True``, on the capability alone;
- filter active + only ``search``: the legacy path;
- filter inactive: the component, without the param;
- an unloadable config: the legacy path, never an unfiltered component;
- the component's search failing, unknown or unreachable: the legacy path, which
  applies the filter itself.

The server's own client runs on a real ``hass`` with the component's commands;
an older component is the current one with fewer capabilities. Core answers
the component's search and the legacy reads. A spy on the pooled socket records
the request the server sent; one on the REST state read records the legacy path.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import websocket_api as component_ws_api
from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import component_api
from ha_mcp.tools.search import component as search_component
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import VISIBILITY_FILENAME, save_visibility_config

from ._client_spies import sent_commands
from ._routing_core import invalidations, pooled_socket, sent_frames

SEARCH = "ha_mcp_tools/search"
ALLOWLIST_AUTH = "search_visibility_allowlist_authorization"
# The nine hide dimensions ``VisibilityConfig.to_wire`` emits.
_WIRE_KEYS = {
    "exclude_categories",
    "exclude_hidden",
    "deny_entity_ids",
    "exclude_areas",
    "exclude_labels",
    "allow_entity_ids",
    "allow_areas",
    "allow_labels",
    "respect_assist_exposure",
}


class Searched(NamedTuple):
    response: dict[str, Any]
    frames: list[dict[str, Any]]
    legacy_reads: int


@pytest.fixture
async def core(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's registry API, two kitchen entities, and the server's data dir."""
    assert await async_setup_component(hass, "config", {})
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen Light"})
    hass.states.async_set(
        "sensor.kitchen_temp", "21", {"friendly_name": "Kitchen Temperature"}
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)


@pytest.fixture
async def client(
    core: None, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    return component_core_client


def _deny_kitchen_light(tmp_path: Path) -> None:
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True, exclude_categories=[], deny_entity_ids=["light.kitchen"]
        ),
    )


def _allow_kitchen_temp(tmp_path: Path) -> None:
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True,
            exclude_categories=["diagnostic"],
            allow_entity_ids=["sensor.kitchen_temp"],
        ),
    )


def _without(monkeypatch: pytest.MonkeyPatch, *capabilities: str) -> None:
    """The component as a build from before ``capabilities``."""
    monkeypatch.setattr(
        system,
        "CAPABILITIES",
        [c for c in system.CAPABILITIES if c not in capabilities],
    )


@contextmanager
def _legacy_reads(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    with patch.object(client, "get_states", AsyncMock(wraps=client.get_states)) as spy:
        yield spy


async def _search(client: HomeAssistantClient) -> Searched:
    socket = await pooled_socket(client)
    with sent_commands(socket) as sent, _legacy_reads(client) as legacy:
        response: dict[str, Any] = await SearchTools(
            client, SmartSearchTools(client=client)
        ).ha_search(query="kitchen")
    return Searched(response, sent_frames(sent, SEARCH), legacy.await_count)


def _entity_ids(response: dict[str, Any]) -> set[str]:
    return {e["entity_id"] for e in response["entities"]}


def _legacy_warning(response: dict[str, Any]) -> bool:
    return any("served via legacy path" in w for w in response.get("warnings", []))


async def test_an_active_filter_rides_the_component_request_with_the_capability(
    tmp_path: Path, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly the nine hide dimensions: no ``enabled`` or ``version``, and no
    ``allowlist_authorization`` for a component that does not advertise it."""
    _without(monkeypatch, ALLOWLIST_AUTH)
    _deny_kitchen_light(tmp_path)

    searched = await _search(client)

    assert searched.response["success"] is True
    assert searched.legacy_reads == 0
    (frame,) = searched.frames
    visibility = frame["visibility"]
    assert set(visibility) == _WIRE_KEYS
    assert visibility["deny_entity_ids"] == ["light.kitchen"]
    assert visibility["exclude_categories"] == []


async def test_an_active_filter_without_the_capability_uses_the_legacy_path(
    tmp_path: Path, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without(monkeypatch, "search_visibility", ALLOWLIST_AUTH)
    _deny_kitchen_light(tmp_path)

    searched = await _search(client)

    assert searched.frames == []
    assert searched.legacy_reads == 1
    assert _entity_ids(searched.response) == {"sensor.kitchen_temp"}


async def test_an_allowlist_needs_the_allowlist_authorization_capability(
    tmp_path: Path, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older visibility component cannot serve the revised allowlist rules."""
    _without(monkeypatch, ALLOWLIST_AUTH)
    _allow_kitchen_temp(tmp_path)

    searched = await _search(client)

    assert searched.frames == []
    assert searched.legacy_reads == 1
    assert _entity_ids(searched.response) == {"sensor.kitchen_temp"}


async def test_an_allowlist_rides_the_component_with_its_authorization_flag(
    tmp_path: Path, client: HomeAssistantClient
) -> None:
    """The wire flag, not the capability alone, tells the component to apply
    the revised precedence."""
    _allow_kitchen_temp(tmp_path)

    searched = await _search(client)

    assert searched.response["success"] is True
    assert searched.legacy_reads == 0
    (frame,) = searched.frames
    assert frame["visibility"]["allow_entity_ids"] == ["sensor.kitchen_temp"]
    assert frame["visibility"]["allowlist_authorization"] is True


async def test_a_deny_only_filter_still_sends_the_authorization_flag(
    tmp_path: Path, client: HomeAssistantClient
) -> None:
    """The flag rides on the capability, not on an active allowlist, so the
    wire shape does not vary with the config."""
    _deny_kitchen_light(tmp_path)

    searched = await _search(client)

    assert searched.legacy_reads == 0
    (frame,) = searched.frames
    visibility = frame["visibility"]
    assert set(visibility) == _WIRE_KEYS | {"allowlist_authorization"}
    assert visibility["allowlist_authorization"] is True
    assert visibility["deny_entity_ids"] == ["light.kitchen"]


async def test_an_inactive_filter_sends_no_visibility_param(
    tmp_path: Path, client: HomeAssistantClient
) -> None:
    """Nothing for a capable component to exclude, and an older search-only
    component is unaffected."""
    save_visibility_config(tmp_path, VisibilityConfig(enabled=False))

    searched = await _search(client)

    assert searched.response["success"] is True
    assert searched.legacy_reads == 0
    (frame,) = searched.frames
    assert "visibility" not in frame


async def test_a_failing_component_search_falls_back_and_keeps_the_filter(
    hass: HomeAssistant,
    tmp_path: Path,
    core: None,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise HomeAssistantError("search failed")

    monkeypatch.setattr("custom_components.ha_mcp_tools.websocket_api._do_search", fail)
    component_ws_api.async_register_commands(hass)
    _deny_kitchen_light(tmp_path)

    searched = await _search(core_client)

    assert searched.legacy_reads == 1
    assert _legacy_warning(searched.response)
    assert "light.kitchen" not in _entity_ids(searched.response)


async def test_a_component_without_the_search_falls_back_silently_and_invalidates(
    hass: HomeAssistant,
    tmp_path: Path,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deny_kitchen_light(tmp_path)
    assert await component_api.get_component_caps(client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], SEARCH)

    with invalidations(search_component) as invalidate:
        searched = await _search(client)

    assert searched.response["success"] is True
    assert searched.legacy_reads == 1
    assert _entity_ids(searched.response) == {"sensor.kitchen_temp"}
    assert not _legacy_warning(searched.response)
    invalidate.assert_called_once_with(client)


async def test_an_unreachable_component_socket_falls_back_with_a_warning(
    tmp_path: Path, client: HomeAssistantClient
) -> None:
    """The legacy path reads REST states and the registry bridge, so it does
    not fail the same way; the capability stays cached."""
    _deny_kitchen_light(tmp_path)
    assert await component_api.get_component_caps(client) is not None

    with (
        patch.object(
            search_component,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket down")),
        ),
        invalidations(search_component) as invalidate,
    ):
        searched = await _search(client)

    assert searched.response["success"] is True
    assert searched.legacy_reads == 1
    assert _legacy_warning(searched.response)
    assert "light.kitchen" not in _entity_ids(searched.response)
    invalidate.assert_not_called()


async def test_an_unloadable_config_never_routes_an_unfiltered_component_search(
    tmp_path: Path, client: HomeAssistantClient
) -> None:
    (tmp_path / VISIBILITY_FILENAME).write_text("{ not valid json", encoding="utf-8")

    searched = await _search(client)

    assert searched.response["success"] is True
    assert searched.frames == []
    assert searched.legacy_reads == 1
