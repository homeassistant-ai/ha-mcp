"""The component's ``server_entry_update`` write on a real ``hass``.

The component applies a ``pip_spec`` change to its own server config entry
through Core's ``async_update_entry``, deferred on a background task so the
WebSocket answer reaches the server before the reload it triggers tears the
server down. The entry is a ``MockConfigEntry`` server row in Core's config
entries; the frames go over Core's WebSocket API.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.const import DEFAULT_PIP_SPEC, DOMAIN
from custom_components.ha_mcp_tools.websocket_api import system
from custom_components.ha_mcp_tools.websocket_api.constants import (
    SERVER_ENTRY_UPDATE_MAX_PIP_SPEC,
)

from ._component_core import ws_call, ws_result
from ._server_entry import add_server_entry

_WS = "ha_mcp_tools/server_entry_update"


async def _update(ws: MockHAClientWebSocket, **frame: Any) -> dict[str, Any]:
    result: dict[str, Any] = await ws_result(ws, _WS, **frame)
    return result


async def _applied(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


# A week: no real timer expires during a test, so only its clock releases the write.
_HELD_S = 7 * 24 * 3600


@pytest.fixture
def held_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "SERVER_ENTRY_UPDATE_FLUSH_DELAY_S", _HELD_S)


async def _release(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=_HELD_S + 1))
    await _applied(hass)


@pytest.mark.usefixtures("held_write")
async def test_the_change_lands_after_the_answer_and_keeps_every_other_option(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The answer comes back before the entry changes; then the other options,
    a server_url override among them, are kept."""
    entry = add_server_entry(
        hass,
        options={"channel": "stable", "pip_spec": "", "server_url": "http://ha:8123"},
    )

    result = await _update(component_ws, pip_spec="ha-mcp==2.0.0")
    before = dict(entry.options)
    await _release(hass)

    assert result["scheduled"] is True
    assert result["entry_id"] == entry.entry_id
    assert (result["applying"], result["previous"]) == (
        {"pip_spec": "ha-mcp==2.0.0"},
        {"pip_spec": ""},
    )
    assert before["pip_spec"] == ""
    assert dict(entry.options) == {
        "channel": "stable",
        "pip_spec": "ha-mcp==2.0.0",
        "server_url": "http://ha:8123",
    }


