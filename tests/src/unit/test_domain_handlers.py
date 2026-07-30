"""Unit tests for domain-handler resolution.

Regression pin: both device_control call sites pass a BARE domain to
``get_domain_handler``, but the old implementation required a dot and
sent every such call to the default handler — so the per-domain
``valid_actions`` tables were unreachable and, e.g., climate's ``heat``
was rejected upfront as an invalid action.
"""

from ha_mcp.utils.domain_handlers import (
    DOMAIN_HANDLERS,
    get_default_handler,
    get_domain_handler,
)


class TestGetDomainHandler:
    def test_bare_domain_resolves_per_domain_table(self):
        # The device_control call-site shape.
        handler = get_domain_handler("climate")
        assert handler is DOMAIN_HANDLERS["climate"]
        assert "heat" in handler["valid_actions"]

    def test_full_entity_id_resolves_per_domain_table(self):
        handler = get_domain_handler("light.living_room")
        assert handler is DOMAIN_HANDLERS["light"]
        assert "set" in handler["valid_actions"]

    def test_unknown_domain_falls_back_to_default(self):
        handler = get_domain_handler("no_such_domain")
        assert handler == get_default_handler()
        assert handler.get("unknown_domain") is True

    def test_empty_string_falls_back_to_default(self):
        assert get_domain_handler("") == get_default_handler()

    def test_read_only_domain_exposes_no_actions(self):
        # A sensor must resolve its own table (empty valid_actions), not
        # the default handler's on/off/toggle.
        handler = get_domain_handler("sensor")
        assert handler["valid_actions"] == []
        assert handler.get("read_only") is True

    def test_tables_keep_the_default_on_off_vocabulary_where_it_resolves(self):
        # The per-domain tables replace the default handler's vocabulary,
        # so every domain whose turn_on/turn_off/toggle services exist must
        # carry the matching short actions itself — dropping them regressed
        # scene/script/automation/media_player/camera "on"/"off" and
        # climate "toggle" when the tables first went live (#2076 review).
        assert "on" in DOMAIN_HANDLERS["scene"]["valid_actions"]
        for domain in ("script", "automation", "media_player", "camera"):
            actions = DOMAIN_HANDLERS[domain]["valid_actions"]
            assert "on" in actions and "off" in actions, domain
        assert "toggle" in DOMAIN_HANDLERS["climate"]["valid_actions"]

    def test_tables_carry_no_unmappable_actions(self):
        # Entries the resolver cannot map to a real HA service were
        # trimmed (light "adjust", fan/humidifier "set", camera "stream",
        # scene "activate") — the e2e resolution test pins the rest
        # against a live instance.
        assert "adjust" not in DOMAIN_HANDLERS["light"]["valid_actions"]
        assert "set" not in DOMAIN_HANDLERS["fan"]["valid_actions"]
        assert "set" not in DOMAIN_HANDLERS["humidifier"]["valid_actions"]
        assert "stream" not in DOMAIN_HANDLERS["camera"]["valid_actions"]
        assert "activate" not in DOMAIN_HANDLERS["scene"]["valid_actions"]


class TestResolveServiceName:
    """Pins the action-to-service mappings the tables rely on."""

    @staticmethod
    def _resolve(domain, action, parameters=None):
        from unittest.mock import MagicMock

        from ha_mcp.tools.device_control import DeviceControlTools

        return DeviceControlTools(client=MagicMock())._resolve_service_name(
            domain, action, parameters
        )

    def test_cover_stop_and_set(self):
        assert self._resolve("cover", "stop")[0] == "stop_cover"
        assert self._resolve("cover", "set")[0] == "set_cover_position"

    def test_media_player_track_navigation(self):
        assert self._resolve("media_player", "next")[0] == "media_next_track"
        assert self._resolve("media_player", "previous")[0] == "media_previous_track"

    def test_lock_open_maps_to_open(self):
        assert self._resolve("lock", "open")[0] == "open"

    def test_alarm_actions_get_alarm_prefix(self):
        for action in ("arm_home", "arm_away", "arm_night", "disarm"):
            assert self._resolve("alarm_control_panel", action)[0] == f"alarm_{action}"

    def test_climate_heat_cool_is_an_hvac_mode(self):
        service, params = self._resolve("climate", "heat_cool")
        assert service == "set_hvac_mode"
        assert params == {"hvac_mode": "heat_cool"}
