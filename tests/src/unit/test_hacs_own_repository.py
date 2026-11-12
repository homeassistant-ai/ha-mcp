"""ha-mcp's own integration stays on the one store repository that owns it.

Regression for #2993: the install guidance names the main repository, and on
HACS a download of it on top of a mirror install left two records claiming
``custom_components/ha_mcp_tools``; the Marketplace then refused to update
either. A download of either of ha-mcp's repositories now goes to the one
that is installed, and two installed records are refused with the way out.

The Marketplace is Core's own, set up on a real ``hass``; its refusals are
Core's replies to Core's own exceptions (``_hacs_marketplace``). HACS, the custom
integration of Home Assistant 2026.10 and older, is a mock integration.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.components.marketplace.exceptions import MarketplaceError
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.hacs_backend import HACS_API, StoreApi
from ha_mcp.tools.hacs_own_repository import route_own_repository_download
from ha_mcp.tools.tools_hacs import HacsTools

from ._hacs_marketplace import (
    MAIN,
    MAIN_ID,
    MIRROR,
    MIRROR_ID,
    HacsIntegration,
    core_install,
    install_hacs,
    pooled_socket,
    sent_commands,
    set_up_marketplace,
    stored,
)

MAIN_ROW = {"id": MAIN_ID, "full_name": MAIN, "name": "HA-MCP"}
MIRROR_ROW = {"id": MIRROR_ID, "full_name": MIRROR, "name": "HA-MCP"}
OTHER_ROW = {"id": "441028036", "full_name": "piitaya/lovelace-mushroom", "name": "M"}

HUE_FORK_ID = "700000001"
OWN = "ha_mcp_tools"
DOUBLED = {
    MAIN_ID: stored(MAIN, "integration", installed="v9.1.1", domain=OWN),
    MIRROR_ID: stored(MIRROR, "integration", installed="v9.1.0", domain=OWN),
    HUE_FORK_ID: stored("fork/hue", "integration", domain="hue_fork"),
    "700000002": stored(
        "someone/hue", "integration", installed="v1", domain="hue_fork"
    ),
}


def _installed(row: dict[str, Any], version: str = "v9.1.0") -> dict[str, Any]:
    return {**row, "installed": True, "installed_version": version}


def _owned(directory: str, owner: str) -> MarketplaceError:
    """Core's refusal of an install whose directory another record owns."""
    return MarketplaceError(
        translation_domain="marketplace",
        translation_key="integration_owned",
        translation_placeholders={"domain": directory, "owner": owner},
    )


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """The Marketplace installs into the configuration directory."""
    return hass_tmp_config_dir


