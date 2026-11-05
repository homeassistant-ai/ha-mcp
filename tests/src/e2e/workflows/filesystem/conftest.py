"""Fixtures shared by the filesystem E2E suites."""

from __future__ import annotations

import os

import pytest

FEATURE_FLAG = "HAMCP_ENABLE_FILESYSTEM_TOOLS"


@pytest.fixture(scope="module")
def _filesystem_feature_flag(ha_container_with_fresh_config):
    """Enable the filesystem feature flag for this module (see the sibling
    suites' identical fixtures)."""
    os.environ[FEATURE_FLAG] = "true"
    yield
    os.environ.pop(FEATURE_FLAG, None)


@pytest.fixture
async def mcp_client_fs(
    _filesystem_feature_flag,
    mcp_server,
    mcp_client,
    ha_container_with_fresh_config,
):
    """Yield an MCP client with filesystem tools available, in every lane."""
    if mcp_server is None:
        yield mcp_client
        return

    from ha_mcp._vendor.fastmcp import Client

    client = Client(mcp_server.mcp)
    async with client:
        yield client