@pytest.mark.usefixtures("held_write")
async def test_a_change_made_while_the_write_waits_survives_it(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The change merges into the options as they are when it applies, not a
    snapshot from the frame."""
    entry = add_server_entry(hass, options={"pip_spec": ""})

    await _update(component_ws, pip_spec="ha-mcp==2.0.0")
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "server_url": "http://changed:8123"}
    )
    await _release(hass)

    assert dict(entry.options) == {
        "pip_spec": "ha-mcp==2.0.0",
        "server_url": "http://changed:8123",
    }


@pytest.fixture
def no_flush_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deferred write runs at once; its deferral is the tests above's."""
    monkeypatch.setattr(system, "SERVER_ENTRY_UPDATE_FLUSH_DELAY_S", 0)


@pytest.mark.usefixtures("no_flush_delay")
@pytest.mark.parametrize(
    ("stored", "sent", "stored_after"),
    [
        # A value of its own replaces the override.
        (
            {"channel": "dev", "pip_spec": "ha-mcp==1.0.0"},
            "ha-mcp==2.0.0",
            "ha-mcp==2.0.0",
        ),
        # Blank, or the bare distribution, means "no override": the paired
        # server stays in force.
        ({"channel": "dev", "pip_spec": "ha-mcp==1.0.0"}, "", ""),
        ({"channel": "dev", "pip_spec": "ha-mcp==1.0.0"}, "   ", ""),
        ({"channel": "dev", "pip_spec": "ha-mcp==1.0.0"}, DEFAULT_PIP_SPEC, ""),
        # Clearing an override the entry never had still writes the key.
        ({"channel": "dev"}, "", ""),
    ],
    ids=["new-value", "clear", "whitespace", "bare-distribution", "clear-absent"],
)
async def test_a_pip_spec_is_stored_as_the_options_flow_would_store_it(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    stored: dict[str, str],
    sent: str,
    stored_after: str,
) -> None:
    entry = add_server_entry(hass, options=stored)

    result = await _update(component_ws, pip_spec=sent)
    await _applied(hass)

    assert result["scheduled"] is True
    assert result["applying"] == {"pip_spec": stored_after}
    assert dict(entry.options) == {**stored, "pip_spec": stored_after}


@pytest.mark.usefixtures("no_flush_delay")
@pytest.mark.parametrize(
    ("stored", "sent"),
    [("ha-mcp==1.0.0", "ha-mcp==1.0.0"), ("", DEFAULT_PIP_SPEC)],
    ids=["same-value", "normalizes-to-stored"],
)
async def test_a_pip_spec_already_in_force_changes_nothing(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    stored: str,
    sent: str,
) -> None:
    """Nothing is written, so nothing reloads; the retired channel option is
    not reported back."""
    entry = add_server_entry(hass, options={"channel": "dev", "pip_spec": stored})
    before = entry.modified_at

    result = await _update(component_ws, pip_spec=sent)
    await _applied(hass)

    assert (result["scheduled"], result["unchanged"]) == (False, True)
    assert result["previous"] == {"pip_spec": stored}
    assert entry.modified_at == before


@pytest.mark.usefixtures("no_flush_delay")
async def test_a_channel_beside_a_pip_spec_is_ignored(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """An older server sends both; the channel option is retired."""
    entry = add_server_entry(hass, options={"pip_spec": ""})

    result = await _update(component_ws, channel="dev", pip_spec="ha-mcp==2.0.0")
    await _applied(hass)

    assert result["applying"] == {"pip_spec": "ha-mcp==2.0.0"}
    assert dict(entry.options) == {"pip_spec": "ha-mcp==2.0.0"}


@pytest.mark.parametrize(
    "frame",
    [{"channel": "dev"}, {"channel": "stable"}, {}],
    ids=["dev", "stable", "empty"],
)
async def test_a_frame_without_a_pip_spec_is_refused_with_where_builds_come_from(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, frame: dict[str, str]
) -> None:
    """Each component release pins its server, so a channel choice is refused
    with where development builds come from."""
    entry = add_server_entry(hass, options={"channel": "stable"})

    reply = await ws_call(component_ws, _WS, **frame)
    await _applied(hass)

    assert reply["success"] is False
    assert "Pre-release" in reply["error"]["message"]
    assert dict(entry.options) == {"channel": "stable"}


async def test_without_a_server_entry_the_update_is_refused(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    MockConfigEntry(domain=DOMAIN, data={"entry_type": "tools"}).add_to_hass(hass)

    reply = await ws_call(component_ws, _WS, pip_spec="ha-mcp==2.0.0")

    assert reply["success"] is False
    assert reply["error"]["code"] != "invalid_format"


@pytest.mark.parametrize(
    "frame",
    [
        {"channel": "nightly"},
        {"pip_spec": "a\nb"},
        {"pip_spec": "x" * (SERVER_ENTRY_UPDATE_MAX_PIP_SPEC + 1)},
        {"bogus": "x"},
    ],
    ids=["unknown-channel", "multiline", "overlong", "extra-key"],
)
async def test_a_malformed_update_is_refused_before_it_runs(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, frame: dict[str, str]
) -> None:
    add_server_entry(hass, options={"pip_spec": ""})

    reply = await ws_call(component_ws, _WS, **frame)

    assert reply["error"]["code"] == "invalid_format"


async def test_only_an_admin_may_update_the_server_entry(
    hass: HomeAssistant, component_ws_read_only: MockHAClientWebSocket
) -> None:
    entry = add_server_entry(hass, options={"pip_spec": ""})

    reply = await ws_call(component_ws_read_only, _WS, pip_spec="ha-mcp==2.0.0")
    await _applied(hass)

    assert reply["error"]["code"] == "unauthorized"
    assert dict(entry.options) == {"pip_spec": ""}
