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
    MAX_OAUTH_CALLBACK_LENGTH,
    MAX_OAUTH_CALLBACKS,
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


async def test_saving_the_default_unchanged_keeps_following_it() -> None:
    # The panel's Save on an untouched list must not freeze today's default.
    hass, entry = _hass({"server_port": 9584})
    result = await _update(hass, allowlist=list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST))
    assert result["saved"] is True
    assert result["customized"] is False
    assert entry.options == {"server_port": 9584}


async def test_an_over_long_callback_saves_nothing_and_is_named() -> None:
    hass, entry = _hass()
    long = "https://example.com/" + "a" * MAX_OAUTH_CALLBACK_LENGTH
    result = await _update(hass, allowlist=[long])
    assert result["saved"] is False
    assert result["invalid"] == [long]
    assert entry.options == {}


async def test_reset_restores_the_default_list() -> None:
    hass, entry = _hass({OPT_OAUTH_REDIRECT_ALLOWLIST: []})
    result = await _update(hass, reset=True)
    assert result["allowlist"] == list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)
    assert OPT_OAUTH_REDIRECT_ALLOWLIST not in entry.options


async def test_an_update_naming_neither_change_is_refused() -> None:
    hass, entry = _hass({"server_port": 9584})
    with pytest.raises(Exception, match="needs allowlist or reset"):
        await _update(hass)
    assert entry.options == {"server_port": 9584}


async def test_an_install_without_the_server_entry_is_told_so() -> None:
    hass = _Hass(FakeConfigEntry(domain="ha_mcp_tools", data={"entry_type": "tools"}))
    hass.config_entries = _ConfigEntries([])
    with pytest.raises(Exception, match="no ha_mcp_tools in-process server"):
        await _update(hass, allowlist=[CALLBACK])


class TestUpdateFrame:
    @pytest.fixture
    def schema(self, monkeypatch: Any) -> Any:
        monkeypatch.setattr(oauth_callbacks, "vol", _base._REAL_VOL)
        return _base._REAL_VOL.Schema(oauth_callbacks._oauth_callbacks_update_schema())

    @pytest.mark.parametrize(
        "frame",
        [
            {"allowlist": [CALLBACK], "reset": True},
            {"allowlist": [CALLBACK] * (MAX_OAUTH_CALLBACKS + 1)},
            {"allowlist": ["https://x/" + "a" * MAX_OAUTH_CALLBACK_LENGTH]},
        ],
        ids=["both_changes", "too_many", "too_long"],
    )
    def test_an_out_of_bounds_frame_is_refused(self, schema: Any, frame) -> None:
        with pytest.raises(_base._REAL_VOL.Invalid):
            schema({"type": wsapi.WS_OAUTH_CALLBACKS_UPDATE, **frame})


class TestAdminGate:
    @pytest.mark.parametrize(
        "conn",
        [_FakeConnection(is_admin=False), _FakeConnection(has_user=False)],
        ids=["non_admin", "no_user"],
    )
    def test_only_an_administrator_can_read_or_change_the_list(
        self, monkeypatch: Any, conn: Any
    ) -> None:
        fake = _FakeWSApi()
        monkeypatch.setattr(wsapi, "websocket_api", fake)
        monkeypatch.setattr(wsapi, "vol", _base._REAL_VOL)
        wsapi.async_register_commands(FakeHass())
        for command in (wsapi.WS_OAUTH_CALLBACKS, wsapi.WS_OAUTH_CALLBACKS_UPDATE):
            with pytest.raises(_Unauthorized):
                fake.registered[command](FakeHass(), conn, {"id": 1, "type": command})


def test_the_settings_panel_speaks_the_component_contract() -> None:
    """The server's panel handler copies these literals; they cannot import each other."""
    from ha_mcp.settings_ui import _handlers_oauth_callbacks as panel

    assert panel.WS_OAUTH_CALLBACKS == wsapi.WS_OAUTH_CALLBACKS
    assert panel.WS_OAUTH_CALLBACKS_UPDATE == wsapi.WS_OAUTH_CALLBACKS_UPDATE
    assert panel.CAPABILITY in wsapi.CAPABILITIES
    assert panel._MAX_CALLBACKS == MAX_OAUTH_CALLBACKS
    assert panel._MAX_CALLBACK_LENGTH == MAX_OAUTH_CALLBACK_LENGTH
