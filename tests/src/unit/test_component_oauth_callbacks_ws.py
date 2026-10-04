"""The ``oauth_callbacks`` commands the settings panel edits the allowlist with.

The none-mode callback allowlist (#2427) is an option on the server entry;
these commands read it and replace it without reloading the entry.
"""

from __future__ import annotations

from typing import Any

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DEFAULT_OAUTH_REDIRECT_ALLOWLIST,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
)
from custom_components.ha_mcp_tools.websocket_api import (  # noqa: E402
    oauth_callbacks,
)

from . import test_component_ws_search as _base  # noqa: E402
from .test_component_ws_search import (  # noqa: E402
    FakeConfigEntry,
    FakeHass,
    _FakeConnection,
    _FakeWSApi,
    _Unauthorized,
    wsapi,
)

CALLBACK = "https://chatgpt.example/cb"


class _ConfigEntries:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def async_entries(self) -> list[Any]:
        return list(self._entries)

    def async_update_entry(self, entry: Any, *, options: Any) -> bool:
        entry.options = dict(options)
        return True


class _Hass:
    def __init__(self, entry: Any) -> None:
        self.config_entries = _ConfigEntries([entry])
        self.data: dict[str, Any] = {}


def _hass(options: dict[str, Any] | None = None) -> tuple[_Hass, Any]:
    entry = FakeConfigEntry(
        domain="ha_mcp_tools",
        data={"entry_type": "server"},
        options=options or {},
        entry_id="srv1",
    )
    return _Hass(entry), entry


async def _update(hass: _Hass, **fields: Any) -> dict[str, Any]:
    msg = {"type": wsapi.WS_OAUTH_CALLBACKS_UPDATE, **fields}
    extra = await oauth_callbacks._oauth_callbacks_update_prep(hass, msg)
    return oauth_callbacks._do_oauth_callbacks_update(hass, msg, **extra)


def test_an_unedited_entry_reports_the_default_list() -> None:
    hass, _ = _hass()
    result = oauth_callbacks._do_oauth_callbacks(hass, {})
    assert result["allowlist"] == list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)
    assert result["customized"] is False


def test_the_list_is_reported_as_inactive_outside_none_mode() -> None:
    hass, _ = _hass({"webhook_auth": "ha_auth"})
    assert oauth_callbacks._do_oauth_callbacks(hass, {})["applies"] is False


async def test_a_save_keeps_the_other_options() -> None:
    hass, entry = _hass({"server_port": 9584})
    result = await _update(hass, allowlist=[f" {CALLBACK} ", CALLBACK])
    assert result["saved"] is True
    assert entry.options == {
        "server_port": 9584,
        OPT_OAUTH_REDIRECT_ALLOWLIST: [CALLBACK],
    }


async def test_an_invalid_entry_saves_nothing_and_is_named() -> None:
    hass, entry = _hass()
    result = await _update(hass, allowlist=[CALLBACK, "http://not-loopback/cb"])
    assert result["saved"] is False
    assert result["invalid"] == ["http://not-loopback/cb"]
    assert entry.options == {}


async def test_reset_restores_the_default_list() -> None:
    hass, entry = _hass({OPT_OAUTH_REDIRECT_ALLOWLIST: []})
    result = await _update(hass, reset=True)
    assert result["allowlist"] == list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)
    assert OPT_OAUTH_REDIRECT_ALLOWLIST not in entry.options


class TestAdminGate:
    @pytest.mark.parametrize(
        "conn",
        [_FakeConnection(is_admin=False), _FakeConnection(has_user=False)],
        ids=["non_admin", "no_user"],
    )
    def test_only_an_administrator_can_change_the_list(
        self, monkeypatch: Any, conn: Any
    ) -> None:
        fake = _FakeWSApi()
        monkeypatch.setattr(wsapi, "websocket_api", fake)
        monkeypatch.setattr(wsapi, "vol", _base._REAL_VOL)
        wsapi.async_register_commands(FakeHass())
        handler = fake.registered[wsapi.WS_OAUTH_CALLBACKS_UPDATE]
        with pytest.raises(_Unauthorized):
            handler(
                FakeHass(), conn, {"id": 1, "type": wsapi.WS_OAUTH_CALLBACKS_UPDATE}
            )
