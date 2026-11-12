"""Unit tests for the YAML singleton behavior in yaml_rt."""

from __future__ import annotations

import threading
from io import StringIO

from ruamel.yaml import YAML

from custom_components.ha_mcp_tools.yaml_rt import make_yaml


def _keeps_quotes(ry: YAML) -> bool:
    buf = StringIO()
    # A plain string holding quote characters is quoted on dump either way; a
    # loaded double-quoted scalar keeps its quotes only with preserve_quotes.
    ry.dump(ry.load('key: "quoted_value"\n'), buf)
    return 'key: "quoted_value"' in buf.getvalue()


def test_make_yaml_singleton_in_same_thread():
    """Verify that make_yaml returns the same instance when called multiple times in one thread."""
    y1 = make_yaml()
    y2 = make_yaml()

    assert y1 is y2
    # Round-trip a quoted scalar to confirm preserve_quotes is set
    assert _keeps_quotes(y1)


def test_make_yaml_singleton_per_thread():
    """Each thread builds its own instance, and a freshly built one keeps quotes."""
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
    # Each new thread starts without a cached instance, so these were built
    # fresh rather than handed down from another test's thread.
    assert instances["t1"].preserve_quotes is True
    assert _keeps_quotes(instances["t1"])
