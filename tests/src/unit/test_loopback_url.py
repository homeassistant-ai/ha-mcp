"""The loopback URL the in-process server reaches Home Assistant core on (#1890).

It honors the http integration's real port and SSL setting
(``hass.config.api``) instead of a hardcoded ``http://127.0.0.1:8123``, which
spoke plaintext into a TLS socket on any instance with ``http.ssl_certificate``
and killed every HA round-trip while the MCP handshake kept working.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from homeassistant.components.http import ApiConfig
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools.loopback_url import (
    derive_loopback_url,
)

# Core's http integration binds the first ``server_host``; the default list
# starts with the IPv4 wildcard.
_DEFAULT_HOST = "0.0.0.0"


def _api(port: Any, use_ssl: bool, host: str = _DEFAULT_HOST) -> ApiConfig:
    """The ApiConfig the http integration would set, with a malformed port allowed."""
    return ApiConfig("192.168.1.2", host, cast("int", port), use_ssl)


@pytest.mark.parametrize(
    ("api", "expected"),
    [
        (None, ("http://127.0.0.1:8123", None)),
        (_api(8123, True), ("https://127.0.0.1:8123", False)),
        (_api(8444, False), ("http://127.0.0.1:8444", None)),
        (_api(65535, False), ("http://127.0.0.1:65535", None)),
        (_api(0, False), ("http://127.0.0.1:8123", None)),
        (_api(True, False), ("http://127.0.0.1:8123", None)),
        (_api(object(), True), ("https://127.0.0.1:8123", False)),
        (_api(8123, True, "10.0.2.15"), ("https://10.0.2.15:8123", False)),
        (_api(8123, False, "fd00::5"), ("http://[fd00::5]:8123", None)),
        (_api(8123, False, "0.0.0.0"), ("http://127.0.0.1:8123", None)),
        (_api(8123, True, "::"), ("https://[::1]:8123", False)),
    ],
    ids=[
        "no-api",
        "ssl",
        "custom-port",
        "highest-port",
        "port-0",
        "bool-port",
        "junk-port",
        "restricted-listen-address",
        "ipv6-listen-address",
        "wildcard-listen-address",
        "ipv6-wildcard-listen-address",
    ],
)
def test_loopback_url_follows_the_http_config_and_rejects_bad_ports(
    hass: HomeAssistant,
    api: ApiConfig | None,
    expected: tuple[str, bool | None],
) -> None:
    hass.config.api = api

    assert derive_loopback_url(hass) == expected
