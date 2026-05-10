"""Shared factory for direct-Supervisor httpx clients.

Three call sites in the codebase talk directly to the Home Assistant
Supervisor REST API at ``http://supervisor`` rather than through
``HomeAssistantClient.httpx_client`` (which is bound to HA Core, not the
Supervisor — different base URL, different token, different role gate):

- :meth:`ha_mcp.client.rest_client.HomeAssistantClient._supervisor_logs_get`
  — fetches addon and system-service logs (#1116, #1126)
- :func:`ha_mcp.tools.tools_bug_report._fetch_addon_logs` — bundles ha-mcp's
  own addon logs into a bug-report payload
- :func:`ha_mcp.settings_ui._restart_addon` — POSTs ``/addons/self/restart``
  from the settings UI

All three share the same boilerplate (base URL, ``Authorization: Bearer
${SUPERVISOR_TOKEN}`` header), so this module supplies a single factory and
keeps the three sites consistent.

The token is read from env at construction time; the caller is responsible
for token-absent handling because each site has its own policy (a rich
:class:`HomeAssistantAuthError`, a silent ``""`` return, or a 400
``JSONResponse``) that does not share a common shape.
"""

from __future__ import annotations

import os

import httpx

from .._version import get_supervisor_base_url

__all__ = ["make_supervisor_httpx_client"]


def make_supervisor_httpx_client(
    *,
    timeout: float | httpx.Timeout,
    verify: bool,
) -> httpx.AsyncClient:
    """Construct an ``httpx.AsyncClient`` pre-configured for the Supervisor REST API.

    Args:
        timeout: Per-request timeout. Accepts either a plain ``float``
            (seconds, applied to all phases) or a full :class:`httpx.Timeout`
            for finer-grained control.
        verify: TLS verify policy. A no-op for the default
            ``http://supervisor`` base URL (plain HTTP — no TLS to verify),
            but kept as a parameter because :func:`get_supervisor_base_url`
            honours ``SUPERVISOR_BASE_URL`` env-var overrides that may be
            HTTPS in non-add-on test rigs.

    Returns:
        A new :class:`httpx.AsyncClient` bound to the Supervisor base URL
        with ``Authorization: Bearer ${SUPERVISOR_TOKEN}`` preset. Callers
        pass relative paths (``/addons/self/logs``) to ``client.get/post``;
        ``base_url`` joins them onto the Supervisor host.

        ``SUPERVISOR_TOKEN`` is read from env at construction time. An absent
        or empty value produces a literal ``"Bearer "`` header — callers are
        expected to short-circuit before reaching here (see module docstring
        for the per-site policies).
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    return httpx.AsyncClient(
        base_url=get_supervisor_base_url(),
        timeout=timeout,
        verify=verify,
        headers={"Authorization": f"Bearer {token}"},
    )
