"""App (add-on) tests run under Core's test plugin, like the unit tree.

The webhook-proxy component imports Home Assistant at module load; Core is a
dev dependency, so the real modules serve it. See ``tests/src/unit/conftest.py``
for why the plugin is loaded here rather than from its entry point.
"""

pytest_plugins = ["pytest_homeassistant.plugins", "tests.core_stand_in_guard"]
