"""The loopback URL the in-process server reaches Home Assistant core on (#1890).

It honors the http integration's real port and SSL setting
(``hass.config.api``) instead of a hardcoded ``http://127.0.0.1:8123``, which
spoke plaintext into a TLS socket on any instance with ``http.ssl_certificate``
and killed every HA round-trip while the MCP handshake kept working.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.loopback_url import (  # noqa: E402
    derive_loopback_url,
)


@pytest.mark.parametrize(
    ("api", "expected"),
    [
        (None, ("http://127.0.0.1:8123", None)),
        (SimpleNamespace(port=8123, use_ssl=True), ("https://127.0.0.1:8123", False)),
        (SimpleNamespace(port=8444, use_ssl=False), ("http://127.0.0.1:8444", None)),
        (SimpleNamespace(port=65535, use_ssl=False), ("http://127.0.0.1:65535", None)),
        (SimpleNamespace(port=0, use_ssl=False), ("http://127.0.0.1:8123", None)),
        (SimpleNamespace(port=True, use_ssl=False), ("http://127.0.0.1:8123", None)),
        (
            SimpleNamespace(port=object(), use_ssl=True),
            ("https://127.0.0.1:8123", False),
        ),
    ],
    ids=[
        "no-api",
        "ssl",
        "custom-port",
        "highest-port",
        "port-0",
        "bool-port",
        "junk-port",
    ],
)
def test_loopback_url_follows_the_http_config_and_rejects_bad_ports(api, expected):
    hass = SimpleNamespace(config=SimpleNamespace(api=api))

    assert derive_loopback_url(hass) == expected
