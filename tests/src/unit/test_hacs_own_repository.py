"""ha-mcp's own integration stays on the one store repository that owns it.

Regression for #2993: the install guidance names the main repository, and an
install of it on top of a mirror install left two records claiming
``custom_components/ha_mcp_tools``; the Marketplace then refuses to update
either. A download of either of ha-mcp's repositories goes to the one that is
installed, and two installed records are refused with the way out.

The Marketplace is Core's own, set up on a real ``hass``; its refusals are
Core's replies to Core's own exceptions (``_hacs_marketplace``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from homeassistant.components.marketplace.exceptions import MarketplaceError
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    DEFAULT_COMMAND_WAIT_TIMEOUT,
    HomeAssistantWebSocketClient,
)
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
    store_commands,
    stored,
    suggestions,
)
from ._held_command import held_command
from ._tool_errors import rejected_call_body
from ._tools_addons_core import outwait, timers

HUE_FORK_ID = "700000001"
OWN = "ha_mcp_tools"
LIST = "marketplace/repositories/list"
INSTALL = "marketplace/repository/install"

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
def tools(core_client: HomeAssistantClient) -> HacsTools:
    return HacsTools(core_client)


@pytest.fixture
async def marketplace(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    socket: HomeAssistantWebSocketClient,
) -> AsyncIterator[OpenMarketplace]:
    """Sets up Core's Marketplace with the repositories a test stores, its
    first-run warning accepted."""
    entries: list[MockConfigEntry] = []

    async def open_marketplace(repositories: Repositories) -> MockConfigEntry:
        entry = await set_up_marketplace(
            hass, aioclient_mock, hass_storage, repositories
        )
        entries.append(entry)
        await socket.send_command("marketplace/warning/accept")
        return entry

    yield open_marketplace
    for entry in entries:
        await hass.config_entries.async_unload(entry.entry_id)


async def _installed(
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
    requested: str,
    version: str | None = None,
) -> tuple[list[str], list[str], Any, dict[str, Any]]:
    """Download ``requested``: the commands sent, the ids installed, the ref
    Core's install got, and the result."""
    with sent_commands(socket) as spy, core_install() as install:
        result = await tools.ha_manage_hacs(
            action="download", repository_id=requested, version=version
        )
    assert install.await_args is not None
    return (
        store_commands(spy),
        [sent["repository"] for sent in sent_with(spy, INSTALL)],
        install.await_args.kwargs["ref"],
        result,
    )


@pytest.mark.parametrize(
    ("requested", "commands"),
    [(CARD_ID, [INSTALL]), ("piitaya/lovelace-mushroom", [LIST, INSTALL])],
)
async def test_other_repositories_cost_no_extra_store_call(
    marketplace: OpenMarketplace,
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
    requested: str,
    commands: list[str],
) -> None:
    """Another repository is installed as asked: an id is one command, a name
    only adds the lookup of its id."""
    await marketplace(_own_records(installed=MIRROR))
    sent, installed, _, _ = await _installed(tools, socket, requested)
    assert sent == commands
    assert installed == [CARD_ID]


@pytest.mark.parametrize(
    ("requested", "owner", "owner_id"),
    [(MAIN, MIRROR, MIRROR_ID), (MIRROR, MAIN, MAIN_ID), (MAIN_ID, MIRROR, MIRROR_ID)],
)
async def test_the_installed_repository_wins(
    marketplace: OpenMarketplace,
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
    requested: str,
    owner: str,
    owner_id: str,
) -> None:
    await marketplace(_own_records(installed=owner))
    _, installed, ref, result = await _installed(tools, socket, requested, "v9.1.1")
    assert (installed, ref) == ([owner_id], "v9.1.1")
    [note] = result["warnings"]
    assert "rerouted" in note
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
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
    installed: str | None,
) -> None:
    await marketplace(_own_records(installed))
    _, ids, _, result = await _installed(tools, socket, MAIN)
    assert ids == [MAIN_ID]
    assert "warnings" not in result


