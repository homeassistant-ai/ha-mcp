"""The Thread/OTBR handler behind ``ha_manage_radio``, and the ``thread_network``
section of ``ha_get_system_health``, on real Home Assistant Core.

Core's own ``thread`` and ``otbr`` integrations run here and answer every
``thread/*`` and ``otbr/*`` command. Only the border router itself is replaced,
as Core's own otbr tests replace it: the ``python_otbr_api.OTBR`` methods that
talk to the router's REST API return what a router reports, and mDNS runs on
Core's mocked zeroconf. The dataset TLVs are device data, taken from Core's
otbr and thread tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import python_otbr_api
from homeassistant.components import otbr, thread
from homeassistant.components.thread.dataset_store import DatasetStore, async_get_store
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_radio import RadioTools
from ha_mcp.tools.tools_system import SystemTools

from ._client_spies import sent_messages

# A border router reached over its REST API; not the multiprotocol app's
# host, so Core lets it change channel.
OTBR_URL = "http://core-openthread-border-router:8081"
EXTENDED_ADDRESS = bytes.fromhex("AEEB2F594B570BBF")
BORDER_AGENT_ID = bytes.fromhex("230C6A1AC57F6F4BE262ACF32E5EF52C")
# The network the border router runs: channel 15, extended PAN F642646DA209B1D0.
ROUTER_DATASET = (
    "0E080000000000010000000300000F35060004001FFFE00208F642646DA209B1D00708FDF57B5A"
    "0FE2AAF60510DE98B5BA1A528FEE049D4B4B01835375030D4F70656E5468726561642048410102"
    "25A40410F5DD18371BFD29E1A601EF6FFAD94C030C0402A0F7F8"
)
# Another network the user shared with Home Assistant.
OTHER_DATASET = (
    "0E080000000000010000000300000F35060004001FFFE0020811111111222222330708FDAD70BF"
    "E5AA15DD051000112233445566778899AABBCCDDEEFF030E486f6d65417373697374616e742101"
    "0212340410445F2B5CA6F2A93A55CE570A70EFEECB0C0402A0F7F8"
)


@pytest.fixture(autouse=True)
def use_mocked_zeroconf(mock_async_zeroconf: MagicMock) -> None:
    """Thread's router discovery runs on Core's mocked zeroconf."""


@pytest.fixture
def radio(core_client: HomeAssistantClient) -> RadioTools:
    return RadioTools(core_client)


@pytest.fixture
def border_router() -> Iterator[dict[str, AsyncMock]]:
    """The border router's REST API, as ``python_otbr_api`` reads it."""
    device = {
        "get_border_agent_id": AsyncMock(return_value=BORDER_AGENT_ID),
        "get_extended_address": AsyncMock(return_value=EXTENDED_ADDRESS),
        "get_active_dataset_tlvs": AsyncMock(
            return_value=bytes.fromhex(ROUTER_DATASET)
        ),
        "get_active_dataset": AsyncMock(
            return_value=python_otbr_api.ActiveDataSet(
                channel=15, extended_pan_id="F642646DA209B1D0"
            )
        ),
        "get_coprocessor_version": AsyncMock(
            return_value="SL-OPENTHREAD/2.4.4.0_GitHub-7074a43e4; EFR32"
        ),
        "set_enabled": AsyncMock(return_value=None),
        "factory_reset": AsyncMock(return_value=None),
        "create_active_dataset": AsyncMock(return_value=None),
        "set_active_dataset_tlvs": AsyncMock(return_value=None),
        "set_channel": AsyncMock(return_value=None),
    }
    with patch.multiple(python_otbr_api.OTBR, **device):
        yield device


@pytest.fixture
async def dataset_store(hass: HomeAssistant) -> DatasetStore:
    """Core's thread integration holding the router's network and one other.

    The router's network is already known before the OTBR entry loads, so
    loading it does not start Core's 30-second preferred-network discovery.
    """
    assert await async_setup_component(hass, thread.DOMAIN, {})
    for tlv in (ROUTER_DATASET, OTHER_DATASET):
        await thread.async_add_dataset(hass, "Google", tlv)
    return await async_get_store(hass)


