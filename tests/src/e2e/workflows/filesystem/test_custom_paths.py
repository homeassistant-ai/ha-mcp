"""E2E coverage for user-configurable custom filesystem directories (#1567).

Proves the full chain end-to-end: the ``ha_mcp_tools.set_allowed_paths``
service persists a custom directory, the file tools grant read+write into it
LIVE (no HA restart), and the non-overridable deny floor still blocks
``.storage`` even when a malicious entry tries to add it.

Cross-lane (no backend marker). The component services are driven directly
over HA REST with the admin token (mirrors ``test_ha_mcp_tools_refusal.py``);
the file tools are driven via the MCP client. The custom allowlist lives in
the component (HA process), so a set over REST is visible to the file handlers
the MCP client invokes — the same HA instance backs both.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool

logger = logging.getLogger(__name__)

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

    from fastmcp import Client

    client = Client(mcp_server.mcp)
    async with client:
        yield client


async def _bootstrap_token(base_url: str) -> str:
    """Fetch the caller token via the admin-gated bootstrap service."""
    import httpx

    from tests.test_constants import TEST_TOKEN

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/api/services/ha_mcp_tools/get_caller_token",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "Content-Type": "application/json",
            },
            params={"return_response": ""},
            json={},
        )
    assert resp.status_code == 200, f"get_caller_token failed: {resp.text!r}"
    service_response = resp.json().get("service_response", {})
    assert service_response.get("success") is True, f"bootstrap failed: {resp.text!r}"
    return service_response["token"]


async def _set_allowed_paths(base_url: str, token: str, paths: list[str]) -> dict:
    """Call set_allowed_paths with the admin token + caller token; return the
    structured service response."""
    import httpx

    from tests.test_constants import TEST_TOKEN

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/api/services/ha_mcp_tools/set_allowed_paths",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "Content-Type": "application/json",
            },
            params={"return_response": ""},
            json={"_ha_mcp_token": token, "paths": paths},
        )
    assert resp.status_code == 200, f"set_allowed_paths failed: {resp.text!r}"
    return resp.json().get("service_response", {})


@pytest.mark.filesystem
class TestCustomFilesystemPaths:
    """The custom-directory allowlist applies live and respects the floor."""

    async def test_custom_dir_grants_read_write_live(
        self, mcp_client_fs, ha_container_with_fresh_config
    ):
        base_url = ha_container_with_fresh_config["base_url"]
        token = await _bootstrap_token(base_url)
        fname = f"pyscript/e2e_{uuid.uuid4().hex}.py"
        try:
            sr = await _set_allowed_paths(base_url, token, ["pyscript"])
            assert sr.get("success") is True, sr
            assert "pyscript" in sr.get("paths", []), sr

            async with MCPAssertions(mcp_client_fs) as mcp:
                written = await mcp.call_tool_success(
                    "ha_write_file",
                    {"path": fname, "content": "# 1567 test", "overwrite": True},
                )
                assert written.get("success") is True, written
                read = await mcp.call_tool_success("ha_read_file", {"path": fname})
                assert "# 1567 test" in read.get("content", ""), read
        finally:
            await safe_call_tool(
                mcp_client_fs, "ha_delete_file", {"path": fname, "confirm": True}
            )
            await _set_allowed_paths(base_url, token, [])

    async def test_deny_floor_blocks_storage_even_with_custom_dir(
        self, mcp_client_fs, ha_container_with_fresh_config
    ):
        base_url = ha_container_with_fresh_config["base_url"]
        token = await _bootstrap_token(base_url)
        try:
            sr = await _set_allowed_paths(base_url, token, [".storage", "pyscript"])
            # The deny floor drops .storage; the valid entry is kept.
            assert ".storage" in sr.get("rejected", []), sr
            assert ".storage" not in sr.get("paths", []), sr
            assert "pyscript" in sr.get("paths", []), sr

            # Even with a valid custom dir configured, .storage stays blocked.
            data = await safe_call_tool(
                mcp_client_fs, "ha_read_file", {"path": ".storage/auth"}
            )
            assert data.get("success") is not True, (
                f".storage must remain unreadable regardless of the custom "
                f"allowlist; got: {data!r}"
            )
        finally:
            await _set_allowed_paths(base_url, token, [])
