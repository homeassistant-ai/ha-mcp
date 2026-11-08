"""E2E coverage for user-configurable custom filesystem directories (#1567).

Proves the full chain end-to-end: the ``ha_mcp_tools.set_allowed_paths``
service persists a custom directory, the file tools grant read+write into it
LIVE (no HA restart), and the non-overridable deny floor still blocks
``.storage`` even when a malicious entry tries to add it.

Cross-lane (no backend marker). The component services are driven directly
over HA REST with the admin token (``utilities/component_services.py``);
the file tools are driven via the MCP client. The custom allowlist lives in
the component (HA process), so a set over REST is visible to the file handlers
the MCP client invokes — the same HA instance backs both.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.cleanup import CleanupSteps
from ...utilities.component_services import (
    bootstrap_caller_token,
    set_allowed_paths,
)

logger = logging.getLogger(__name__)

# Drives ha_mcp_tools.set_allowed_paths and the file tools, all of which
# register only in the "File & YAML Tools" config entry (#2292).
pytestmark = pytest.mark.requires_tools_entry


@pytest.mark.filesystem
class TestCustomFilesystemPaths:
    """The custom-directory allowlist applies live and respects the floor."""

    async def test_custom_dir_grants_read_write_live(
        self, mcp_client_fs, ha_container_with_fresh_config
    ):
        base_url = ha_container_with_fresh_config["base_url"]
        token = await bootstrap_caller_token(base_url)
        fname = f"pyscript/e2e_{uuid.uuid4().hex}.py"
        try:
            sr = await set_allowed_paths(base_url, token, ["pyscript"])
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
            with CleanupSteps() as cleanup:
                with cleanup.step():
                    await safe_call_tool(
                        mcp_client_fs,
                        "ha_delete_file",
                        {"path": fname, "confirm": True},
                    )
                with cleanup.step():
                    await set_allowed_paths(base_url, token, [])

    async def test_exact_file_grants_root_read_write_live(
        self, mcp_client_fs, ha_container_with_fresh_config
    ):
        base_url = ha_container_with_fresh_config["base_url"]
        token = await bootstrap_caller_token(base_url)
        fname = "sensor.yaml"
        try:
            sr = await set_allowed_paths(base_url, token, [fname])
            assert sr.get("paths") == [fname], sr

            async with MCPAssertions(mcp_client_fs) as mcp:
                written = await mcp.call_tool_success(
                    "ha_write_file",
                    {
                        "path": fname,
                        "content": "# 2268 exact-file test\n",
                        "overwrite": True,
                    },
                )
                assert written.get("success") is True, written
                read = await mcp.call_tool_success("ha_read_file", {"path": fname})
                assert "# 2268 exact-file test" in read.get("content", ""), read
        finally:
            with CleanupSteps() as cleanup:
                with cleanup.step():
                    await safe_call_tool(
                        mcp_client_fs,
                        "ha_delete_file",
                        {"path": fname, "confirm": True},
                    )
                with cleanup.step():
                    await set_allowed_paths(base_url, token, [])

    async def test_deny_floor_blocks_storage_even_with_custom_dir(
        self, mcp_client_fs, ha_container_with_fresh_config
    ):
        base_url = ha_container_with_fresh_config["base_url"]
        token = await bootstrap_caller_token(base_url)
        try:
            # Mixed batch: a deny-floor entry (dropped), a valid entry, and a
            # duplicate-after-normalization of it ("pyscript/" -> "pyscript").
            sr = await set_allowed_paths(
                base_url, token, [".storage", "pyscript", "pyscript/"]
            )
            # The deny floor drops .storage; the valid entry is kept once (the
            # normalized duplicate is deduped, not stored twice).
            assert ".storage" in sr.get("rejected", []), sr
            assert ".storage" not in sr.get("paths", []), sr
            assert sr.get("paths") == ["pyscript"], sr

            # Even with a valid custom dir configured, .storage stays blocked.
            data = await safe_call_tool(
                mcp_client_fs, "ha_read_file", {"path": ".storage/auth"}
            )
            assert data.get("success") is not True, (
                f".storage must remain unreadable regardless of the custom "
                f"allowlist; got: {data!r}"
            )
        finally:
            with CleanupSteps() as cleanup, cleanup.step():
                await set_allowed_paths(base_url, token, [])