@pytest.fixture
async def socket(core_client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled_socket(core_client)


@pytest.fixture
def hacs(hass: HomeAssistant, core_client: HomeAssistantClient) -> HacsIntegration:
    """HACS with no repositories yet; each test lists its own."""
    store = HacsIntegration(repositories=[])
    install_hacs(hass, store)
    return store


@pytest.fixture
async def doubled(
    hass: HomeAssistant,
    socket: HomeAssistantWebSocketClient,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> AsyncIterator[MockConfigEntry]:
    """The Marketplace with both of ha-mcp's repositories installed (#2993)."""
    entry = await set_up_marketplace(hass, aioclient_mock, hass_storage, DOUBLED)
    await socket.send_command("marketplace/warning/accept")
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)


async def _download_error(**arguments: Any) -> dict[str, Any]:
    tools: HacsTools = arguments.pop("tools")
    with pytest.raises(ToolError) as excinfo:
        await tools.ha_manage_hacs(action="download", **arguments)
    body: dict[str, Any] = json.loads(str(excinfo.value))
    return body


@pytest.mark.parametrize("requested", ["piitaya/x", "441028036"])
async def test_other_repositories_pass_through_without_a_store_call(
    hacs: HacsIntegration, socket: HomeAssistantWebSocketClient, requested: str
) -> None:
    """Another repository, by name or id, costs no store call: its download
    stays one store command."""
    hacs.repositories = [OTHER_ROW, _installed(MIRROR_ROW)]
    with sent_commands(socket) as spy:
        routed = await route_own_repository_download(socket, HACS_API, requested, None)
    assert routed == (requested, None, None)
    spy.assert_not_awaited()


@pytest.mark.parametrize(
    ("requested", "installed", "owner"),
    [
        (MAIN, MIRROR_ROW, MIRROR),
        (MIRROR, MAIN_ROW, MAIN),
        (MAIN_ID, MIRROR_ROW, MIRROR),
    ],
)
async def test_the_installed_repository_wins(
    hacs: HacsIntegration,
    socket: HomeAssistantWebSocketClient,
    requested: str,
    installed: dict[str, Any],
    owner: str,
) -> None:
    hacs.repositories = [MAIN_ROW, MIRROR_ROW, OTHER_ROW, _installed(installed)]
    target, version, note = await route_own_repository_download(
        socket, HACS_API, requested, "v9.1.1"
    )
    assert (target, version) == (owner, "v9.1.1")
    assert note is not None
    assert owner in note


@pytest.mark.parametrize(
    "repositories",
    [
        pytest.param([_installed(MAIN_ROW), MIRROR_ROW], id="requested-installed"),
        pytest.param([MAIN_ROW, MIRROR_ROW], id="nothing-installed"),
    ],
)
async def test_the_requested_repository_is_kept_when_nothing_else_owns_it(
    hacs: HacsIntegration,
    socket: HomeAssistantWebSocketClient,
    repositories: list[dict[str, Any]],
) -> None:
    hacs.repositories = repositories
    assert await route_own_repository_download(socket, HACS_API, MAIN, None) == (
        MAIN,
        None,
        None,
    )


async def test_two_installed_records_on_the_marketplace_leave_the_decision_to_the_store(
    doubled: MockConfigEntry, socket: HomeAssistantWebSocketClient
) -> None:
    """The Marketplace refuses the download itself; no handwritten copy of
    its rule stands in front of it."""
    routed = await route_own_repository_download(
        socket, StoreApi("marketplace"), MAIN, None
    )
    assert routed == (MAIN, None, None)


async def test_a_refused_download_on_a_doubled_marketplace_carries_the_way_out(
    doubled: MockConfigEntry, core_client: HomeAssistantClient
) -> None:
    """The store's ownership refusal on a doubled install names an owner that
    cannot be updated either, so the suggestions are the recovery routes, and
    the tool looks up the installed records behind the refusal."""
    with core_install(side_effect=_owned(OWN, MIRROR)):
        body = await _download_error(tools=HacsTools(core_client), repository_id=MAIN)

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
        body = await _download_error(
            tools=HacsTools(core_client), repository_id=HUE_FORK_ID
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
        body = await _download_error(
            tools=HacsTools(core_client), repository_id=HUE_FORK_ID
        )

    assert "belongs to nobody" in body["error"]["message"]
    assert "already provides" not in json.dumps(body)


async def test_download_of_the_main_repository_installs_from_the_mirror(
    hacs: HacsIntegration, core_client: HomeAssistantClient
) -> None:
    """The server's own recipe no longer creates the second record."""
    hacs.repositories = [MAIN_ROW, _installed(MIRROR_ROW)]

    result = await HacsTools(core_client).ha_manage_hacs(
        action="download", repository_id=MAIN
    )

    downloads = [m for m in hacs.received if m["type"] == "hacs/repository/download"]
    assert [m["repository"] for m in downloads] == [MIRROR_ID]
    assert result["success"] is True
    assert any("rerouted" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    ("requested", "installed", "version", "expected"),
    [
        (MAIN_ROW, MIRROR_ROW, "v9.1.2.dev3397", "v9.1.2-dev.3397"),
        (MIRROR_ROW, MAIN_ROW, "v9.1.2-dev.3397", "v9.1.2.dev3397"),
        (MAIN_ROW, MIRROR_ROW, "v9.1.1", "v9.1.1"),
    ],
)
async def test_a_development_ref_is_renamed_for_the_target(
    hacs: HacsIntegration,
    socket: HomeAssistantWebSocketClient,
    requested: dict[str, Any],
    installed: dict[str, Any],
    version: str,
    expected: str,
) -> None:
    """The repositories tag dev builds differently, so a rerouted dev request
    names the build the way the installed repository tags it."""
    hacs.repositories = [requested, _installed(installed)]
    target, target_version, note = await route_own_repository_download(
        socket, HACS_API, requested["full_name"], version
    )
    assert target == installed["full_name"]
    assert target_version == expected
    assert note is not None
    assert (expected in note) == (expected != version)


async def test_a_failed_list_lookup_leaves_the_request_alone(
    hacs: HacsIntegration, socket: HomeAssistantWebSocketClient
) -> None:
    """The store's list failing is the download's problem to report, with the
    download's own context; the lookup never speaks for it."""
    hacs.refusals["hacs/repositories/list"] = "kaboom"
    assert await route_own_repository_download(socket, HACS_API, MAIN, None) == (
        MAIN,
        None,
        None,
    )
    assert hacs.commands() == ["hacs/repositories/list"]


async def test_a_release_the_target_tags_differently_asks_for_its_own_tag(
    hacs: HacsIntegration, socket: HomeAssistantWebSocketClient
) -> None:
    """Before 9.0 the mirror tagged releases with the component's numbers, so
    a request for such a version cannot be renamed; it is refused with the
    way to list the installed repository's tags instead of a store 404."""
    hacs.repositories = [MAIN_ROW, _installed(MIRROR_ROW)]
    with pytest.raises(ToolError) as excinfo:
        await route_own_repository_download(socket, HACS_API, MAIN, "v8.6.0")
    error = json.loads(str(excinfo.value))["error"]
    assert "v8.6.0" in error["message"]
    assert any("include_releases=True" in s for s in error["suggestions"])


async def test_two_installed_records_still_download_on_hacs(
    hacs: HacsIntegration, socket: HomeAssistantWebSocketClient
) -> None:
    """HACS never checked ownership, so a doubled install on Home Assistant
    2026.10 can still pull the release whose repair keeps one listing; the
    request passes through with the warning instead of a refusal."""
    hacs.repositories = [_installed(MAIN_ROW), _installed(MIRROR_ROW)]
    target, version, note = await route_own_repository_download(
        socket, HACS_API, MAIN, None
    )
    assert (target, version) == (MAIN, None)
    assert note is not None
    assert "Marketplace" in note and "HACS still updates" in note
    assert MAIN in note and "Repairs" in note
    assert "restart" in note.lower()
    # The downloaded release may predate the repair: the way out is named too.
    assert "manual route in the FAQ" in note