@pytest.fixture
async def otbr_entry_id(
    hass: HomeAssistant,
    dataset_store: DatasetStore,
    border_router: dict[str, AsyncMock],
    aioclient_mock: AiohttpClientMocker,
) -> str:
    """One OTBR config entry, loaded by Core; any unpatched REST call fails."""
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(
        domain=otbr.DOMAIN,
        data={"url": OTBR_URL},
        title="OpenThread Border Router",
        unique_id=BORDER_AGENT_ID.hex(),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    return entry.entry_id


def _dataset_id(store: DatasetStore, tlv: str) -> str:
    return next(entry.id for entry in store.datasets.values() if entry.tlv == tlv)


async def test_network_status_reports_the_otbr_entry_and_border_routers(
    radio: RadioTools, otbr_entry_id: str
) -> None:
    out = await radio.ha_manage_radio(radio="thread", action="network_status")

    assert out["success"] is True
    assert out["radio"] == "thread"
    assert out["config_entry_id"] == otbr_entry_id
    assert list(out["border_routers"]) == [EXTENDED_ADDRESS.hex()]


async def test_network_status_without_otbr_degrades_to_unavailable(
    hass: HomeAssistant, radio: RadioTools
) -> None:
    assert await async_setup_component(hass, "config", {})

    out = await radio.ha_manage_radio(radio="thread", action="network_status")

    assert out["success"] is True
    assert out["available"] is False
    assert out["warnings"]


async def test_list_datasets_returns_cores_datasets(
    radio: RadioTools, dataset_store: DatasetStore
) -> None:
    out = await radio.ha_manage_radio(radio="thread", action="list_datasets")

    listed = [dataset["dataset_id"] for dataset in out["datasets"]["datasets"]]
    assert listed == list(dataset_store.datasets)


@pytest.mark.parametrize(
    ("action", "params", "missing"),
    [
        ("set_preferred_dataset", {}, "dataset_id"),
        ("delete_dataset", {}, "dataset_id"),
        ("add_dataset", {"source": "Google"}, "tlv"),
        ("set_channel", {}, "channel"),
    ],
)
async def test_missing_required_param_is_refused_before_core_is_asked(
    core_client: HomeAssistantClient,
    radio: RadioTools,
    action: str,
    params: dict[str, Any],
    missing: str,
) -> None:
    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="thread", action=action, params=params, confirm=True
        )

    assert f"requires: {missing}" in str(exc.value)
    sent.assert_not_called()


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("delete_dataset", {"dataset_id": "d1"}),
        ("set_network", {"dataset_id": "d1"}),
        ("create_network", {}),
        ("set_channel", {"channel": 20}),
    ],
)
async def test_destructive_action_without_confirm_sends_nothing(
    core_client: HomeAssistantClient,
    radio: RadioTools,
    action: str,
    params: dict[str, Any],
) -> None:
    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(radio="thread", action=action, params=params)

    assert "confirm=True" in str(exc.value)
    sent.assert_not_called()


async def test_set_preferred_dataset_makes_cores_store_prefer_it(
    radio: RadioTools, dataset_store: DatasetStore
) -> None:
    dataset_id = _dataset_id(dataset_store, OTHER_DATASET)

    out = await radio.ha_manage_radio(
        radio="thread",
        action="set_preferred_dataset",
        params={"dataset_id": dataset_id},
    )

    assert out["success"] is True
    assert dataset_store.preferred_dataset == dataset_id


async def test_delete_dataset_removes_it_from_cores_store(
    radio: RadioTools, dataset_store: DatasetStore
) -> None:
    dataset_id = _dataset_id(dataset_store, OTHER_DATASET)

    out = await radio.ha_manage_radio(
        radio="thread",
        action="delete_dataset",
        params={"dataset_id": dataset_id},
        confirm=True,
    )

    assert out["success"] is True
    assert dataset_id not in dataset_store.datasets


async def test_discover_routers_starts_the_scan(
    hass: HomeAssistant, radio: RadioTools, mock_async_zeroconf: MagicMock
) -> None:
    assert await async_setup_component(hass, thread.DOMAIN, {})

    out = await radio.ha_manage_radio(radio="thread", action="discover_routers")

    assert out["success"] is True
    assert out["long_running"] is True
    mock_async_zeroconf.async_add_service_listener.assert_called_once()


async def test_add_dataset_stores_the_tlv_from_its_source(
    hass: HomeAssistant, radio: RadioTools
) -> None:
    assert await async_setup_component(hass, thread.DOMAIN, {})

    out = await radio.ha_manage_radio(
        radio="thread",
        action="add_dataset",
        params={"source": "Google", "tlv": OTHER_DATASET},
    )

    assert out["success"] is True
    store = await async_get_store(hass)
    [stored] = store.datasets.values()
    assert (stored.source, stored.tlv) == ("Google", OTHER_DATASET)


async def test_set_network_targets_the_only_border_router(
    radio: RadioTools,
    dataset_store: DatasetStore,
    border_router: dict[str, AsyncMock],
    otbr_entry_id: str,
) -> None:
    out = await radio.ha_manage_radio(
        radio="thread",
        action="set_network",
        params={"dataset_id": _dataset_id(dataset_store, OTHER_DATASET)},
        confirm=True,
    )

    assert out["extended_address"] == EXTENDED_ADDRESS.hex()
    border_router["set_active_dataset_tlvs"].assert_awaited_once_with(
        bytes.fromhex(OTHER_DATASET)
    )


