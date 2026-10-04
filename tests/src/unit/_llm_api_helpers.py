"""Shared fakes for the ``llm_api`` unit tests (issue #1745).

Importing this module loads the component, so a test module imports it after it
has installed the ``homeassistant.*`` stubs.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from custom_components.ha_mcp_tools import llm_api
from custom_components.ha_mcp_tools.const import DOMAIN, EXPOSURE_FULL

FULL_ID = f"{DOMAIN}-entry-1745"


def make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}

    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_executor)
    return hass


def tool_entry(
    name: str = "ha_search",
    *,
    exposed: bool = True,
    pinned: bool = False,
    stamped: bool = True,
    description: str | None = None,
) -> SimpleNamespace:
    meta = (
        {"ha_mcp": {"llm_api_exposed": exposed, "pinned": pinned}} if stamped else None
    )
    return SimpleNamespace(
        name=name,
        description=description if description is not None else f"{name} description",
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
        meta=meta,
    )


def fake_session(
    monkeypatch,
    *,
    tools: list[Any] | None = None,
    instructions: str | None = "Use the skills-first workflow.",
    call_result: Any = None,
    raise_on_open: BaseException | None = None,
    delay: float = 0.0,
) -> SimpleNamespace:
    """Patch ``llm_api._mcp_session`` with a fake and return the session."""
    session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=tools or [])),
        call_tool=AsyncMock(return_value=call_result),
    )
    init_result = SimpleNamespace(instructions=instructions)

    @asynccontextmanager
    async def fake_mcp_session(url):
        """Stand in for ``_mcp_session``: record the url, yield the fake session."""
        session.url = url
        if raise_on_open is not None:
            raise raise_on_open
        if delay:
            await asyncio.sleep(delay)
        yield session, init_result

    monkeypatch.setattr(llm_api, "_mcp_session", fake_mcp_session)
    return session


def make_api(hass, mode: str = EXPOSURE_FULL) -> Any:
    return llm_api.HaMcpLlmApi(
        hass=hass,
        id=FULL_ID,
        name="HA-MCP Server",
        server_url="http://127.0.0.1:9584/private_x",
        mode=mode,
    )
