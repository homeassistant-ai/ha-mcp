"""ha-mcp's own integration stays on the one store repository that owns it.

Regression for #2993: the install guidance names the main repository, and on
HACS a download of it on top of a mirror install left two records claiming
``custom_components/ha_mcp_tools``; the Marketplace then refused to update
either. A download of either of ha-mcp's repositories now goes to the one
that is installed, and two installed records are refused with the way out.

The Marketplace is Core's own, set up on a real ``hass``; its refusals are
Core's replies to Core's own exceptions (``_hacs_marketplace``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.marketplace.exceptions import MarketplaceError
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandTimeout
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.hacs_backend import StoreApi
from ha_mcp.tools.hacs_own_repository import route_own_repository_download
from ha_mcp.tools.tools_hacs import HacsTools

from ._client_spies import sent_commands
from ._hacs_marketplace import (
    CARD_ID,
    MAIN,
    MAIN_ID,
    MIRROR,
    MIRROR_ID,
    core_install,
    pooled_socket,
    sent_with,
    set_up_marketplace,
    stored,
)
from ._tool_errors import error_of, rejected_call_body

MARKETPLACE = StoreApi("marketplace")
HUE_FORK_ID = "700000001"
OWN = "ha_mcp_tools"

type Repositories = dict[str, dict[str, Any]]
type OpenMarketplace = Callable[[Repositories], Awaitable[MockConfigEntry]]


def _own_records(installed: str | None) -> Repositories:
    """Both of ha-mcp's repositories, ``installed`` the one installed, and a card."""
    return {
        repository_id: stored(
            full_name,
            "integration",
            installed="v9.1.0" if full_name == installed else None,
            domain=OWN,
        )
        for repository_id, full_name in ((MAIN_ID, MAIN), (MIRROR_ID, MIRROR))
    } | {CARD_ID: stored("piitaya/lovelace-mushroom", "plugin")}


DOUBLED = {
    MAIN_ID: stored(MAIN, "integration", installed="v9.1.1", domain=OWN),
    MIRROR_ID: stored(MIRROR, "integration", installed="v9.1.0", domain=OWN),
    HUE_FORK_ID: stored("fork/hue", "integration", domain="hue_fork"),
    "700000002": stored(
        "someone/hue", "integration", installed="v1", domain="hue_fork"
    ),
}


def _owned(directory: str, owner: str) -> MarketplaceError:
    """Core's refusal of an install whose directory another record owns."""
    return MarketplaceError(
        translation_domain="marketplace",
        translation_key="integration_owned",
        translation_placeholders={"domain": directory, "owner": owner},
    )


