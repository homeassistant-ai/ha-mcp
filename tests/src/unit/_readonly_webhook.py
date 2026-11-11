"""The read-only webhook modules fixture shared by the webhook tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def readonly_webhook_modules():
    import importlib.util
    from pathlib import Path

    from custom_components.ha_mcp_tools import readonly_webhook as embedded

    source = (
        Path(__file__).resolve().parents[3]
        / "homeassistant-addon-webhook-proxy-dev/mcp_proxy_dev/readonly_webhook.py"
    )
    assert source.read_bytes() == Path(embedded.__file__).read_bytes()
    spec = importlib.util.spec_from_file_location("readonly_webhook_proxy_test", source)
    assert spec is not None and spec.loader is not None
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    return embedded, proxy
