"""Transport-security defaults for ha-mcp's Streamable-HTTP servers.

fastmcp >= 3.4.3 ships ``HostOriginGuardMiddleware`` (a DNS-rebinding guard) that
is **on by default**. Inserted at the front of the Streamable-HTTP ASGI stack, it
rejects any request whose ``Host`` header is not loopback / an explicitly-allowed
host with ``421 Misdirected Request``, and any browser ``Origin`` that is not
same-origin / loopback / explicitly allowed with ``403 Forbidden`` -- before the
request reaches any route.

That default is wrong for ha-mcp. We are *designed* to be reached through
operator-chosen reverse proxies and tunnels (Cloudflare Tunnel, nginx, Traefik,
Nabu Casa Remote UI) and via direct LAN IPs, whose ``Host`` values we cannot
enumerate. Leaving the guard on would ``421`` the majority of real deployments --
including the plain browser landing page, which is a no-``Origin`` navigation
that still trips the ``Host`` check.

Our security boundary is the high-entropy secret path (and, in OAuth mode, the
per-user token), not the ``Host`` header. The loopback settings sidecar enforces
its *own* Host/Origin allow-list (see ``stdio_settings_sidecar``), independent of
this setting. So defaulting fastmcp's guard off on the main server restores
exactly the pre-3.4.3 behaviour without giving up the protection that matters.

Operators who front ha-mcp differently can re-enable the guard (and pin their
own allow-lists) via ``FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=true`` plus
``FASTMCP_HTTP_ALLOWED_HOSTS`` / ``FASTMCP_HTTP_ALLOWED_ORIGINS``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: fastmcp's env var for the Host/Origin (DNS-rebinding) guard. Present as a
#: field on fastmcp's Settings only from 3.4.3 onward.
HOST_ORIGIN_PROTECTION_ENV = "FASTMCP_HTTP_HOST_ORIGIN_PROTECTION"

#: fastmcp Settings attribute backing the env var above.
_HOST_ORIGIN_PROTECTION_ATTR = "http_host_origin_protection"


def ensure_host_origin_guard_default_off() -> None:
    """Default fastmcp's Host/Origin guard off for ha-mcp's Streamable-HTTP servers.

    Idempotent and safe to call from every server-creation path. Call before the
    Streamable-HTTP app is built (``mcp.http_app()`` / ``mcp.run(transport="http")``
    / ``mcp.run_async(...)``): ``http_app`` reads
    ``fastmcp.settings.http_host_origin_protection`` at build time, so mutating
    that singleton beforehand deterministically neutralises the guard.

    Does nothing when the running fastmcp predates the guard (the setting field is
    absent, i.e. < 3.4.3) or when the operator set the env var explicitly (their
    choice wins). A failed mutation is logged at WARNING and left retryable -- it
    is not recorded as done -- so a later reload re-attempts it.
    """
    try:
        import fastmcp
    except Exception:  # pragma: no cover - fastmcp is a hard dependency
        return

    settings = getattr(fastmcp, "settings", None)
    if settings is None or not hasattr(settings, _HOST_ORIGIN_PROTECTION_ATTR):
        # fastmcp < 3.4.3: no guard exists, nothing to disable.
        return

    if getattr(settings, _HOST_ORIGIN_PROTECTION_ATTR) is False:
        # Already off -- a prior call, or the operator's own default. Idempotent.
        return

    if HOST_ORIGIN_PROTECTION_ENV in os.environ:
        # Operator explicitly opted in (guard on); honour their choice.
        return

    try:
        setattr(settings, _HOST_ORIGIN_PROTECTION_ATTR, False)
    except Exception:
        logger.warning(
            "Could not disable fastmcp's Host/Origin (DNS-rebinding) guard; MCP "
            "reached through a reverse proxy, tunnel, or LAN IP -- and the browser "
            "landing page -- may fail with 421/403. Set %s=false, or pin "
            "FASTMCP_HTTP_ALLOWED_HOSTS / FASTMCP_HTTP_ALLOWED_ORIGINS.",
            HOST_ORIGIN_PROTECTION_ENV,
            exc_info=True,
        )
        return

    # Belt: honoured by any fresh ``Settings()`` read and by child processes. Set
    # only after a confirmed mutation so a failed attempt stays retryable.
    os.environ.setdefault(HOST_ORIGIN_PROTECTION_ENV, "false")
