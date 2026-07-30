"""Every domain-table action must resolve to a service the instance has.

The per-domain ``valid_actions`` tables became reachable in #2076 (the
bare-domain handler fix) after years behind an unreachable lookup, and
had never been validated against real service names — 15 entries passed
validation and failed at dispatch. This test drives every table entry
through ``_resolve_service_name`` and asserts the resulting service
exists on the live instance, so a table entry can never again outrun
the resolver or the HA service surface.
"""

import logging
from unittest.mock import MagicMock

from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.utils.domain_handlers import DOMAIN_HANDLERS

from ...utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)


def _service_names(domain_services) -> set[str]:
    """Normalize a ha_list_services per-domain payload to service names."""
    if isinstance(domain_services, dict):
        return set(domain_services)
    if isinstance(domain_services, list):
        names: set[str] = set()
        for item in domain_services:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, dict):
                name = item.get("service") or item.get("name")
                if name:
                    names.add(name)
        return names
    return set()


async def test_every_table_action_resolves_to_a_live_service(mcp_client):
    result = await mcp_client.call_tool("ha_list_services", {})
    data = assert_mcp_success(result, "list all services")
    services_by_domain = data.get("services", {})
    assert services_by_domain, f"no services returned: {list(data)}"

    tools = DeviceControlTools(client=MagicMock())
    missing: list[str] = []
    checked = 0
    for domain, handler in DOMAIN_HANDLERS.items():
        live_services = _service_names(services_by_domain.get(domain))
        if not live_services:
            # Domain not loaded on this instance (e.g. no alarm panel in
            # the test container) — nothing to validate against.
            logger.info("skipping %s: not loaded on instance", domain)
            continue
        for action in handler.get("valid_actions", []):
            checked += 1
            service_name, _ = tools._resolve_service_name(domain, action, None)
            if service_name not in live_services:
                missing.append(f"{domain}.{action} -> {service_name}")

    assert checked > 0, "no domain from DOMAIN_HANDLERS is loaded on the instance"
    assert not missing, (
        "domain-table actions resolving to services the instance does not "
        f"have: {missing}"
    )
    logger.info("validated %d action->service resolutions", checked)
