"""A loopback server whose answer is not HTTP, for the relay failure tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def garbled_upstream() -> AsyncIterator[int]:
    """The port of a server that answers each request with bytes aiohttp cannot
    parse. aiohttp's error for that quotes the request URL, path included, so a
    relay that logs or returns the error text gives the secret path away."""

    async def answer(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readline()
        writer.write(b"this is not http\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()