@pytest.fixture
async def socket(core_client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled_socket(core_client)


@pytest.fixture
async def marketplace(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> AsyncIterator[OpenMarketplace]:
    """Sets up Core's Marketplace with the repositories a test stores."""
    entries: list[MockConfigEntry] = []

    async def open_marketplace(repositories: Repositories) -> MockConfigEntry:
        entry = await set_up_marketplace(
            hass, aioclient_mock, hass_storage, repositories
        )
        entries.append(entry)
        return entry

    yield open_marketplace
    for entry in entries:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
async def doubled(
    marketplace: OpenMarketplace, socket: HomeAssistantWebSocketClient
) -> MockConfigEntry:
    """The Marketplace with both of ha-mcp's repositories installed (#2993)."""
    entry = await marketplace(DOUBLED)
    await socket.send_command("marketplace/warning/accept")
    return entry


@pytest.mark.parametrize("requested", ["piitaya/x", CARD_ID])
async def test_other_repositories_pass_through_without_a_store_call(
    marketplace: OpenMarketplace,
    socket: HomeAssistantWebSocketClient,
    requested: str,
) -> None:
    """Another repository, by name or id, costs no store call: its download
    stays one store command."""
    await marketplace(_own_records(installed=MIRROR))
    with sent_commands(socket) as spy:
        routed = await route_own_repository_download(
            socket, MARKETPLACE, requested, None
        )
    assert routed == (requested, None, None)
    spy.assert_not_awaited()


@pytest.mark.parametrize(
    ("requested", "owner"), [(MAIN, MIRROR), (MIRROR, MAIN), (MAIN_ID, MIRROR)]
)
async def test_the_installed_repository_wins(
    marketplace: OpenMarketplace,
    socket: HomeAssistantWebSocketClient,
    requested: str,
    owner: str,
) -> None:
    await marketplace(_own_records(installed=owner))
    target, version, note = await route_own_repository_download(
        socket, MARKETPLACE, requested, "v9.1.1"
    )
    assert (target, version) == (owner, "v9.1.1")
    assert note is not None
    assert owner in note


@pytest.mark.parametrize(
    "installed",
    [
        pytest.param(MAIN, id="requested-installed"),
        pytest.param(None, id="nothing-installed"),
    ],
)
async def test_the_requested_repository_is_kept_when_nothing_else_owns_it(
    marketplace: OpenMarketplace,
    socket: HomeAssistantWebSocketClient,
    installed: str | None,
) -> None:
    await marketplace(_own_records(installed))
    assert await route_own_repository_download(socket, MARKETPLACE, MAIN, None) == (
        MAIN,
        None,
        None,
    )


async def test_two_installed_records_on_the_marketplace_leave_the_decision_to_the_store(
    doubled: MockConfigEntry, socket: HomeAssistantWebSocketClient
) -> None:
    """The Marketplace refuses the download itself; no handwritten copy of
    its rule stands in front of it."""
    routed = await route_own_repository_download(socket, MARKETPLACE, MAIN, None)
    assert routed == (MAIN, None, None)


async def test_a_refused_download_on_a_doubled_marketplace_carries_the_way_out(
    doubled: MockConfigEntry, core_client: HomeAssistantClient
) -> None:
    """The store's ownership refusal on a doubled install names an owner that
    cannot be updated either, so the suggestions are the recovery routes, and
    the tool looks up the installed records behind the refusal."""
    with core_install(side_effect=_owned(OWN, MIRROR)):
        body = await rejected_call_body(
            HacsTools(core_client).ha_manage_hacs(action="download", repository_id=MAIN)
        )

    suggestions = " ".join(body["error"]["suggestions"])
    assert "Repairs" in suggestions
    assert "Devices & services" in suggestions
    assert f'action="download", repository_id="{MIRROR}"' not in suggestions
    assert sorted(body["installed"]) == [MAIN, MIRROR]


async def test_another_integrations_refusal_names_its_owner_to_update(
    doubled: MockConfigEntry, core_client: HomeAssistantClient
) -> None:
    """A download refused because another installed record owns the
    directory points at that owner, not at a generic failure. Both HA-MCP
    listings are installed, but the refusal is about some other
    integration, so HA-MCP's repair routes would not help; and Core's
    sentence ends in a period that is not part of the owner's name."""
    with core_install(side_effect=_owned("hue_fork", "someone/hue")):
        body = await rejected_call_body(
            HacsTools(core_client).ha_manage_hacs(
                action="download", repository_id=HUE_FORK_ID
            )
        )

    suggestions = " ".join(body["error"]["suggestions"])
    assert (
        'ha_manage_hacs(action="download", repository_id="someone/hue")' in suggestions
    )
    assert "Repairs" not in suggestions


async def test_a_message_merely_containing_the_words_is_not_an_ownership_refusal(
    doubled: MockConfigEntry, core_client: HomeAssistantClient
) -> None:
    """A library error inside Core's install-failed sentence that happens to
    say a directory belongs to someone."""
    library_error = MarketplaceError("The hue_fork directory belongs to nobody.")
    with core_install(side_effect=library_error):
        body = await rejected_call_body(
            HacsTools(core_client).ha_manage_hacs(
                action="download", repository_id=HUE_FORK_ID
            )
        )

    assert "belongs to nobody" in body["error"]["message"]
    assert "already provides" not in json.dumps(body)


async def test_download_of_the_main_repository_installs_from_the_mirror(
    marketplace: OpenMarketplace,
    socket: HomeAssistantWebSocketClient,
    core_client: HomeAssistantClient,
) -> None:
    """The server's own recipe no longer creates the second record."""
    await marketplace(_own_records(installed=MIRROR))
    await socket.send_command("marketplace/warning/accept")

    with sent_commands(socket) as spy, core_install():
        result = await HacsTools(core_client).ha_manage_hacs(
            action="download", repository_id=MAIN
        )

    installs = sent_with(spy, "marketplace/repository/install")
    assert [sent["repository"] for sent in installs] == [MIRROR_ID]
    assert result["success"] is True
    assert any("rerouted" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    ("requested", "installed", "version", "expected"),
    [
        (MAIN, MIRROR, "v9.1.2.dev3397", "v9.1.2-dev.3397"),
        (MIRROR, MAIN, "v9.1.2-dev.3397", "v9.1.2.dev3397"),
        (MAIN, MIRROR, "v9.1.1", "v9.1.1"),
    ],
)
async def test_a_development_ref_is_renamed_for_the_target(
    marketplace: OpenMarketplace,
    socket: HomeAssistantWebSocketClient,
    requested: str,
    installed: str,
    version: str,
    expected: str,
) -> None:
    """The repositories tag dev builds differently, so a rerouted dev request
    names the build the way the installed repository tags it."""
    await marketplace(_own_records(installed))
    target, target_version, note = await route_own_repository_download(
        socket, MARKETPLACE, requested, version
    )
    assert target == installed
    assert target_version == expected
    assert note is not None
    assert (expected in note) == (expected != version)


async def test_a_failed_list_lookup_leaves_the_request_alone(
    marketplace: OpenMarketplace, socket: HomeAssistantWebSocketClient
) -> None:
    """The store's list failing is the download's problem to report, with the
    download's own context; the lookup never speaks for it. The mirror is
    installed, so an answered lookup would have rerouted the request."""
    await marketplace(_own_records(installed=MIRROR))
    with patch.object(
        socket,
        "send_command",
        AsyncMock(side_effect=HomeAssistantCommandTimeout("Command timeout")),
    ) as no_answer:
        routed = await route_own_repository_download(socket, MARKETPLACE, MAIN, None)
    assert routed == (MAIN, None, None)
    no_answer.assert_awaited_once_with(MARKETPLACE.command("list"))


async def test_a_release_the_target_tags_differently_asks_for_its_own_tag(
    marketplace: OpenMarketplace, socket: HomeAssistantWebSocketClient
) -> None:
    """Before 9.0 the mirror tagged releases with the component's numbers, so
    a request for such a version cannot be renamed; it is refused with the
    way to list the installed repository's tags instead of a store 404."""
    await marketplace(_own_records(installed=MIRROR))
    with pytest.raises(ToolError) as excinfo:
        await route_own_repository_download(socket, MARKETPLACE, MAIN, "v8.6.0")
    error = error_of(excinfo)
    assert "v8.6.0" in error["message"]
    assert any("include_releases=True" in s for s in error["suggestions"])
