"""The config-entry domains the component lists and searches as flow helpers."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_config_flows

from .constants import FLOW_HELPER_DOMAINS

_LOGGER = logging.getLogger(__package__)

# ``options_withheld`` value on the records of a custom-only domain.
OPTIONS_WITHHELD_CUSTOM = "custom_integration"

# Added to a ``search``'s ``warnings``, and reported to the server's listing as
# ``helper_flows_degraded``, when the loader read below failed.
HELPER_FLOWS_DEGRADED_WARNING = (
    "Home Assistant's loader could not list its helper flows, so custom helper "
    "integrations are missing from this response."
)


async def _flow_helper_domains(hass: HomeAssistant) -> dict[str, Any]:
    """Core's helper flows (what ``flow_handlers?type=helper`` returns) and the
    custom-only ones among them.

    A custom-only domain's entries go out with ``options: None`` and
    ``options_withheld`` (:data:`OPTIONS_WITHHELD_CUSTOM`): unlike
    ``config_entries`` and ``ha_get_integration``, this path has no flow-schema
    redaction. A custom override of a Core helper domain keeps its options, which
    the auto-backup reads. A failed loader read falls back to Core's own list.
    """
    try:
        flows = frozenset(await async_get_config_flows(hass, "helper"))
    except Exception:  # any loader failure: degrade to Core's list, never fail
        _LOGGER.warning(
            "could not read the helper flows from Core's loader", exc_info=True
        )
        return {
            "flow_domains": FLOW_HELPER_DOMAINS,
            "custom_domains": frozenset(),
            "helper_flows_degraded": True,
        }
    return {"flow_domains": flows, "custom_domains": flows - FLOW_HELPER_DOMAINS}
