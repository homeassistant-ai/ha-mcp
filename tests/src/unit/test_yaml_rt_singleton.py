"""Unit tests for the YAML singleton behavior in yaml_rt."""

from __future__ import annotations

import threading
from io import StringIO

import pytest

from custom_components.ha_mcp_tools.yaml_rt import (
    _STORAGE,
    make_yaml,
)


@pytest.fixture(autouse=True)
def clear_thread_local():
    """Ensure the thread-local storage is clean before each test."""
    if hasattr(_STORAGE, "yaml"):
        del _STORAGE.yaml
    yield
    if hasattr(_STORAGE, "yaml"):
        del _STORAGE.yaml


def test_make_yaml_singleton_in_same_thread():
    """Verify that make_yaml returns the same instance when called multiple times in one thread."""
    y1 = make_yaml()
    y2 = make_yaml()

    assert y1 is y2
    # Round-trip a quoted scalar to confirm preserve_quotes is set
    buf = StringIO()
    y1.dump({"key": '"quoted_value"'}, buf)
    assert '"quoted_value"' in buf.getvalue()


def test_make_yaml_singleton_per_thread():
    """Verify that make_yaml returns different instances for different threads."""
    instances = {}

    def worker(name):
        instances[name] = make_yaml()

    t1 = threading.Thread(target=worker, args=("t1",))
    t2 = threading.Thread(target=worker, args=("t2",))

    t1.start()
    t1.join()
    t2.start()
    t2.join()

    assert "t1" in instances
    assert "t2" in instances
    assert instances["t1"] is not instances["t2"]


def test_make_yaml_rebuilds_after_storage_cleared():
    """Verify that make_yaml rebuilds the instance after thread-local storage is cleared."""
    y1 = make_yaml()
    del _STORAGE.yaml
    y2 = make_yaml()

    assert y2 is not y1
    assert y2.preserve_quotes is True
    # Round-trip a quoted scalar to confirm preserve_quotes actually took effect
    buf = StringIO()
    y2.dump({"key": '"quoted_value"'}, buf)
    assert '"quoted_value"' in buf.getvalue()
