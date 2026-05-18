"""Unit tests for the YAML singleton behavior in yaml_rt."""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
from ruamel.yaml import YAML

# Mock Home Assistant imports so the package __init__ can be loaded.
sys.modules["voluptuous"] = MagicMock()
homeassistant = MagicMock()
sys.modules["homeassistant"] = homeassistant
sys.modules["homeassistant.components"] = homeassistant.components
sys.modules["homeassistant.config_entries"] = homeassistant.config_entries
sys.modules["homeassistant.core"] = homeassistant.core
sys.modules["homeassistant.helpers"] = homeassistant.helpers
sys.modules["homeassistant.helpers.config_validation"] = homeassistant.helpers.config_validation

from custom_components.ha_mcp_tools.yaml_rt import (  # noqa: E402
    _THREAD_LOCAL,
    make_yaml,
)


@pytest.fixture(autouse=True)
def clear_thread_local():
    """Ensure the thread-local storage is clean before each test."""
    if hasattr(_THREAD_LOCAL, "yaml"):
        del _THREAD_LOCAL.yaml
    yield
    if hasattr(_THREAD_LOCAL, "yaml"):
        del _THREAD_LOCAL.yaml


def test_make_yaml_singleton_in_same_thread():
    """Verify that make_yaml returns the same instance when called multiple times in one thread."""
    # We patch YAML constructor to count instantiations.
    # Since YAML is a class, we patch its __init__.
    with patch.object(YAML, "__init__", return_value=None) as mock_init:
        y1 = make_yaml()
        y2 = make_yaml()

        assert y1 is y2
        assert mock_init.call_count == 1


def test_make_yaml_singleton_per_thread():
    """Verify that make_yaml returns different instances for different threads."""
    instances = {}

    def worker(name):
        instances[name] = make_yaml()

    with patch.object(YAML, "__init__", return_value=None) as mock_init:
        t1 = threading.Thread(target=worker, args=("t1",))
        t2 = threading.Thread(target=worker, args=("t2",))

        t1.start()
        t1.join()
        t2.start()
        t2.join()

        assert "t1" in instances
        assert "t2" in instances
        assert instances["t1"] is not instances["t2"]
        # One instantiation per thread
        assert mock_init.call_count == 2
