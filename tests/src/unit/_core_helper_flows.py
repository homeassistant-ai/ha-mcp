"""The helper flows Core's own flow registry lists."""

from homeassistant.generated.config_flows import FLOWS

CORE_HELPER_FLOWS: frozenset[str] = frozenset(FLOWS["helper"])
