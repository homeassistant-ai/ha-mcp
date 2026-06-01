"""Unit tests for the consolidated HACS action tools.

Exercise the per-action handler success paths (``_hacs_info`` /
``_hacs_download`` / ``_hacs_add_repository``) and the dispatcher's
error-routing with a mocked WebSocket client. Complements the
validation-guard tests in ``test_identifier_validation_family.py`` and the
ctx/progress test in ``test_context_injection.py``.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_hacs import HACS_ADD_REGISTRATION_TIMEOUT, HacsTools


async def _identity_timezone(_client, data):
    """Stand-in for add_timezone_metadata that returns data unchanged."""
    return data


def _ws(result):
    """A WS client whose send_command returns a successful HACS response."""
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value={"success": True, "result": result})
    return ws


@contextmanager
def _patched_hacs(ws):
    """Patch HACS availability, the WS client factory, and tz metadata."""
    with (
        patch(
            "ha_mcp.tools.tools_hacs._assert_hacs_available",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ha_mcp.tools.tools_hacs.add_timezone_metadata",
            new=_identity_timezone,
        ),
    ):
        yield


@pytest.fixture
def tools():
    return HacsTools(MagicMock())


class TestGetHacsInfo:
    async def test_info_returns_repository_detail(self, tools):
        ws = _ws(
            {
                "name": "Mushroom",
                "full_name": "piitaya/lovelace-mushroom",
                "category": "plugin",
                "installed": True,
                "installed_version": "4.0.0",
            }
        )
        with _patched_hacs(ws):
            result = await tools.ha_get_hacs_info(
                action="info", repository_id="441028036"
            )

        assert result["success"] is True
        # Echoes the caller's identifier and surfaces the structured fields.
        assert result["repository_id"] == "441028036"
        assert result["name"] == "Mushroom"
        assert result["installed"] is True
        assert result["installed_version"] == "4.0.0"
        # A numeric id needs no resolution round-trip — exactly one WS call.
        ws.send_command.assert_awaited_once()
        assert ws.send_command.await_args.args[0] == "hacs/repository/info"


class TestManageHacsDownload:
    async def test_download_defaults_version_to_latest(self, tools):
        ws = _ws({"status": "ok"})
        with _patched_hacs(ws):
            result = await tools.ha_manage_hacs(
                action="download", repository_id="441028036"
            )

        assert result["success"] is True
        assert result["version"] == "latest"  # no version given -> "latest"
        assert result["repository"] == "441028036"  # numeric id resolves to itself
        assert "Successfully installed" in result["message"]
        assert ws.send_command.await_args.args[0] == "hacs/repository/download"


class TestManageHacsAddRepository:
    async def test_add_repository_translates_category_and_returns_registered_id(
        self, tools
    ):
        # The user-facing "lovelace" category must reach HACS as its internal
        # name "plugin" (CATEGORY_MAP), and the returned id comes from the repo
        # that actually registers — the add ack itself carries no id.
        ws = _ws({})
        registered = {"id": "999", "full_name": "owner/my-card", "name": "My Card"}
        with (
            _patched_hacs(ws),
            patch(
                "ha_mcp.tools.tools_hacs.wait_for_repo_registration",
                new_callable=AsyncMock,
            ) as wait_mock,
        ):
            wait_mock.return_value = registered
            result = await tools.ha_manage_hacs(
                action="add_repository",
                repository="owner/my-card",
                category="lovelace",
            )

        assert result["success"] is True
        assert result["repository_id"] == "999"
        # The add path confirms registration with the fail-fast budget, not the
        # 30 s resolve/download default.
        assert (
            wait_mock.await_args.kwargs.get("timeout") == HACS_ADD_REGISTRATION_TIMEOUT
        )
        ws.send_command.assert_awaited_once()
        assert ws.send_command.await_args.args[0] == "hacs/repositories/add"
        assert ws.send_command.await_args.kwargs["category"] == "plugin"
        assert ws.send_command.await_args.kwargs["repository"] == "owner/my-card"

    async def test_add_repository_errors_when_repo_never_registers(self, tools):
        # HACS accepts the add command but the repository never appears in the
        # list (archived / invalid / wrong category). The tool must surface an
        # error rather than a false "Successfully added".
        ws = _ws({})
        with (
            _patched_hacs(ws),
            patch(
                "ha_mcp.tools.tools_hacs.wait_for_repo_registration",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await tools.ha_manage_hacs(
                action="add_repository",
                repository="owner/archived",
                category="integration",
            )
        assert "SERVICE_CALL_FAILED" in str(excinfo.value)
        assert "did not register" in str(excinfo.value)

    async def test_add_repository_rejects_slashless_format_before_ws(self, tools):
        # The "owner/repo" format guard lives after _assert_hacs_available but
        # before the WS add; the e2e test for it skips when HACS is
        # unavailable, so pin it deterministically here.
        ws = _ws({})
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="add_repository",
                repository="no-slash",
                category="integration",
            )
        assert "VALIDATION_INVALID_PARAMETER" in str(excinfo.value)
        assert "format" in str(excinfo.value).lower()
        ws.send_command.assert_not_awaited()


class TestDispatcherErrorRouting:
    async def test_unexpected_handler_error_is_wrapped_with_action_context(self, tools):
        # A non-ToolError escaping a handler must be converted to a structured
        # ToolError carrying the tool + action context (Pattern A wrap branch).
        with (
            patch.object(
                HacsTools,
                "_hacs_search",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await tools.ha_get_hacs_info(action="search")
        msg = str(excinfo.value)
        assert "ha_get_hacs_info" in msg
        assert "search" in msg

    async def test_structured_toolerror_passes_through_unwrapped(self, tools):
        # A structured ToolError raised inside a handler must propagate with its
        # original error code intact, not be re-wrapped as INTERNAL_ERROR.
        sentinel = ToolError('{"error": {"code": "RESOURCE_NOT_FOUND"}}')
        with (
            patch.object(HacsTools, "_hacs_info", new=AsyncMock(side_effect=sentinel)),
            pytest.raises(ToolError) as excinfo,
        ):
            await tools.ha_get_hacs_info(action="info", repository_id="123")
        assert "RESOURCE_NOT_FOUND" in str(excinfo.value)
        assert "INTERNAL_ERROR" not in str(excinfo.value)