async def test_two_installed_records_leave_the_decision_to_the_store(
    marketplace: OpenMarketplace,
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """The Marketplace refuses the download itself; no handwritten copy of
    its rule stands in front of it."""
    await marketplace(DOUBLED)
    _, ids, _, result = await _installed(tools, socket, MAIN)
    assert ids == [MAIN_ID]
    assert "warnings" not in result


async def test_a_refused_download_on_a_doubled_install_carries_the_way_out(
    marketplace: OpenMarketplace, tools: HacsTools
) -> None:
    """The store's ownership refusal on a doubled install names an owner that
    cannot be updated either, so the suggestions are the recovery routes, and
    the tool looks up the installed records behind the refusal."""
    await marketplace(DOUBLED)
    with core_install(side_effect=_owned(OWN, MIRROR)):
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="download", repository_id=MAIN)
        )

    hints = " ".join(suggestions(body))
    assert "Repairs" in hints
    assert "Devices & services" in hints
    assert f'action="download", repository_id="{MIRROR}"' not in hints
    assert sorted(body["installed"]) == [MAIN, MIRROR]
    assert body["translation_key"] == "integration_owned"


async def test_another_integrations_refusal_names_its_owner_to_update(
    marketplace: OpenMarketplace, tools: HacsTools
) -> None:
    """A download refused because another installed record owns the
    directory points at that owner. Both HA-MCP listings are installed, but
    the refusal is about some other integration, so HA-MCP's repair routes
    would not help."""
    await marketplace(DOUBLED)
    with core_install(side_effect=_owned("hue_fork", "someone/hue")):
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="download", repository_id=HUE_FORK_ID)
        )

    hints = " ".join(suggestions(body))
    assert 'ha_manage_hacs(action="download", repository_id="someone/hue")' in hints
    assert "Repairs" not in hints


async def test_a_message_merely_containing_the_words_is_not_an_ownership_refusal(
    marketplace: OpenMarketplace, tools: HacsTools
) -> None:
    """A library error inside Core's install-failed sentence that happens to
    say a directory belongs to someone."""
    await marketplace(DOUBLED)
    library_error = MarketplaceError("The hue_fork directory belongs to nobody.")
    with core_install(side_effect=library_error):
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="download", repository_id=HUE_FORK_ID)
        )

    assert "belongs to nobody" in body["error"]["message"]
    assert "already provides" not in json.dumps(body)


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
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
    requested: str,
    installed: str,
    version: str,
    expected: str,
) -> None:
    """The repositories tag dev builds differently, so a rerouted dev request
    names the build the way the installed repository tags it."""
    await marketplace(_own_records(installed))
    _, _, ref, result = await _installed(tools, socket, requested, version)
    assert ref == expected
    [note] = result["warnings"]
    assert (expected in note) == (expected != version)


async def test_a_list_lookup_without_an_answer_leaves_the_request_alone(
    hass: HomeAssistant,
    marketplace: OpenMarketplace,
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """The download reports the store's trouble with its own context; the
    lookup never speaks for it. The mirror is installed, so an answered
    lookup would have rerouted the request."""
    await marketplace(_own_records(installed=MIRROR))
    before = timers(hass)
    with sent_commands(socket) as spy, core_install():
        async with held_command(hass, LIST, first_only=True):
            call = asyncio.create_task(
                tools.ha_manage_hacs(action="download", repository_id=MAIN_ID)
            )
            await outwait(
                hass,
                DEFAULT_COMMAND_WAIT_TIMEOUT - 1,
                DEFAULT_COMMAND_WAIT_TIMEOUT,
                before,
            )
            await asyncio.wait([call])
    assert call.result()["success"] is True
    assert [sent["repository"] for sent in sent_with(spy, INSTALL)] == [MAIN_ID]


async def test_a_release_the_target_tags_differently_asks_for_its_own_tag(
    marketplace: OpenMarketplace,
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """Before 9.0 the mirror tagged releases with the component's numbers, so
    a request for such a version cannot be renamed; it is refused with the
    way to list the installed repository's tags instead of a store 404."""
    await marketplace(_own_records(installed=MIRROR))
    with sent_commands(socket) as spy:
        body = await rejected_call_body(
            tools.ha_manage_hacs(
                action="download", repository_id=MAIN, version="v8.6.0"
            )
        )
    assert "v8.6.0" in body["error"]["message"]
    assert any("include_releases=True" in s for s in suggestions(body))
    assert INSTALL not in store_commands(spy)
