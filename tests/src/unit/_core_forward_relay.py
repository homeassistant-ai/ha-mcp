"""A recording relay session and answer for the core-forward TLS tests."""

from __future__ import annotations

from typing import Any

import aiohttp


class _CoreAnswer:
    status = 200
    content_type = "application/json"

    async def __aenter__(self) -> _CoreAnswer:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def read(self) -> bytes:
        return b'{"access_token":"core"}'


class _RelaySession:
    """Rejects a verified https hop: core's certificate names neither
    127.0.0.1 nor a private-CA host the container trusts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _CoreAnswer:
        self.calls.append((url, kwargs))
        if url.startswith("https://") and kwargs.get("ssl") is not False:
            raise aiohttp.ClientError("certificate verify failed")
        return _CoreAnswer()
