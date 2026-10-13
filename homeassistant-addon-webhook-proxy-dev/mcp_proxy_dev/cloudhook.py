"""Nabu Casa cloudhook support for the forwarder (#2696).

Settings → Home Assistant Cloud → Webhooks lists every registered webhook and
can expose it at a ``hooks.nabu.casa`` URL. Home Assistant Cloud relays such a
call in-process as ``homeassistant.util.aiohttp.MockRequest`` — no ``read()``,
no transport — and returns only ``response.body`` to the cloud, so a reply on
that path must be buffered, never streamed. Real aiohttp requests (the
documented ``/api/webhook/...`` URL) are untouched.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiohttp import web
from homeassistant.util.aiohttp import MockRequest

_LOGGER = logging.getLogger(__name__)

# A cloudhook must buffer the whole reply, and a subscription stream never
# ends: give up after this long instead of buffering it forever.
CLOUDHOOK_REPLY_SECONDS = 60
CLOUDHOOK_OAUTH_UNAVAILABLE = (
    "OAuth sign-in cannot start over a cloudhook: Home Assistant Cloud relays only the "
    "Content-Type header back, so the WWW-Authenticate challenge never reaches the "
    "client. Connect with the secret webhook URL and no OAuth instead."
)


def is_cloudhook(request: web.Request) -> bool:
    """True when Home Assistant Cloud relayed the call as a ``MockRequest``."""
    return isinstance(request, MockRequest)


async def read_body(request: web.Request) -> bytes:
    """Read the request body; ``MockRequest`` only exposes it via ``content``."""
    if is_cloudhook(request):
        return await request.content.read()
    return await request.read()


async def buffered_response(
    request: web.Request, upstream_resp: aiohttp.ClientResponse, headers: dict[str, str]
) -> web.Response:
    """Buffer the upstream reply into a plain response, with a deadline for cloudhooks."""
    if not is_cloudhook(request):
        body = await upstream_resp.read()
    else:
        try:
            async with asyncio.timeout(CLOUDHOOK_REPLY_SECONDS):
                body = await upstream_resp.read()
        except TimeoutError:
            _LOGGER.error(
                "MCP Proxy: cloudhook reply did not finish within %ds; "
                "a streaming reply cannot be relayed through Home Assistant Cloud",
                CLOUDHOOK_REPLY_SECONDS,
            )
            return web.Response(
                status=504,
                text="Streaming MCP replies cannot be relayed through a cloudhook",
            )
    return web.Response(status=upstream_resp.status, body=body, headers=headers)


def discovery_rejection(request: web.Request) -> web.Response | None:
    """Refuse OAuth discovery over a cloudhook.

    Home Assistant Cloud relays only ``Content-Type`` back, so the 401's
    ``WWW-Authenticate`` challenge can never reach the client.
    """
    if not is_cloudhook(request):
        return None
    return web.Response(status=400, text=CLOUDHOOK_OAUTH_UNAVAILABLE)
