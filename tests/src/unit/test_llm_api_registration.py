"""Registering the toolset as Home Assistant LLM API(s) (issue #1745).

``async_register_llm_api`` runs on every in-process server bring-up. The
exposure option picks which APIs a conversation agent can select, a repeated
bring-up must not stack registrations, and nothing that goes wrong here may
reach the bring-up, which would tear the running server down.
"""

from __future__ import annotations

import logging
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import llm_api
from custom_components.ha_mcp_tools.const import (
    DATA_LLM_API_UNSUB,
    DOMAIN,
    EXPOSURE_BOTH,
    EXPOSURE_FULL,
    EXPOSURE_TOOL_SEARCH,
    OPT_LLM_API_EXPOSURE,
)

from ._llm_api_helpers import registered_apis

_FULL_ID = f"{DOMAIN}-entry-1745"
_SEARCH_ID = f"{_FULL_ID}-toolsearch"
_URL = "http://127.0.0.1:9584/private_x"


def _add_entry(
    hass: HomeAssistant, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="HA-MCP Server",
        entry_id="entry-1745",
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


def _ours(hass: HomeAssistant) -> dict[str, Any]:
    """The APIs this component registered; Core registers its own as well."""
    return {
        api_id: api
        for api_id, api in registered_apis(hass).items()
        if api_id.startswith(DOMAIN)
    }


async def _register(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    port: int = 9584,
    secret_path: str = "/private_x",
) -> None:
    await llm_api.async_register_llm_api(
        hass, entry, port=port, secret_path=secret_path
    )


async def test_default_exposure_registers_only_the_tool_search_api(
    hass: HomeAssistant,
) -> None:
    await _register(hass, _add_entry(hass))

    apis = _ours(hass)
    assert set(apis) == {_SEARCH_ID}
    api = apis[_SEARCH_ID]
    assert api.name == "HA-MCP Server (tool search)"
    assert api.mode == EXPOSURE_TOOL_SEARCH
    assert api.server_url == _URL
    unsubs = hass.data[DOMAIN][DATA_LLM_API_UNSUB]
    assert len(unsubs) == 1 and all(callable(unsub) for unsub in unsubs)


async def test_full_exposure_registers_only_the_full_api(
    hass: HomeAssistant,
) -> None:
    await _register(hass, _add_entry(hass, {OPT_LLM_API_EXPOSURE: EXPOSURE_FULL}))

    apis = _ours(hass)
    assert set(apis) == {_FULL_ID}
    assert apis[_FULL_ID].mode == EXPOSURE_FULL
    assert apis[_FULL_ID].name == "HA-MCP Server"


async def test_both_exposure_registers_two_apis_for_one_server(
    hass: HomeAssistant,
) -> None:
    await _register(hass, _add_entry(hass, {OPT_LLM_API_EXPOSURE: EXPOSURE_BOTH}))

    apis = _ours(hass)
    assert set(apis) == {_FULL_ID, _SEARCH_ID}
    assert {api.server_url for api in apis.values()} == {_URL}
    assert len(hass.data[DOMAIN][DATA_LLM_API_UNSUB]) == 2


async def test_an_unknown_stored_exposure_degrades_to_the_default(
    hass: HomeAssistant,
) -> None:
    await _register(hass, _add_entry(hass, {OPT_LLM_API_EXPOSURE: "bogus"}))

    assert set(_ours(hass)) == {_SEARCH_ID}


async def test_unregister_removes_the_apis_and_is_idempotent(
    hass: HomeAssistant,
) -> None:
    await _register(hass, _add_entry(hass, {OPT_LLM_API_EXPOSURE: EXPOSURE_BOTH}))

    llm_api.async_unregister_llm_api(hass)

    assert _ours(hass) == {}
    assert DATA_LLM_API_UNSUB not in hass.data[DOMAIN]
    # Reload paths run the teardown again; the second run must not raise.
    llm_api.async_unregister_llm_api(hass)


async def test_a_second_bring_up_replaces_the_stale_registration(
    hass: HomeAssistant,
) -> None:
    """A crashed reload never ran its teardown; Core refuses duplicate ids."""
    entry = _add_entry(hass)

    await _register(hass, entry)
    await _register(hass, entry, port=9999, secret_path="/private_y")

    apis = _ours(hass)
    assert len(apis) == 1
    assert apis[_SEARCH_ID].server_url == "http://127.0.0.1:9999/private_y"


async def test_an_id_core_refuses_is_logged_not_raised(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Anything escaping would tear the already-running server down (#1782)."""
    llm.async_register_api(
        hass,
        llm_api.HaMcpLlmApi(hass=hass, id=_SEARCH_ID, name="taken", server_url=_URL),
    )

    with caplog.at_level(logging.WARNING):
        await _register(hass, _add_entry(hass))

    assert DATA_LLM_API_UNSUB not in hass.data.get(DOMAIN, {})
    assert "Could not register the HA-MCP LLM API" in caplog.text


async def test_an_unexpected_registration_failure_is_logged_not_raised(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        llm, "async_register_api", MagicMock(side_effect=RuntimeError("unexpected"))
    )

    with caplog.at_level(logging.WARNING):
        await _register(hass, _add_entry(hass))

    assert DATA_LLM_API_UNSUB not in hass.data.get(DOMAIN, {})
    assert "Could not register the HA-MCP LLM API" in caplog.text


async def test_a_build_without_the_mcp_sdk_skips_the_feature_with_a_warning(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The SDK arrives with the runtime-installed server package."""
    monkeypatch.setitem(sys.modules, "mcp.client.session", None)

    with caplog.at_level(logging.WARNING):
        await _register(hass, _add_entry(hass))

    assert _ours(hass) == {}
    assert DATA_LLM_API_UNSUB not in hass.data.get(DOMAIN, {})
    assert "required LLM dependency is not importable" in caplog.text
