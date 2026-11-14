"""App (add-on) tests run under Core's test plugin, like the unit tree.

The webhook-proxy component imports Home Assistant at module load; Core is a
dev dependency, so the real modules serve it. See ``tests/src/unit/conftest.py``
for why the plugin is loaded here rather than from its entry point.
"""

import pytest

pytest_plugins = ["pytest_homeassistant.plugins", "tests.core_stand_in_guard"]


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Core writes into its config directory; a shared one holds other tests' files."""
    return hass_tmp_config_dir
