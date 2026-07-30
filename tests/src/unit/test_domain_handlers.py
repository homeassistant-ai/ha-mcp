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
