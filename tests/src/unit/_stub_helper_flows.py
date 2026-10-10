"""The helper flows the unit tests' Core stub lists (see conftest)."""

import sys

STUB_HELPER_FLOWS: frozenset[str] = frozenset(
    sys.modules["homeassistant.generated.config_flows"].FLOWS["helper"]
)
