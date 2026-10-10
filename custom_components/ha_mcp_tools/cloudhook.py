"""Nabu Casa cloudhook support for the webhook ingress (#2696).

Settings → Home Assistant Cloud → Webhooks can expose the in-process server's
webhook at a ``hooks.nabu.casa`` URL. Home Assistant Cloud relays such a call
in-process as ``homeassistant.util.aiohttp.MockRequest`` — no ``read()``, no
transport — and returns only ``response.body`` to the cloud, so a reply on that
path must be buffered, never streamed. Real aiohttp requests are untouched.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiohttp import web

_LOGGER = logging.getLogger(__name__)

# A cloudhook must buffer the whole reply, and a subscription stream never
# ends: give up after this long instead of buffering it forever.
REPLY_SECONDS = 60
OAUTH_UNAVAILABLE = (
    "OAuth cannot be used over a cloudhook: Home Assistant Cloud relays only the "
    "Content-Type header back, so the WWW-Authenticate challenge never reaches the "
    "client. Connect with the secret webhook URL and auth mode 'none' instead."
)


async def buffered_response(
    cloudhook: bool, upstream_resp: aiohttp.ClientResponse, headers: dict[str, str]
) -> web.Response:
    """Buffer the upstream reply into a plain response, with a deadline for cloudhooks."""
    if not cloudhook:
        body = await upstream_resp.read()
    else:
        try:
            async with asyncio.timeout(REPLY_SECONDS):
                body = await upstream_resp.read()
        except TimeoutError:
            _LOGGER.error(
                "MCP webhook: cloudhook reply did not finish within %ds; "
                "a streaming reply cannot be relayed through Home Assistant Cloud",
                REPLY_SECONDS,
            )
            return web.Response(
                status=504,
                text="Streaming MCP replies cannot be relayed through a cloudhook",
            )
    return web.Response(status=upstream_resp.status, body=body, headers=headers)