async def test_set_network_without_otbr_raises(
    hass: HomeAssistant, core_client: HomeAssistantClient, radio: RadioTools
) -> None:
    # set_network is a write action: an absent OTBR must raise (not degrade
    # to a no-op success the way the read-only network_status does). Without
    # the integration Core does not know otbr/info.
    assert await async_setup_component(hass, "config", {})

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="thread",
            action="set_network",
            params={"dataset_id": "d1"},
            confirm=True,
        )

    assert "otbr integration or command is unavailable" in str(exc.value).lower()
    assert [call.args[0]["type"] for call in sent.await_args_list] == ["otbr/info"]


async def test_set_network_with_no_loaded_otbr_entry_raises(
    hass: HomeAssistant, core_client: HomeAssistantClient, radio: RadioTools
) -> None:
    # With the otbr integration set up but no entry loaded, Core's otbr/info
    # refuses with not_loaded rather than answering an empty map.
    assert await async_setup_component(hass, otbr.DOMAIN, {})

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="thread",
            action="set_network",
            params={"dataset_id": "d1"},
            confirm=True,
        )

    assert "otbr integration is not loaded" in str(exc.value).lower()
    assert [call.args[0]["type"] for call in sent.await_args_list] == ["otbr/info"]


async def test_create_network_with_explicit_address_skips_the_lookup(
    core_client: HomeAssistantClient,
    radio: RadioTools,
    border_router: dict[str, AsyncMock],
    otbr_entry_id: str,
) -> None:
    with sent_messages(core_client) as sent:
        out = await radio.ha_manage_radio(
            radio="thread",
            action="create_network",
            params={"extended_address": EXTENDED_ADDRESS.hex()},
            confirm=True,
        )

    assert out["extended_address"] == EXTENDED_ADDRESS.hex()
    border_router["create_active_dataset"].assert_awaited_once()
    assert [call.args[0]["type"] for call in sent.await_args_list] == [
        "otbr/create_network"
    ]


async def test_set_channel_reaches_the_border_router(
    radio: RadioTools, border_router: dict[str, AsyncMock], otbr_entry_id: str
) -> None:
    out = await radio.ha_manage_radio(
        radio="thread",
        action="set_channel",
        params={"channel": 20},
        confirm=True,
    )

    assert out["success"] is True
    assert out["extended_address"] == EXTENDED_ADDRESS.hex()
    border_router["set_channel"].assert_awaited_once_with(20, delay=ANY)


async def test_unknown_action_lists_supported(
    core_client: HomeAssistantClient, radio: RadioTools
) -> None:
    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(radio="thread", action="nope")

    assert "network_status" in str(exc.value)
    sent.assert_not_called()


async def _thread_network(hass: HomeAssistant, client: HomeAssistantClient) -> Any:
    assert await async_setup_component(hass, "system_health", {})
    result = await SystemTools(client).ha_get_system_health(include="thread_network")
    return result["thread_network"]


async def test_system_health_lists_datasets_and_routers_without_the_network_key(
    hass: HomeAssistant, core_client: HomeAssistantClient, otbr_entry_id: str
) -> None:
    section = await _thread_network(hass, core_client)

    assert not {"error", "datasets_error", "border_routers_error"} & set(section)
    assert (section["dataset_count"], section["border_router_count"]) == (2, 1)
    assert all({"preferred", "source"} <= set(d) for d in section["datasets"])
    router = section["border_routers"][0]
    assert router["extended_address"] == EXTENDED_ADDRESS.hex()
    assert router["url"] == OTBR_URL
    assert "active_dataset_tlvs" not in router


async def test_system_health_keeps_the_datasets_when_no_otbr_is_loaded(
    hass: HomeAssistant, core_client: HomeAssistantClient, dataset_store: DatasetStore
) -> None:
    section = await _thread_network(hass, core_client)

    assert "error" not in section
    assert section["dataset_count"] == 2
    assert (section["border_routers"], section["border_router_count"]) == ([], 0)
    assert "OTBR border routers not available" in section["border_routers_error"]


async def test_system_health_reports_both_refusals_without_thread(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    section = await _thread_network(hass, core_client)

    assert "error" not in section
    assert (section["dataset_count"], section["border_router_count"]) == (0, 0)
    assert "Thread datasets not available" in section["datasets_error"]
    assert "OTBR border routers not available" in section["border_routers_error"]
