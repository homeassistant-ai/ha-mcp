"""How the in-process server reaches Home Assistant core over loopback (#1890)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .const import DEFAULT_LOOPBACK_URL

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def derive_loopback_url(hass: HomeAssistant) -> tuple[str, bool | None]:
    """Resolve the loopback base URL for HA core from the http integration.

    Returns ``(url, verify_ssl)`` where ``verify_ssl`` is ``False`` when the
    URL is ``https`` (HA's certificate is issued for its hostname, never for
    127.0.0.1, so verification on the loopback hop can only fail) and ``None``
    when no override of the server's default is needed.

    The hardcoded ``http://127.0.0.1:8123`` default this replaces broke every
    instance with ``http.ssl_certificate`` configured (issue #1890): port 8123
    speaks TLS there, so the server's plaintext REST/WS calls died with
    "Server disconnected without sending a response" / "did not receive a
    valid HTTP response" on every tool call — while the MCP handshake and
    tools/list (no HA round-trip) kept working. A custom ``server_port``
    similarly broke the hardcoded port. Both live in ``hass.config.api``,
    set by the ``http`` integration this component depends on; the constant
    remains the fallback if it is ever absent.
    """
    api = getattr(hass.config, "api", None)
    if api is None:
        # Leave a trail: if this ever fires on a real instance, the resulting
        # failure looks exactly like issue #1890 (TLS loopback broken, MCP
        # handshake fine) and took a live reproduction to diagnose last time.
        _LOGGER.debug(
            "hass.config.api unavailable; using hardcoded loopback default %s",
            DEFAULT_LOOPBACK_URL,
        )
        return DEFAULT_LOOPBACK_URL, None
    # Strict type checks (not coercion / truthiness): a malformed api object
    # must resolve to the plaintext default on port 8123, never to a surprise
    # port or https flip. bool is excluded because it is an int subclass.
    port_raw = getattr(api, "port", None)
    port = (
        port_raw
        if isinstance(port_raw, int)
        and not isinstance(port_raw, bool)
        and 0 < port_raw <= 65535
        else 8123
    )
    if getattr(api, "use_ssl", False) is True:
        return f"https://127.0.0.1:{port}", False
    return f"http://127.0.0.1:{port}", None
