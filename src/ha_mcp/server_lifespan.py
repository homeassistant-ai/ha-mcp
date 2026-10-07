"""FastMCP lifespan shared by every launcher: background startup passes."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from ._version import is_embedded
from .app_discovery import announce_mcp_discovery
from .client.rest_client import HomeAssistantClient
from .config import OAUTH_MODE_TOKEN, get_global_settings
from .hacs_auto_refresh import hacs_refresh_lifespan

logger = logging.getLogger(__name__)


async def warn_if_non_admin_token() -> None:
    """Ask Home Assistant about the configured token so a non-admin one is logged at startup (#2546)."""
    try:
        # The embedded component checks its token belongs to an active
        # administrator on every start; OAuth mode has no server-level token.
        settings = get_global_settings()
        if is_embedded() or settings.homeassistant_token == OAUTH_MODE_TOKEN:
            return
        async with HomeAssistantClient() as client:
            if await client.token_is_admin() is None:
                logger.info(
                    "Could not ask Home Assistant whether the token is an "
                    "administrator's; admin-only requests go out one at a time "
                    "until the first one answers it"
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Startup admin-token check failed", exc_info=True)


@asynccontextmanager
async def server_lifespan(server: Any) -> AsyncIterator[dict[str, Any]]:
    """Run the HACS nudge, admin-token check and app discovery for the server's lifetime.

    Attached as the FastMCP ``lifespan`` so it runs on every launcher, including
    the app's ``start.py``, which calls ``mcp.run()`` directly.
    """
    async with hacs_refresh_lifespan(server) as state:
        tasks = [
            asyncio.create_task(warn_if_non_admin_token()),
            asyncio.create_task(announce_mcp_discovery()),
        ]
        try:
            yield state
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
