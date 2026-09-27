"""FastMCP lifespan shared by every launcher: background startup passes."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from ._version import is_embedded
from .client.rest_client import HomeAssistantClient
from .config import OAUTH_MODE_TOKEN, get_global_settings
from .hacs_auto_refresh import hacs_refresh_lifespan

logger = logging.getLogger(__name__)


async def warn_if_non_admin_token() -> None:
    """Ask Home Assistant about the configured token so a non-admin one is logged at startup (#2546)."""
    try:
        # The embedded server provisions its own admin token; OAuth mode has
        # no server-level token to ask about.
        settings = get_global_settings()
        if is_embedded() or settings.homeassistant_token == OAUTH_MODE_TOKEN:
            return
        async with HomeAssistantClient() as client:
            await client.token_is_admin()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Startup admin-token check skipped", exc_info=True)


@asynccontextmanager
async def server_lifespan(server: Any) -> AsyncIterator[dict[str, Any]]:
    """Run the HACS startup nudge and the admin-token check for the server's lifetime."""
    async with hacs_refresh_lifespan(server) as state:
        task = asyncio.create_task(warn_if_non_admin_token())
        try:
            yield state
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
