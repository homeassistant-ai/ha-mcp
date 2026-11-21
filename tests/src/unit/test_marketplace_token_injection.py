"""The pre-boot edits of Core's stored config entries for the E2E images.

``inject_marketplace_token`` is the shared transform behind both CI injection
paths (the testcontainer seed copy in ``e2e/_conftest_testcontainer.py`` and
the HAOS qcow2 pre-boot edit in
``marketplace_token.inject_marketplace_token_in_qcow2``). It connects the
Marketplace to GitHub in CI, so its GitHub calls don't ride the shared-IP
60 req/h unauthenticated budget.

Every document below is what Core writes to ``.storage/core.config_entries``:
the entries are Core's own storage records of real config entries.

``haos_runtime`` and ``marketplace_token`` are loaded by file path: their
module-level imports are stdlib-only, so this collects everywhere, without assuming ``tests/src``
is importable as a package.
"""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from homeassistant.auth.models import User
from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.config_entries import (
    STORAGE_KEY,
    STORAGE_VERSION,
    STORAGE_VERSION_MINOR,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_bytes
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant.typing import WebSocketGenerator

from ._hacs_marketplace import set_up_marketplace
from ._marketplace_records import unload_marketplace


def _load(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


haos_runtime = _load("haos_runtime")
marketplace_token = _load("marketplace_token")


def _stored(entry: MockConfigEntry) -> dict[str, Any]:
    """The entry as Core stores it."""
    stored: dict[str, Any] = json.loads(json_bytes(entry.as_storage_fragment))
    return stored


def _doc(*entries: MockConfigEntry) -> dict[str, Any]:
    """A ``.storage/core.config_entries`` document holding ``entries``."""
    return {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_VERSION_MINOR,
        "key": STORAGE_KEY,
        "data": {"entries": [_stored(entry) for entry in entries]},
    }


def _server_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain="ha_mcp_tools",
        entry_id=haos_runtime.HA_MCP_SERVER_ENTRY_ID,
        data={"entry_type": "server"},
        options={},
    )


async def test_the_injected_token_connects_the_marketplace_to_github(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    hass_admin_user: User,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Core's Marketplace, set up from the injected entry, reports GitHub
    connected, and a user who accepted its warning, which Core stores in the
    same ``data`` the token goes into, keeps that acceptance."""
    entry = await set_up_marketplace(hass, aioclient_mock, hass_storage, {})
    manager: MarketplaceManager = entry.runtime_data
    manager.async_accept_warning(hass_admin_user.id)
    doc = _doc(MockConfigEntry(domain="sun"), entry)
    await unload_marketplace(hass)
    before = copy.deepcopy(doc["data"]["entries"])

    assert marketplace_token.inject_marketplace_token(doc, "ghs_test_token") is True

    injected = doc["data"]["entries"][1]
    assert {k: v for k, v in injected.items() if k != "data"} == {
        k: v for k, v in before[1].items() if k != "data"
    }
    assert doc["data"]["entries"][0] == before[0]

    hass.config_entries.async_update_entry(entry, data=injected["data"])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "marketplace/info"})
    info = (await client.receive_json())["result"]
    await unload_marketplace(hass)

    assert info["github_connected"] is True
    assert info["warning_accepted"] is True


def test_a_stale_token_is_replaced() -> None:
    doc = _doc(MockConfigEntry(domain="marketplace", data={"token": "stale"}))
    assert marketplace_token.inject_marketplace_token(doc, "fresh") is True
    assert doc["data"]["entries"][0]["data"] == {"token": "fresh"}


def test_a_document_without_the_marketplace_is_left_untouched() -> None:
    doc = _doc(MockConfigEntry(domain="sun"), MockConfigEntry(domain="demo"))
    before = copy.deepcopy(doc)
    assert marketplace_token.inject_marketplace_token(doc, "tok") is False
    assert doc == before


def test_qcow2_injector_is_noop_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No GITHUB_TOKEN -> no guestfish invocation, no exception."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def _fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("guestfish must not run without GITHUB_TOKEN")

    monkeypatch.setattr(marketplace_token.subprocess, "run", _fail)
    marketplace_token.inject_marketplace_token_in_qcow2(tmp_path / "image.qcow2")


# set_embedded_server_pip_spec (#1527): matched by entry_id, because the
# server entry shares its domain with the tools entry.


def test_pip_spec_and_admin_token_reach_only_the_server_entry() -> None:
    doc = _doc(
        MockConfigEntry(domain="ha_mcp_tools", entry_id="other"), _server_entry()
    )

    assert (
        haos_runtime.set_embedded_server_pip_spec(doc, "file:///w.whl", "tok") is True
    )

    tools, server = doc["data"]["entries"]
    assert server["options"]["pip_spec"] == "file:///w.whl"
    # #2427: the component no longer creates its own administrator.
    assert server["data"]["admin_token"] == "tok"
    assert server["data"]["entry_type"] == "server"
    assert "pip_spec" not in tools["options"]
    assert "admin_token" not in tools["data"]


def test_pip_spec_without_a_server_entry_leaves_the_document_untouched() -> None:
    doc = _doc(
        MockConfigEntry(
            domain="ha_mcp_tools", entry_id="other", options={"pip_spec": "keep"}
        )
    )
    before = copy.deepcopy(doc)
    assert haos_runtime.set_embedded_server_pip_spec(doc, "x", "tok") is False
    assert doc == before


# remove_tools_entry (#2292): the transform behind the no-tools lanes' qcow2
# pre-boot edit (remove_tools_entry_in_qcow2), discriminated the way the
# component itself dispatches (data.entry_type == "server" is the server
# entry; anything else under the ha_mcp_tools domain is the tools entry).


def test_the_tools_entry_is_removed_and_the_server_entry_kept() -> None:
    doc = _doc(
        MockConfigEntry(domain="sun"),
        MockConfigEntry(domain="ha_mcp_tools", entry_id="e2e_test_ha_mcp_tools_entry"),
        _server_entry(),
    )

    assert haos_runtime.remove_tools_entry(doc) == ["e2e_test_ha_mcp_tools_entry"]

    remaining = doc["data"]["entries"]
    assert [e["domain"] for e in remaining] == ["sun", "ha_mcp_tools"]
    assert remaining[1]["entry_id"] == haos_runtime.HA_MCP_SERVER_ENTRY_ID


def test_a_document_without_a_tools_entry_is_left_untouched() -> None:
    doc = _doc(MockConfigEntry(domain="sun"), _server_entry())
    before = copy.deepcopy(doc)
    assert haos_runtime.remove_tools_entry(doc) == []
    assert doc == before
