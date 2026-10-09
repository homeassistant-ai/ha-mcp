"""Nabu Casa cloudhook support for the forwarder (#2696).

Settings → Home Assistant Cloud → Webhooks lists every registered webhook and
can expose it at a ``hooks.nabu.casa`` URL. Home Assistant Cloud relays such a
call in-process as ``homeassistant.util.aiohttp.MockRequest`` — no ``read()``,
no transport — and returns only ``response.body`` to the cloud, so a reply on
that path must be buffered, never streamed. Real aiohttp requests (the
documented ``/api/webhook/...`` URL) are untouched.
"""

from __future__ import annotations

from aiohttp import web
from homeassistant.util.aiohttp import MockRequest


def is_cloudhook(request: web.Request) -> bool:
    """True when Home Assistant Cloud relayed the call as a ``MockRequest``."""
    return isinstance(request, MockRequest)


async def read_body(request: web.Request) -> bytes:
    """Read the request body; ``MockRequest`` only exposes it via ``content``."""
    if is_cloudhook(request):
        return await request.content.read()
    return await request.read()
