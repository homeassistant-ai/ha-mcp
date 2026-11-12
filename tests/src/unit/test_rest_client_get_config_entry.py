"""``HomeAssistantClient.get_config_entry`` reads one entry over WebSocket.

Core's ``config_entries/get_single`` answers ``{"config_entry": fragment}`` for
one entry, or ``not_found``. Core has no REST read for one entry, so the REST
entry list stays as the fallback for the cases the WebSocket cannot serve: no
answer came back, or the token's user is not an admin (``get_single`` is
admin-only, the REST list is open to every user).

Every read goes to a real ``hass`` through the server's own client.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
)

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)

_REST_LOGGER = "ha_mcp.client.rest_client"


@pytest.fixture
async def config(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """Core's config-entry API, which serves both reads."""
    assert await async_setup_component(hass, "config", {})


def _add_entry(hass: HomeAssistant, domain: str = "sun") -> MockConfigEntry:
    entry = MockConfigEntry(domain=domain, title="Sun")
    entry.add_to_hass(hass)
    return entry


def _spies(client: HomeAssistantClient) -> Any:
    return (
        patch.object(
            client, "send_websocket_message", wraps=client.send_websocket_message
        ),
        patch.object(client, "list_config_entries", wraps=client.list_config_entries),
    )


def _get_single_levels(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.levelname
        for r in caplog.records
        if r.name == _REST_LOGGER and "get_single" in r.getMessage()
    ]


@pytest.fixture
async def read_only_client(
    core_client: HomeAssistantClient, hass_read_only_access_token: str
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client for a user who is not an admin."""
    client = HomeAssistantClient(
        base_url=core_client.base_url,
        token=hass_read_only_access_token,
        verify_ssl=False,
    )
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.usefixtures("config")
async def test_reads_the_entry_with_get_single(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    entry = _add_entry(hass)
    send_spy, list_spy = _spies(core_client)

    with send_spy as send, list_spy as listed:
        read = await core_client.get_config_entry(entry.entry_id)

    assert (read["entry_id"], read["domain"], read["title"]) == (
        entry.entry_id,
        "sun",
        "Sun",
    )
    send.assert_awaited_once_with(
        {"type": "config_entries/get_single", "entry_id": entry.entry_id},
        expected_error_codes=frozenset({"not_found", "unauthorized"}),
    )
    listed.assert_not_awaited()


@pytest.mark.usefixtures("config")
async def test_not_found_is_the_404_callers_expect_and_not_an_error_log(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _add_entry(hass)
    _send_spy, list_spy = _spies(core_client)

    with (
        list_spy as listed,
        caplog.at_level(logging.DEBUG, logger=_REST_LOGGER),
        pytest.raises(HomeAssistantAPIError) as exc,
    ):
        await core_client.get_config_entry("missing")

    assert exc.value.status_code == 404
    assert str(exc.value) == "Config entry not found: missing"
    listed.assert_not_awaited()
    assert _get_single_levels(caplog) == ["DEBUG"]


@pytest.mark.usefixtures("config")
async def test_no_websocket_answer_falls_back_to_the_rest_list(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    entry = _add_entry(hass)
    _add_entry(hass, "moon")
    _send_spy, list_spy = _spies(core_client)

    with (
        patch.object(
            core_client,
            "send_websocket_message",
            side_effect=HomeAssistantConnectionError("closed"),
        ),
        list_spy as listed,
    ):
        read = await core_client.get_config_entry(entry.entry_id)
        with pytest.raises(HomeAssistantAPIError) as exc:
            await core_client.get_config_entry("missing")

    assert read["entry_id"] == entry.entry_id
    assert listed.await_count == 2
    # The REST fallback still reports a missing entry as the 404.
    assert exc.value.status_code == 404


@pytest.mark.usefixtures("config")
async def test_non_admin_token_falls_back_to_the_rest_list(
    hass: HomeAssistant,
    read_only_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # get_single is admin-only; Core's REST entry list is open to every user, so
    # a non-admin token keeps the read it had before.
    entry = _add_entry(hass)
    _send_spy, list_spy = _spies(read_only_client)

    with list_spy as listed, caplog.at_level(logging.DEBUG, logger=_REST_LOGGER):
        read = await read_only_client.get_config_entry(entry.entry_id)

    assert read["entry_id"] == entry.entry_id
    listed.assert_awaited_once()
    assert _get_single_levels(caplog) == ["DEBUG"]


class _BrokenOptionsFlow(ConfigFlow):
    """An integration whose options-flow support check raises."""

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        raise RuntimeError("boom")


@pytest.mark.usefixtures("config")
async def test_other_core_error_is_raised_not_retried_over_rest(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Core answered, so its error is the result: listing every entry over REST
    # would only hide it.
    mock_integration(hass, MockModule("broken_flow"))
    entry = _add_entry(hass, "broken_flow")
    _send_spy, list_spy = _spies(core_client)

    with (
        mock_config_flow("broken_flow", _BrokenOptionsFlow),
        list_spy as listed,
        caplog.at_level(logging.DEBUG, logger=_REST_LOGGER),
        pytest.raises(HomeAssistantAPIError) as exc,
    ):
        await core_client.get_config_entry(entry.entry_id)

    assert exc.value.status_code != 404
    assert "get_single failed" in str(exc.value)
    listed.assert_not_awaited()
    assert _get_single_levels(caplog) == ["ERROR"]
