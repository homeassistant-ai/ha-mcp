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
from ha_mcp.tools.hacs_backend import StoreApi
from ha_mcp.tools.hacs_own_repository import route_own_repository_download
from ha_mcp.tools.tools_hacs import HacsTools

from ._hacs_marketplace import (
    MAIN,
    MAIN_ID,
    MIRROR,
    MIRROR_ID,
    core_install,
    pooled_socket,
    set_up_marketplace,
    stored,
)

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
