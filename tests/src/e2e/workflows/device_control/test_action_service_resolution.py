"""Every domain-table action must resolve to a service the instance has.

The per-domain ``valid_actions`` tables became reachable in #2076 (the
bare-domain handler fix) after years behind an unreachable lookup, and
had never been validated against real service names — 15 entries passed
validation and failed at dispatch. This test drives every table entry
through ``_resolve_service_name`` and asserts the resulting service
exists on the live instance, so a table entry can never again outrun
the resolver or the HA service surface.

``ha_list_services`` returns a FLAT ``services`` dict keyed
``"<domain>.<service>"`` and paginated (default 50), so the check
queries one domain at a time with a generous limit rather than relying
on an unfiltered listing. Every action-carrying domain must actually be
checked — a domain that stops loading on the instance fails the pin
instead of silently shrinking its coverage.
"""

import logging
from unittest.mock import MagicMock

from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.utils.domain_handlers import DOMAIN_HANDLERS

from ...utilities.assertions import safe_call_tool

logger = logging.getLogger(__name__)


async def test_every_table_action_resolves_to_a_live_service(mcp_client):
    tools = DeviceControlTools(client=MagicMock())
    missing: list[str] = []
    checked_domains: set[str] = set()
    checked = 0
    for domain, handler in DOMAIN_HANDLERS.items():
        actions = handler.get("valid_actions", [])
        if not actions:
            continue
        data = await safe_call_tool(
            mcp_client, "ha_list_services", {"domain": domain, "limit": 200}
        )
        live_services = set(data.get("services", {})) if data.get("success") else set()
        if not live_services:
            logger.info("no services returned for %s", domain)
            continue
        checked_domains.add(domain)
        for action in actions:
            checked += 1
            service_name, _ = tools._resolve_service_name(domain, action, None)
            if f"{domain}.{service_name}" not in live_services:
                missing.append(f"{domain}.{action} -> {service_name}")

    # Every action-carrying domain loads on the test instance today; a
    # domain silently dropping out of the pin (instance config drift)
    # must fail loudly rather than shrink the coverage unnoticed.
    expected = {d for d, h in DOMAIN_HANDLERS.items() if h.get("valid_actions")}
    assert checked_domains == expected, (
        f"domains missing from the resolution pin (not loaded on the "
        f"instance?): {sorted(expected - checked_domains)}"
    )
    assert not missing, (
        "domain-table actions resolving to services the instance does not "
        f"have: {missing}"
    )
    logger.info("validated %d action->service resolutions", checked)
