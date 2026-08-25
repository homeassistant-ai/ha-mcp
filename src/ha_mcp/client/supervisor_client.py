"""Shared factory for direct-Supervisor httpx clients.

Several code paths call the Home Assistant Supervisor REST API directly with
a short-lived client configured for the Supervisor base URL and
``SUPERVISOR_TOKEN``. They include log collection, app (add-on) management,
and backup operations, screenshot-engine discovery, and app restart.

These calls cannot use ``HomeAssistantClient.httpx_client``, which targets
Home Assistant Core rather than Supervisor. In app mode it carries the same
token value, but requests use a different base URL and authorization surface.
This module centralizes the direct transport setup.
"""

from __future__ import annotations

import os
import ssl

import httpx

from .._version import get_supervisor_base_url

__all__ = ["make_supervisor_httpx_client"]


def make_supervisor_httpx_client(
    *,
    timeout: float | httpx.Timeout,
    verify: bool | str | ssl.SSLContext,
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
            HTTPS in non-add-on test rigs. The full httpx ``verify`` surface
            (``bool``, CA-bundle path, or :class:`ssl.SSLContext`) is
            accepted and forwarded verbatim.

    Returns:
        A new :class:`httpx.AsyncClient` bound to the Supervisor base URL
        with ``Authorization: Bearer ${SUPERVISOR_TOKEN}`` preset. Callers
        pass relative paths (``/addons/self/logs``) to ``client.get/post``;
        ``base_url`` joins them onto the Supervisor host.

    Raises:
        RuntimeError: ``SUPERVISOR_TOKEN`` is unset or empty in the
            environment. Callers translate that failure for their own
            response surface. Detecting it here prevents a malformed
            ``Authorization: Bearer `` header from masking the missing
            environment variable as a token rejection from Supervisor.

    Note:
        ``SUPERVISOR_TOKEN`` is read from env at construction time and
        baked into the constructed client's ``Authorization`` header.
        Reusing a single client across token rotations would not pick up
        the new value — short-lived ``async with`` callers are unaffected,
        but a future long-lived caller would need to discard and re-create.
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError(
            "SUPERVISOR_TOKEN is not set; "
            "make_supervisor_httpx_client cannot construct an "
            "authenticated client. Callers must verify the token is "
            "present before invoking the factory."
        )
    return httpx.AsyncClient(
        base_url=get_supervisor_base_url(),
        timeout=timeout,
        verify=verify,
        headers={"Authorization": f"Bearer {token}"},
    )
