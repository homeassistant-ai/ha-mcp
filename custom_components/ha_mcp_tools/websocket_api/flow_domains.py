"""The config-entry domains the component lists and searches as flow helpers."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_config_flows

from .constants import FLOW_HELPER_DOMAINS


async def _flow_helper_domains(hass: HomeAssistant) -> dict[str, frozenset[str]]:
    """Return the running Core's helper flows and the custom-only ones among them.

    ``flow_domains`` is what Core's ``flow_handlers?type=helper`` endpoint returns,
    the source the server's ``flow_helper_lookup._helper_flow_domains`` reads:
    Core's generated ``FLOWS["helper"]`` plus custom integrations that have a
    config flow and ``integration_type: "helper"``. ``custom_domains`` holds the
    domains outside Core's own list; the options of their entries are withheld,
    since nothing in the component knows which of their fields hold credentials.
    A custom integration overriding a Core helper domain keeps that domain's
    treatment, so its options stay readable for the auto-backup.
    """
    flows = frozenset(await async_get_config_flows(hass, "helper"))
    return {"flow_domains": flows, "custom_domains": flows - FLOW_HELPER_DOMAINS}
