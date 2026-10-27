"""Compatibility checks for screenshot-engine API differences."""

from __future__ import annotations

import asyncio
import json

import httpx
from packaging.version import InvalidVersion, Version

from .provision import EngineKind

MAX_IDENTIFICATION_BODY_BYTES = 4096
IDENTIFICATION_TIMEOUT_SECONDS = 2.0


async def detect_engine_kind_from_capabilities(
    http_client: httpx.AsyncClient, engine_url: str
) -> EngineKind | None:
    """Identify WPE at an explicit URL when its HTTP 400 makes it relevant."""
    try:
        async with asyncio.timeout(IDENTIFICATION_TIMEOUT_SECONDS):
            async with http_client.stream(
                "GET",
                f"{engine_url}/capabilities",
                timeout=IDENTIFICATION_TIMEOUT_SECONDS,
            ) as response:
                if response.status_code != 200:
                    return None
                content_type = response.headers.get("content-type", "")
                if (
                    content_type.split(";", maxsplit=1)[0].strip().lower()
                    != "application/json"
                ):
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    remaining = MAX_IDENTIFICATION_BODY_BYTES - len(body)
                    if len(chunk) > remaining:
                        body.extend(chunk[:remaining])
                        return None
                    body.extend(chunk)
    except (httpx.HTTPError, TimeoutError):
        return None

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if (
        isinstance(payload, dict)
        and payload.get("name") == "WPE Screenshot Engine"
        and payload.get("api_version") == 1
    ):
        return "wpe"
    return None


def is_legacy_puppet(engine_kind: EngineKind | None, version: str | None) -> bool:
    """Return whether a verified Puppet version predates WIDTHxauto support."""
    if engine_kind != "puppet" or not version:
        return False
    try:
        return Version(version) < Version("2.5.0")
    except InvalidVersion:
        return False
