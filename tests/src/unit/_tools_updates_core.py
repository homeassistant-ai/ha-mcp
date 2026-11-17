"""Update entities and the upstream pages ``ha_manage_updates`` reads.

Each update is an ``UpdateEntity`` of a platform below Core's ``update``
integration, as Core's own tests build them. GitHub, the Home Assistant blog
and PyPI are upstreams: respx serves the pages a test puts in ``Web`` and
answers 404 for any other, while the client's requests to Core pass through.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockPlatform, mock_platform

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_updates import UpdateTools

CORE_UPDATE = "update.home_assistant_core_update"


class SoftwareUpdate(UpdateEntity):
    """An update the device below the integration offers; it records installs."""

    _attr_should_poll = False

    def __init__(
        self,
        entity_id: str,
        *,
        title: str | None = None,
        installed: str = "1.0.0",
        latest: str = "1.1.0",
        features: UpdateEntityFeature = UpdateEntityFeature(0),
        release_url: str | None = None,
        in_progress: bool = False,
        auto_update: bool = False,
        notes: str | None = None,
    ) -> None:
        self.entity_id = entity_id
        self._attr_unique_id = entity_id
        self._attr_title = title
        self._attr_installed_version = installed
        self._attr_latest_version = latest
        self._attr_supported_features = features
        self._attr_release_url = release_url
        self._attr_in_progress = in_progress
        self._attr_auto_update = auto_update
        self.notes = notes
        self.installs: list[tuple[str | None, bool]] = []

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        self.installs.append((version, backup))

    async def async_release_notes(self) -> str | None:
        return self.notes


async def set_up_updates(
    hass: HomeAssistant, *updates: SoftwareUpdate, integration: str = "test"
) -> None:
    """Core's update integration, with ``updates`` from ``integration``."""

    async def setup_platform(
        hass: HomeAssistant,
        config: ConfigType,
        async_add_entities: AddEntitiesCallback,
        discovery_info: DiscoveryInfoType | None = None,
    ) -> None:
        async_add_entities(updates)

    mock_platform(
        hass,
        f"{integration}.update",
        MockPlatform(async_setup_platform=setup_platform),
    )
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(
        hass, "update", {"update": {"platform": integration}}
    )
    await hass.async_block_till_done()


async def manage_updates(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = await UpdateTools(client).ha_manage_updates(**kwargs)
    return result


def core_release(version: str) -> str:
    """GitHub's API URL of a Home Assistant Core release."""
    return f"https://api.github.com/repos/home-assistant/core/releases/tags/{version}"


class Web:
    """The upstream pages a test serves; any other URL answers 404."""

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.hosts: dict[str, dict[str, Any]] = {}
        self.unreachable: set[str] = set()
        self.requested: list[str] = []

    def serve(self, url: str, **response: Any) -> None:
        """Answer ``url`` with ``httpx.Response(200, **response)``."""
        self.pages[url] = response

    def answer(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        if url in self.unreachable:
            raise httpx.ConnectError("upstream unreachable", request=request)
        page = self.pages.get(url, self.hosts.get(request.url.host))
        if page is None:
            return httpx.Response(404)
        return httpx.Response(200, **page)


@pytest.fixture
def web() -> Iterator[Web]:
    """The upstreams, served from a ``Web``; Core's own address passes through."""
    pages = Web()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="127.0.0.1").pass_through()
        router.route().mock(side_effect=pages.answer)
        yield pages
