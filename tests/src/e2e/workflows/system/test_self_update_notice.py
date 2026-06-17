"""E2E tests for the self-update notice (``ha_mcp_update``) across status tools.

The standalone channels (pip / Docker / stdio) have no Supervisor-driven
auto-update, so the server surfaces a newer-release notice via
``ha_get_overview`` / ``ha_get_system_health`` / ``ha_get_updates``. These tests
exercise the real MCP client -> server -> tool -> ``update_check`` path against a
live HA and assert the field actually reaches the response.

Determinism without timing or a live PyPI dependency: the in-process server's
``_fetch_latest_from_pypi`` is monkeypatched to return a version far higher than
anything the test build could be running, so ``update_available`` is always true
and ``latest`` is a known constant. The lru-cached check is cleared so the next
call re-resolves with the patched fetch.

External-only: the monkeypatch reaches the server only when it runs in-process
(testcontainer / HAOS external). In inaddon mode ``mcp_client`` is an HTTP
transport into a separate addon container the test process can't patch — and the
add-on is auto-updated by the Supervisor there anyway.
"""

from __future__ import annotations

import logging

import pytest

from ha_mcp import update_check

from ...utilities.assertions import MCPAssertions

pytestmark = [pytest.mark.external_only]

logger = logging.getLogger(__name__)

# Far above any real or dev build version, so ``_is_newer`` is always true
# regardless of what the test build reports as its current version.
_FAKE_LATEST = "99.0.0"


@pytest.fixture(autouse=True)
def _reset_update_memo():
    """Clear the process-wide update-check memo after each test.

    ``get_update_info`` is ``lru_cache``-memoized for the process; without this a
    patched ``99.0.0`` result would leak into other e2e tests sharing the
    in-process server.
    """
    yield
    update_check.get_update_info.cache_clear()


@pytest.mark.system
class TestSelfUpdateNoticeSurfacedInTools:
    """Every status surface carries ``ha_mcp_update`` when an update is available."""

    @pytest.mark.parametrize(
        "tool,args",
        [
            ("ha_get_overview", {}),
            ("ha_get_system_health", {}),
            ("ha_get_updates", {}),
        ],
    )
    async def test_update_available_surfaced(
        self,
        mcp_client,
        monkeypatch: pytest.MonkeyPatch,
        tool: str,
        args: dict,
    ) -> None:
        monkeypatch.delenv("HA_MCP_DISABLE_UPDATE_CHECK", raising=False)
        # Patch the in-process server's PyPI fetch to a version guaranteed newer
        # than whatever this build reports, then clear the memo so the next tool
        # call re-resolves through the patched fetch.
        monkeypatch.setattr(
            update_check, "_fetch_latest_from_pypi", lambda _package: _FAKE_LATEST
        )
        update_check.get_update_info.cache_clear()

        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success(tool, args)

        assert "ha_mcp_update" in result, (
            f"{tool} did not surface ha_mcp_update: {sorted(result)}"
        )
        update = result["ha_mcp_update"]
        assert update["latest"] == _FAKE_LATEST
        assert update["update_available"] is True
        assert isinstance(update["current"], str) and update["current"]
