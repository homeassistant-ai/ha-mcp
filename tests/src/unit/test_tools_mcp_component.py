"""Unit tests for tools_mcp_component module.

Tests the ha_install_mcp_tools error handling path to verify that
exceptions are properly converted to ToolError with structured error
information and HACS-specific suggestions.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_mcp_component import MCP_TOOLS_REPO, McpComponentTools


class TestHaInstallMcpToolsErrorHandling:
    """Tests for the exception handler in ha_install_mcp_tools."""

    @pytest.fixture
    def tools(self):
        """Create McpComponentTools instance with a mock client."""
        return McpComponentTools(AsyncMock())

    @pytest.mark.asyncio
    async def test_exception_raises_tool_error(self, tools):
        """Exceptions in ha_install_mcp_tools should raise ToolError, not return a dict."""
        mock_check = AsyncMock(side_effect=RuntimeError("Unexpected HACS failure"))
        with (
            patch("ha_mcp.tools.tools_hacs._assert_hacs_available", mock_check),
            pytest.raises(ToolError) as exc_info,
        ):
            await tools.ha_install_mcp_tools(restart=False)

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False

    @pytest.mark.asyncio
    async def test_exception_includes_hacs_suggestions(self, tools):
        """ToolError from ha_install_mcp_tools should include HACS-specific suggestions."""
        mock_check = AsyncMock(side_effect=ConnectionError("Cannot reach HACS"))
        with (
            patch("ha_mcp.tools.tools_hacs._assert_hacs_available", mock_check),
            pytest.raises(ToolError) as exc_info,
        ):
            await tools.ha_install_mcp_tools(restart=False)

        error_data = json.loads(str(exc_info.value))
        suggestions = error_data["error"]["suggestions"]
        assert any("HACS" in s for s in suggestions)
        assert any("hacs.xyz" in s for s in suggestions)
        assert any("GitHub" in s for s in suggestions)

    @pytest.mark.asyncio
    async def test_exception_preserves_tool_context(self, tools):
        """ToolError should include the tool name and restart parameter in context."""
        mock_check = AsyncMock(side_effect=RuntimeError("Something went wrong"))
        with (
            patch("ha_mcp.tools.tools_hacs._assert_hacs_available", mock_check),
            pytest.raises(ToolError) as exc_info,
        ):
            await tools.ha_install_mcp_tools(restart=True)

        error_data = json.loads(str(exc_info.value))
        assert error_data.get("tool") == "ha_install_mcp_tools"
        assert error_data.get("restart") is True


def _list_response_with_repo(repo_id: int = 42) -> dict:
    return {
        "success": True,
        "result": [
            {"full_name": MCP_TOOLS_REPO, "id": repo_id, "installed": False},
        ],
    }


def _list_response_empty() -> dict:
    return {"success": True, "result": []}


def _build_ws_client(
    list_responses: list[dict],
    subscribe_result: tuple[int, "asyncio.Queue"] | Exception = (1, None),
):
    """Build a MagicMock WS client whose ``send_command`` returns each list_responses entry in turn.

    ``subscribe_command`` returns ``subscribe_result`` directly (or raises if
    Exception). ``unsubscribe_command`` is a no-op AsyncMock.
    """
    ws_client = MagicMock()
    ws_client.send_command = AsyncMock(side_effect=list_responses)

    if isinstance(subscribe_result, Exception):
        ws_client.subscribe_command = AsyncMock(side_effect=subscribe_result)
    else:
        ws_client.subscribe_command = AsyncMock(return_value=subscribe_result)

    ws_client.unsubscribe_command = AsyncMock()
    return ws_client


class TestWaitForRepoRegistration:
    """Subscription-driven helper that replaces the old 10x1s blind poll.

    Lives in ``tools_hacs`` so both the installer flow
    (``ha_install_mcp_tools``) and the download flow
    (``ha_hacs_download`` via ``_resolve_hacs_repo_id``) can share it.
    """

    @pytest.mark.asyncio
    async def test_post_subscribe_sample_finds_repo_already_listed(self):
        """Repo already in the post-subscribe list — return without waiting."""
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        ws_client = _build_ws_client(
            list_responses=[_list_response_with_repo(repo_id=42)],
            subscribe_result=(7, queue),
        )

        repo = await wait_for_repo_registration(ws_client, MCP_TOOLS_REPO)

        assert repo is not None
        assert str(repo.get("id")) == "42"
        ws_client.subscribe_command.assert_awaited_once()
        ws_client.unsubscribe_command.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_event_triggers_targeted_list_lookup(self):
        """Matching dispatch event → fresh list lookup to get the full entry."""
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(
            {
                "id": 7,
                "type": "event",
                "event": {
                    "action": "registration",
                    "repository": MCP_TOOLS_REPO,
                    "repository_id": 99,
                },
            }
        )
        ws_client = _build_ws_client(
            # Post-subscribe sample: empty. Then event arrives; helper
            # re-lists to pick up the full entry.
            list_responses=[
                _list_response_empty(),
                _list_response_with_repo(repo_id=99),
            ],
            subscribe_result=(7, queue),
        )

        repo = await wait_for_repo_registration(ws_client, MCP_TOOLS_REPO)

        assert repo is not None
        assert str(repo.get("id")) == "99"
        assert ws_client.send_command.await_count == 2

    @pytest.mark.asyncio
    async def test_unrelated_event_does_not_recheck_list(self):
        """Unrelated dispatch must NOT trigger a list lookup.

        HACS' ``hacs/repositories/list`` payload can be 2 MB+ on busy
        installs; re-listing on every unrelated dispatch event would
        defeat the whole point of using the dispatcher as the signal.
        The list re-check belongs on the backstop-poll path only.
        """
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(
            {
                "id": 7,
                "type": "event",
                "event": {
                    "action": "registration",
                    "repository": "someone-else/other-repo",
                    "repository_id": 1,
                },
            }
        )
        ws_client = MagicMock()
        # send_command is called once for the post-subscribe sample,
        # then must NOT be called again for the unrelated event —
        # the test would block on the empty queue otherwise, so the
        # short backstop interval ensures the timeout fires and we
        # assert send_command was called exactly once (sample-only).
        ws_client.send_command = AsyncMock(return_value=_list_response_empty())
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.unsubscribe_command = AsyncMock()

        repo = await wait_for_repo_registration(
            ws_client, MCP_TOOLS_REPO, timeout=0.05, backstop_poll_interval=0.02
        )

        assert repo is None
        # 1 = post-subscribe sample. Anything more means we re-listed
        # on the unrelated event (1 extra) or on the backstop tick
        # (allowed — but with timeout=0.05 we may see one backstop poll
        # and at most one final list lookup for the queue-shutdown
        # path; cap at 3 to fail loudly on regression to per-event
        # re-listing).
        assert ws_client.send_command.await_count <= 3, (
            f"send_command should not be called per-event; saw "
            f"{ws_client.send_command.await_count} calls"
        )

    @pytest.mark.asyncio
    async def test_subscribe_failure_falls_back_to_single_list_lookup(self):
        """If ``hacs/subscribe`` fails, do one list lookup as fallback."""
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        ws_client = _build_ws_client(
            list_responses=[_list_response_with_repo(repo_id=42)],
            subscribe_result=RuntimeError("HACS subscribe blew up"),
        )

        repo = await wait_for_repo_registration(ws_client, MCP_TOOLS_REPO)

        assert repo is not None
        assert str(repo.get("id")) == "42"
        ws_client.unsubscribe_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_returns_none_after_budget(self):
        """Wall-clock backstop fires when neither event nor list shows the repo."""
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        ws_client = MagicMock()
        ws_client.send_command = AsyncMock(return_value=_list_response_empty())
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.unsubscribe_command = AsyncMock()

        repo = await wait_for_repo_registration(
            ws_client, MCP_TOOLS_REPO, timeout=0.05, backstop_poll_interval=0.02
        )

        assert repo is None
        ws_client.unsubscribe_command.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_queue_shutdown_attempts_one_last_lookup(self):
        """Mid-wait connection teardown: try one final list lookup before giving up."""
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        queue.shutdown(immediate=True)

        ws_client = _build_ws_client(
            list_responses=[
                _list_response_empty(),  # post-subscribe sample finds nothing
                _list_response_with_repo(repo_id=42),  # last-chance lookup
            ],
            subscribe_result=(7, queue),
        )

        repo = await wait_for_repo_registration(ws_client, MCP_TOOLS_REPO)

        assert repo is not None
        assert str(repo.get("id")) == "42"
        ws_client.unsubscribe_command.assert_awaited_once_with(7)


class TestResolveHacsRepoIdUsesWait:
    """``_resolve_hacs_repo_id`` for GitHub paths now routes through the
    subscribe-based waiter so the post-add race is handled the same way
    as in the installer flow."""

    @pytest.mark.asyncio
    async def test_numeric_id_short_circuits(self):
        """Pre-resolved numeric ids must NOT subscribe — just pass through."""
        from ha_mcp.tools.tools_hacs import _resolve_hacs_repo_id

        ws_client = MagicMock()
        ws_client.subscribe_command = AsyncMock()  # must not be called

        numeric_id, display_name = await _resolve_hacs_repo_id(ws_client, "441028036")

        assert numeric_id == "441028036"
        assert display_name == "441028036"
        ws_client.subscribe_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_path_uses_subscribe_based_wait(self):
        """Github-path identifiers route through ``wait_for_repo_registration``."""
        from ha_mcp.tools.tools_hacs import _resolve_hacs_repo_id

        queue: asyncio.Queue = asyncio.Queue()
        ws_client = MagicMock()
        ws_client.send_command = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {
                        "full_name": "piitaya/lovelace-mushroom",
                        "id": 12345,
                        "name": "Mushroom",
                    },
                ],
            }
        )
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.unsubscribe_command = AsyncMock()

        numeric_id, display_name = await _resolve_hacs_repo_id(
            ws_client, "piitaya/lovelace-mushroom"
        )

        assert numeric_id == "12345"
        assert display_name == "Mushroom"
        ws_client.subscribe_command.assert_awaited_once()
        ws_client.unsubscribe_command.assert_awaited_once_with(7)
