"""Update entities and the upstream pages ``ha_manage_updates`` reads.

Each update is an ``UpdateEntity`` of a platform below Core's ``update``
integration, as Core's own tests build them. GitHub, the Home Assistant blog
and PyPI are upstreams: respx serves the pages a test puts in ``Web`` and
answers 404 for any other, while the client's requests to Core pass through.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from ipaddress import IPv4Address
from typing import Any

import httpx
import pytest
import respx
from aiohasupervisor.models import LogLevel, SupervisorInfo, UpdateChannel
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockPlatform, mock_platform

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_updates import UpdateTools

CORE_UPDATE = "update.home_assistant_core_update"


class SoftwareUpdate(UpdateEntity):
    """An update the device below the integration offers. It records installs
    and checks for updates; a check offers ``next_latest`` when set."""

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
        in_progress: bool | None = False,
        auto_update: bool = False,
        device_class: UpdateDeviceClass | None = None,
        notes: str | None = None,
    ) -> None:
        self.entity_id = entity_id
        self._attr_unique_id = entity_id
        self._attr_title = title
        self._attr_installed_version = installed
        self._attr_latest_version = latest
        self._attr_supported_features = features
        self._attr_release_url = release_url
        self._progress = in_progress
        self._attr_auto_update = auto_update
        self._attr_device_class = device_class
        self.notes = notes
        self.installs: list[tuple[str | None, bool]] = []
        self.checks = 0
        self.next_latest: str | None = None
        # Runs as the install finishes, before Core answers the call.
        self.on_install: Callable[[], object] | None = None
        # An install waits for this before it finishes.
        self.held: asyncio.Event | None = None

    @property
    def in_progress(self) -> bool | None:
        return self._progress

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        self.installs.append((version, backup))
        if self.held is not None:
            await self.held.wait()
        self._attr_installed_version = version or self._attr_latest_version
        if self.on_install is not None:
            self.on_install()

    async def async_update(self) -> None:
        self.checks += 1
        if self.next_latest is not None:
            self._attr_latest_version = self.next_latest

    async def async_release_notes(self) -> str | None:
        return self.notes


async def set_up_updates(
    hass: HomeAssistant, *updates: SoftwareUpdate, integration: str = "test"
) -> None:
    """Core's update integration, with ``updates`` from ``integration``."""
    await set_up_platforms(hass, {integration: list(updates)})


async def set_up_platforms(
    hass: HomeAssistant, platforms: dict[str, list[SoftwareUpdate]]
) -> None:
    """Core's update integration, with each integration's updates; added
    through discovery when another integration has set ``update`` up."""
    loaded = "update" in hass.config.components
    for integration, updates in platforms.items():

        async def setup_platform(
            hass: HomeAssistant,
            config: ConfigType,
            async_add_entities: AddEntitiesCallback,
            discovery_info: DiscoveryInfoType | None = None,
            updates: list[SoftwareUpdate] = updates,
        ) -> None:
            async_add_entities(updates)

        mock_platform(
            hass,
            f"{integration}.update",
            MockPlatform(async_setup_platform=setup_platform),
        )
        if loaded:
            await async_load_platform(hass, "update", integration, {}, {})
    assert await async_setup_component(hass, "config", {})
    if not loaded:
        assert await async_setup_component(
            hass, "update", {"update": [{"platform": name} for name in platforms]}
        )
    await hass.async_block_till_done()


def restore_update(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> str:
    """An update entity Core restored from its registry: its integration did
    not set it up again, so Core holds only its placeholder state."""
    entry = entity_registry.async_get_or_create(
        "update",
        "test",
        "gone",
        suggested_object_id="gone",
        supported_features=UpdateEntityFeature.INSTALL,
    )
    entry.write_unavailable_state(hass)
    return entry.entity_id


@pytest.fixture(params=[False, True], ids=["without-component", "with-component"])
async def either_client(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> HomeAssistantClient:
    """``core_client``, and the same with the component's commands registered."""
    if request.param:
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    return core_client


async def core_refusal(hass: HomeAssistant, service: str, **data: Any) -> str:
    """The text Core refuses ``update.<service>`` with, called in process."""
    with pytest.raises(HomeAssistantError) as refusal:
        await hass.services.async_call("update", service, data, blocking=True)
    return str(refusal.value)


def supervisor_info(channel: UpdateChannel) -> dict[str, Any]:
    """``GET /supervisor/info`` as Supervisor answers it on ``channel``, built
    from aiohasupervisor's model."""
    return SupervisorInfo(
        version="2026.11.0",
        version_latest="2026.11.0",
        update_available=False,
        channel=channel,
        arch="amd64",
        supported=True,
        healthy=True,
        ip_address=IPv4Address("172.30.32.2"),
        timezone="UTC",
        logging=LogLevel.INFO,
        debug=False,
        debug_block=False,
        diagnostics=None,
        auto_update=True,
        country=None,
        detect_blocking_io=False,
        feature_flags={},
    ).to_dict()


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
        # Listings GitHub pages through, by URL without the query.
        self.listings: dict[str, list[dict[str, Any]]] = {}
        self.unreachable: set[str] = set()
        self.requested: list[str] = []

    def serve(self, url: str, status: int = 200, **response: Any) -> None:
        """Answer ``url``, with or without a query, with
        ``httpx.Response(status, **response)``."""
        self.pages[url] = {"status_code": status, **response}

    def answer(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        if url in self.unreachable:
            raise httpx.ConnectError("upstream unreachable", request=request)
        path = url.partition("?")[0]
        if path in self.listings:
            return httpx.Response(200, json=_page(self.listings[path], request))
        page = self.pages.get(
            url, self.pages.get(path, self.hosts.get(request.url.host))
        )
        if page is None:
            return httpx.Response(404)
        return httpx.Response(**{"status_code": 200, **page})

    def listing_pages(self, url: str) -> list[int]:
        """The pages of the listing at ``url`` that were asked for, in order."""
        return [
            int(httpx.URL(u).params.get("page", "1"))
            for u in self.requested
            if u.partition("?")[0] == url
        ]


def _page(rows: list[dict[str, Any]], request: httpx.Request) -> list[dict[str, Any]]:
    """The page of ``rows`` a request asks for, as GitHub pages a listing
    (30 rows unless ``per_page`` says otherwise)."""
    per_page = int(request.url.params.get("per_page", "30"))
    page = int(request.url.params.get("page", "1"))
    return rows[(page - 1) * per_page : page * per_page]


@pytest.fixture
def web() -> Iterator[Web]:
    """The upstreams, served from a ``Web``; Core's own address passes through."""
    pages = Web()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="127.0.0.1").pass_through()
        router.route().mock(side_effect=pages.answer)
        yield pages
