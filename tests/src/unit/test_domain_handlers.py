"""Domain-handler resolution and the service each device action calls.

Regression pin: both device_control call sites pass a BARE domain to
``get_domain_handler``, but the old implementation required a dot and
sent every such call to the default handler — so the per-domain
``valid_actions`` tables were unreachable and, e.g., climate's ``heat``
was rejected upfront as an invalid action.

The action-to-service mappings are checked through the device control tool on
the server's own client: the call lands on a service registered in a real
``hass``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import async_mock_service

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import device_control
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.utils.domain_handlers import (
    DOMAIN_HANDLERS,
    get_default_handler,
    get_domain_handler,
)


class TestGetDomainHandler:
    def test_bare_domain_resolves_per_domain_table(self) -> None:
        # The device_control call-site shape.
        handler = get_domain_handler("climate")
        assert handler is DOMAIN_HANDLERS["climate"]
        assert "heat" in handler["valid_actions"]

    def test_full_entity_id_resolves_per_domain_table(self) -> None:
        handler = get_domain_handler("light.living_room")
        assert handler is DOMAIN_HANDLERS["light"]
        assert "set" in handler["valid_actions"]

    def test_unknown_domain_falls_back_to_default(self) -> None:
        handler = get_domain_handler("no_such_domain")
        assert handler == get_default_handler()
        assert handler.get("unknown_domain") is True

    def test_empty_string_falls_back_to_default(self) -> None:
        assert get_domain_handler("") == get_default_handler()

    def test_read_only_domain_exposes_no_actions(self) -> None:
        # A sensor must resolve its own table (empty valid_actions), not
        # the default handler's on/off/toggle.
        handler = get_domain_handler("sensor")
        assert handler["valid_actions"] == []
        assert handler.get("read_only") is True

    def test_tables_keep_the_default_on_off_vocabulary_where_it_resolves(self) -> None:
        # The per-domain tables replace the default handler's vocabulary,
        # so every domain whose turn_on/turn_off/toggle services exist must
        # carry the matching short actions itself — dropping them regressed
        # scene/script/automation/media_player/camera "on"/"off" and
        # climate "toggle" when the tables first went live (#2076 review).
        assert "on" in get_domain_handler("scene")["valid_actions"]
        for domain in ("script", "automation", "media_player", "camera"):
            actions = get_domain_handler(domain)["valid_actions"]
            assert "on" in actions and "off" in actions, domain
        assert "toggle" in get_domain_handler("climate")["valid_actions"]

    def test_tables_carry_no_unmappable_actions(self) -> None:
        # Entries the resolver cannot map to a real HA service were
        # trimmed (light "adjust", fan/humidifier "set", camera "stream",
        # scene "activate") — the e2e resolution test pins the rest
        # against a live instance.
        assert "adjust" not in get_domain_handler("light")["valid_actions"]
        assert "set" not in get_domain_handler("fan")["valid_actions"]
        assert "set" not in get_domain_handler("humidifier")["valid_actions"]
        assert "stream" not in get_domain_handler("camera")["valid_actions"]
        assert "activate" not in get_domain_handler("scene")["valid_actions"]


@pytest.fixture(autouse=True)
def _no_state_listener(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The state_changed listener is this server's own service; these tests
    # only check which service the call lands on.
    monkeypatch.setattr(
        device_control, "start_websocket_listener", AsyncMock(return_value=False)
    )
    yield


@pytest.mark.parametrize(
    ("entity_id", "action", "service", "data"),
    [
        pytest.param("cover.garage", "stop", "stop_cover", {}, id="cover-stop"),
        pytest.param("cover.garage", "set", "set_cover_position", {}, id="cover-set"),
        pytest.param(
            "media_player.tv", "next", "media_next_track", {}, id="media-next"
        ),
        pytest.param(
            "media_player.tv",
            "previous",
            "media_previous_track",
            {},
            id="media-previous",
        ),
        pytest.param("lock.front_door", "open", "open", {}, id="lock-open"),
        *(
            pytest.param(
                "alarm_control_panel.home",
                action,
                f"alarm_{action}",
                {},
                id=f"alarm-{action}",
            )
            for action in ("arm_home", "arm_away", "arm_night", "disarm")
        ),
        pytest.param(
            "climate.living_room",
            "heat_cool",
            "set_hvac_mode",
            {"hvac_mode": "heat_cool"},
            id="climate-hvac-mode",
        ),
    ],
)
async def test_device_action_calls_the_service_home_assistant_has(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_id: str,
    action: str,
    service: str,
    data: dict[str, Any],
) -> None:
    domain = entity_id.split(".", 1)[0]
    calls = async_mock_service(hass, domain, service)

    result = await DeviceControlTools(client=core_client).control_device_smart(
        entity_id=entity_id, action=action, validate_first=False
    )

    assert result["command_sent"] is True
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": entity_id, **data}
