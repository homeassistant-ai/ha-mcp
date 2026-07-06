"""Transport-security defaults for ha-mcp's Streamable-HTTP servers.

fastmcp >= 3.4.3 ships ``HostOriginGuardMiddleware`` (a DNS-rebinding guard) that
is **on by default**. Inserted at the front of the ASGI stack, it rejects any
request whose ``Host`` header is not loopback / an explicitly-allowed host with
``421 Misdirected Request``, and any browser ``Origin`` that is not same-origin /
loopback / explicitly allowed with ``403 Forbidden`` -- before the request
reaches any route.

That default is wrong for ha-mcp. We are *designed* to be reached through
operator-chosen reverse proxies and tunnels (Cloudflare Tunnel, nginx, Traefik,
Nabu Casa Remote UI) and via direct LAN IPs, whose ``Host`` values we cannot
enumerate. Leaving the guard on would ``421`` the majority of real deployments --
including the plain browser landing page, which is a no-``Origin`` navigation
that still trips the ``Host`` check.

Our security boundary is the high-entropy secret path (and, in OAuth mode, the
per-user token), not the ``Host`` header. The genuinely unauthenticated loopback
surface -- the settings sidecar -- already enforces its *own* Host/Origin
allow-list (see ``stdio_settings_sidecar``). So defaulting fastmcp's guard off on
the main server restores exactly the pre-3.4.3 behaviour without giving up the
protection that matters.

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
    """Default fastmcp's Host/Origin guard off for ha-mcp HTTP servers.

    Idempotent. Call once before building/serving any Streamable-HTTP app
    (``mcp.http_app()`` / ``mcp.run(transport="http"|"sse")`` /
    ``mcp.run_async(...)``) -- ``http_app`` reads
    ``fastmcp.settings.http_host_origin_protection`` at build time, so mutating
    that singleton beforehand deterministically neutralises the guard.

    No-op when:
    - the operator has set ``FASTMCP_HTTP_HOST_ORIGIN_PROTECTION`` explicitly
      (either direction -- their choice wins), or
    - the running fastmcp predates the guard (the setting field is absent).
    """
    if HOST_ORIGIN_PROTECTION_ENV in os.environ:
        # Respect an explicit operator choice in either direction.
        return

    # Belt: honoured by any fresh ``Settings()`` read and by child processes.
    os.environ[HOST_ORIGIN_PROTECTION_ENV] = "false"

    # Suspenders (load-bearing): fastmcp's ``http_app`` reads the already-built
    # module-global settings singleton, so mutate it directly.
    try:
        import fastmcp

        settings = getattr(fastmcp, "settings", None)
        if settings is not None and hasattr(settings, _HOST_ORIGIN_PROTECTION_ATTR):
            setattr(settings, _HOST_ORIGIN_PROTECTION_ATTR, False)
    except Exception:  # pragma: no cover - defensive: never block startup
        logger.debug("Could not default fastmcp Host/Origin guard off", exc_info=True)
