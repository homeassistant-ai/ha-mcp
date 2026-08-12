"""Unit tests for the consolidated HACS action tools.

Exercise the per-action handler success paths (``_hacs_info`` /
``_hacs_download`` / ``_hacs_remove`` / ``_hacs_update_information`` /
``_hacs_add_repository``) and the dispatcher's error-routing with a mocked
WebSocket client. Complements the validation-guard tests in
``test_identifier_validation_family.py`` and the ctx/progress test in
``test_context_injection.py``.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_hacs import (
    HACS_ADD_REGISTRATION_TIMEOUT,
    HACS_RESOLVE_REGISTRATION_TIMEOUT,
    HacsTools,
)


def test_resolve_budget_stays_fast() -> None:
    """A regression back to a slow resolve or post-add budget would re-open the
    #1515 not-found stall; pin both fast budgets explicitly (the waiter itself
    has no default budget — every caller supplies its own)."""
    assert HACS_RESOLVE_REGISTRATION_TIMEOUT <= 10
    assert HACS_ADD_REGISTRATION_TIMEOUT <= 10


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

    async def test_info_command_error_keeps_command_context(self, tools):
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=HomeAssistantCommandError(
                "Command failed: kaboom", "unknown_error"
            )
        )
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_get_hacs_info(action="info", repository_id="441028036")
        assert "kaboom" in str(excinfo.value)
        assert "hacs/repository/info" in str(excinfo.value)


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

    async def test_owner_repo_resolve_uses_fast_fail_budget(self, tools):
        # A GitHub-path download resolves owner/repo -> numeric id via
        # wait_for_repo_registration. That lookup targets an already-registered
        # repo, so it must use the short HACS_RESOLVE_REGISTRATION_TIMEOUT, not
        # the 30 s post-add registration budget — otherwise a not-found path
        # stalls the caller (and the E2E suite, #1515) for 30 s.
        ws = _ws({"status": "ok"})
        registered = {"id": "555", "full_name": "owner/repo", "name": "Repo"}
        with (
            _patched_hacs(ws),
            patch(
                "ha_mcp.tools.tools_hacs.wait_for_repo_registration",
                new_callable=AsyncMock,
            ) as wait_mock,
        ):
            wait_mock.return_value = registered
            result = await tools.ha_manage_hacs(
                action="download", repository_id="owner/repo"
            )

        assert result["success"] is True
        assert result["repository"] == "Repo"  # resolved display name
        assert (
            wait_mock.await_args.kwargs.get("timeout")
            == HACS_RESOLVE_REGISTRATION_TIMEOUT
        )

    async def test_download_timeout_reports_the_real_budget(self, tools):
        # Download runs on the same 60 s budget as remove/refresh; without its
        # own timeout context the classifier claims the 30 s default, and a
        # false failure invites a blind retry of work HACS finishes anyway.
        from ha_mcp.client.rest_client import HomeAssistantCommandTimeout

        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=HomeAssistantCommandTimeout("Command timeout")
        )
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="download", repository_id="441028036")
        msg = str(excinfo.value)
        assert "60" in msg
        assert "Operation 'hacs/repository/download'" in msg
        assert "may still have completed" in msg

    async def test_download_command_error_keeps_command_context(self, tools):
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=HomeAssistantCommandError(
                "Command failed: kaboom", "unknown_error"
            )
        )
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="download", repository_id="441028036")
        assert "kaboom" in str(excinfo.value)
        assert "hacs/repository/download" in str(excinfo.value)

    async def test_download_command_timeout_error_reports_real_budget(self, tools):
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=HomeAssistantCommandError(
                "Command failed: backend timeout", "unknown_error"
            )
        )
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="download", repository_id="441028036")
        assert "Operation 'hacs/repository/download' timed out after 60.0s" in str(
            excinfo.value
        )


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


def _remove_ws(*, installed: bool = True, remove_response: dict | None = None):
    """A WS client scripted for the remove path: info probe, then remove."""
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        side_effect=[
            {"success": True, "result": {"installed": installed}},
            remove_response or {"success": True, "result": {}},
        ]
    )
    return ws


class TestManageHacsRemove:
    async def test_remove_by_numeric_id_sends_remove_command(self, tools):
        ws = _remove_ws()
        with _patched_hacs(ws):
            result = await tools.ha_manage_hacs(
                action="remove", repository_id="401454435"
            )

        assert result["success"] is True
        assert result["repository_id"] == "401454435"
        assert "Successfully removed" in result["message"]
        # Loaded-module caveat must reach the caller — file removal alone
        # does not unload an integration.
        assert "restart" in result["note"]
        # A numeric id needs no owner/repo resolution round-trip, so with the
        # availability probe patched out by the fixture the WS traffic is
        # exactly the installed-state probe followed by the remove.
        commands = [c.args[0] for c in ws.send_command.await_args_list]
        assert commands == ["hacs/repository/info", "hacs/repository/remove"]
        # HACS's WS API is asymmetric: info takes repository_id, remove takes
        # repository — pin both so neither regresses (e2e caught the mixup).
        assert ws.send_command.await_args_list[0].kwargs["repository_id"] == "401454435"
        assert ws.send_command.await_args.kwargs["repository"] == "401454435"

    async def test_remove_by_owner_repo_resolves_first(self, tools):
        ws = _remove_ws()
        registered = {
            "id": "555",
            "full_name": "hif2k1/battery_sim",
            "name": "Battery Sim",
        }
        with (
            _patched_hacs(ws),
            patch(
                "ha_mcp.tools.tools_hacs.wait_for_repo_registration",
                new_callable=AsyncMock,
            ) as wait_mock,
        ):
            wait_mock.return_value = registered
            result = await tools.ha_manage_hacs(
                action="remove", repository_id="hif2k1/battery_sim"
            )

        assert result["success"] is True
        assert result["repository"] == "Battery Sim"
        assert ws.send_command.await_args.args[0] == "hacs/repository/remove"
        assert ws.send_command.await_args.kwargs["repository"] == "555"

    async def test_remove_rejects_empty_repository_id_before_ws(self, tools):
        ws = _ws({})
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="remove", repository_id="   ")
        assert "repository_id" in str(excinfo.value)
        ws.send_command.assert_not_awaited()

    async def test_remove_store_only_repository_raises_not_found(self, tools):
        # HACS's own remove command reports success for a repository that was
        # never downloaded (its uninstall no-ops without local files) — the
        # tool must reject instead of claiming "Successfully removed".
        ws = _remove_ws(installed=False)
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="remove", repository_id="401454435")

        assert "RESOURCE_NOT_FOUND" in str(excinfo.value)
        assert "not downloaded" in str(excinfo.value)
        # The remove command must never be sent for a store-only repo.
        commands = [c.args[0] for c in ws.send_command.await_args_list]
        assert commands == ["hacs/repository/info"]

    async def test_remove_surfaces_backend_failure(self, tools):
        ws = _remove_ws(remove_response={"success": False, "error": "boom"})
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="remove", repository_id="401454435")
        # HACS's own error text must survive the wrap — the context command
        # name alone would satisfy a bare "remove" check.
        assert "boom" in str(excinfo.value)
        assert "remove" in str(excinfo.value).lower()

    async def test_remove_proceeds_when_installed_probe_fails(self, tools):
        # The WS client raises on a failed info frame; the probe must swallow
        # that and fall through — a rate-limited or transient info lookup
        # must not make a downloaded repository unremovable, nor misattribute
        # its own error to the remove (Patch76 review, PR #2124).
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=[
                HomeAssistantCommandError("Command failed: rate limited", "unknown"),
                {"success": True, "result": {}},
            ]
        )
        with _patched_hacs(ws):
            result = await tools.ha_manage_hacs(
                action="remove", repository_id="401454435"
            )

        assert result["success"] is True
        commands = [c.args[0] for c in ws.send_command.await_args_list]
        assert commands == ["hacs/repository/info", "hacs/repository/remove"]

    async def test_remove_command_error_keeps_command_context(self, tools):
        # The WS client RAISES HomeAssistantCommandError on a failed result
        # frame (it never returns success=False), so the raised path is the
        # one real HACS failures take — the command context must survive it.
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=[
                {"success": True, "result": {"installed": True}},
                HomeAssistantCommandError("Command failed: kaboom", "unknown_error"),
            ]
        )
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="remove", repository_id="401454435")
        assert "kaboom" in str(excinfo.value)
        assert "hacs/repository/remove" in str(excinfo.value)

    async def test_remove_command_timeout_error_reports_real_budget(self, tools):
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=[
                {"success": True, "result": {"installed": True}},
                HomeAssistantCommandError(
                    "Command failed: backend timeout", "unknown_error"
                ),
            ]
        )
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="remove", repository_id="401454435")
        assert "Operation 'hacs/repository/remove' timed out after 60.0s" in str(
            excinfo.value
        )

    async def test_remove_timeout_says_outcome_is_unknown(self, tools):
        # HACS force-refreshes from GitHub before uninstalling; when that
        # blows the WS wait the uninstall usually still completes, so a
        # plain "failed" would invite a destructive retry. The error must
        # say the outcome is unverified and point at the info probe.
        from ha_mcp.client.rest_client import HomeAssistantCommandTimeout

        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=[
                {"success": True, "result": {"installed": True}},
                HomeAssistantCommandTimeout("Command timeout"),
            ]
        )
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="remove", repository_id="401454435")
        assert "may still have completed" in str(excinfo.value)

    async def test_remove_numeric_id_reports_real_name_from_info(self, tools):
        # The resolve short-circuit echoes a numeric id back as the "name";
        # the installed-state probe carries the real identity, and the
        # response must use it so the caller can verify WHICH repository
        # the id meant.
        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=[
                {
                    "success": True,
                    "result": {"installed": True, "full_name": "hif2k1/battery_sim"},
                },
                {"success": True, "result": {}},
            ]
        )
        with _patched_hacs(ws):
            result = await tools.ha_manage_hacs(
                action="remove", repository_id="401454435"
            )
        assert result["repository"] == "hif2k1/battery_sim"
        assert "hif2k1/battery_sim" in result["message"]

    async def test_foreign_params_are_rejected_not_ignored(self, tools):
        # A parameter belonging to another action must fail loudly —
        # ha_manage_hacs(action="remove", version=...) plausibly means
        # "uninstall this version", which remove cannot honor; silently
        # dropping it would remove the whole repository.
        ws = _ws({})
        cases = [
            {"action": "remove", "repository_id": "1", "version": "v4.0.0"},
            {"action": "remove", "repository_id": "1", "repository": "o/r"},
            {"action": "download", "repository_id": "1", "category": "theme"},
            {
                "action": "add_repository",
                "repository": "o/r",
                "category": "theme",
                "repository_id": "1",
            },
        ]
        for kwargs in cases:
            with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
                await tools.ha_manage_hacs(**kwargs)
            assert "VALIDATION_INVALID_PARAMETER" in str(excinfo.value), kwargs
            assert "do not apply" in str(excinfo.value), kwargs
        ws.send_command.assert_not_awaited()

    async def test_remove_returns_top_level_success_envelope(self, tools):
        # AGENTS.md response contract: {"success": True, "data": ...} at the
        # TOP level. add_timezone_metadata wraps its payload under "data", so
        # the handler must hoist success above the wrapper — with the real
        # wrapper shape (not the identity stand-in) the envelope holds.
        async def _wrapping_timezone(_client, data):
            return {"data": data, "metadata": {"home_assistant_timezone": "UTC"}}

        ws = _remove_ws()
        with (
            _patched_hacs(ws),
            patch(
                "ha_mcp.tools.tools_hacs.add_timezone_metadata",
                new=_wrapping_timezone,
            ),
        ):
            result = await tools.ha_manage_hacs(
                action="remove", repository_id="401454435"
            )

        assert result["success"] is True
        assert result["data"]["repository_id"] == "401454435"
        assert "metadata" in result


class TestManageHacsUpdateInformation:
    async def test_update_information_numeric_id_sends_refresh(self, tools):
        ws = _ws({})
        with _patched_hacs(ws):
            result = await tools.ha_manage_hacs(
                action="update_information", repository_id="441028036"
            )

        assert result["success"] is True
        # A numeric id needs no resolution round-trip — exactly one WS call.
        ws.send_command.assert_awaited_once()
        assert ws.send_command.await_args.args[0] == "hacs/repository/refresh"
        # HACS's WS API is asymmetric: refresh takes repository (like remove),
        # not repository_id (like info). The 60 s budget covers HACS's forced
        # GitHub re-fetch, which the 30 s default would report as a false
        # failure.
        assert ws.send_command.await_args.kwargs["repository"] == "441028036"
        assert ws.send_command.await_args.kwargs["_wait_timeout"] == 60.0

    async def test_update_information_path_resolves_then_refreshes(self, tools):
        ws = _ws({})
        registered = {"id": 123, "name": "lovelace-mushroom"}
        with (
            _patched_hacs(ws),
            patch(
                "ha_mcp.tools.tools_hacs.wait_for_repo_registration",
                new_callable=AsyncMock,
            ) as wait_mock,
        ):
            wait_mock.return_value = registered
            result = await tools.ha_manage_hacs(
                action="update_information",
                repository_id="piitaya/lovelace-mushroom",
            )

        assert result["success"] is True
        assert result["repository"] == "lovelace-mushroom"  # resolved display name
        assert ws.send_command.await_args.args[0] == "hacs/repository/refresh"
        # The resolved numeric id, not the owner/repo path, reaches HACS.
        assert ws.send_command.await_args.kwargs["repository"] == "123"

    async def test_update_information_empty_id_raises(self, tools):
        ws = _ws({})
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="update_information", repository_id="")
        assert "repository_id" in str(excinfo.value)
        ws.send_command.assert_not_awaited()

    async def test_update_information_missing_id_raises(self, tools):
        ws = _ws({})
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="update_information")
        assert "repository_id" in str(excinfo.value)
        ws.send_command.assert_not_awaited()

    async def test_update_information_rejects_foreign_params(self, tools):
        # update_information takes only repository_id; a download-only
        # parameter must fail loudly rather than be silently dropped.
        ws = _ws({})
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="update_information",
                repository_id="1",
                version="v1.0.0",
            )
        assert "VALIDATION_INVALID_PARAMETER" in str(excinfo.value)
        assert "version" in str(excinfo.value)
        assert "do not apply" in str(excinfo.value)
        ws.send_command.assert_not_awaited()

    async def test_update_information_timeout_reports_the_real_budget(self, tools):
        # The generic timeout classifier defaults to a 30 s message; the
        # refresh handler must attach its actual 60 s budget instead.
        from ha_mcp.client.rest_client import HomeAssistantCommandTimeout

        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=HomeAssistantCommandTimeout("Command timeout")
        )
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="update_information", repository_id="441028036"
            )
        assert "60" in str(excinfo.value)
        assert "hacs/repository/refresh" in str(excinfo.value)
        # The rendered message must name the command that timed out. Without
        # the command fallback in the classifier it reads "Operation
        # 'operation' timed out", which no other assertion here would catch.
        assert "Operation 'hacs/repository/refresh'" in str(excinfo.value)

    async def test_update_information_command_error_keeps_command_context(self, tools):
        # The raised path is the one real HACS failures take; the dict branch
        # below it only serves stubbed clients.
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=HomeAssistantCommandError(
                "Command failed: kaboom", "unknown_error"
            )
        )
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="update_information", repository_id="441028036"
            )
        assert "kaboom" in str(excinfo.value)
        assert "hacs/repository/refresh" in str(excinfo.value)

    async def test_update_information_failed_response_raises(self, tools):
        ws = AsyncMock()
        ws.send_command = AsyncMock(
            return_value={"success": False, "error": {"message": "boom"}}
        )
        with _patched_hacs(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="update_information", repository_id="441028036"
            )
        # HACS's own error text must survive the wrap, alongside the command
        # context that names which call failed.
        assert "boom" in str(excinfo.value)
        assert "hacs/repository/refresh" in str(excinfo.value)


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
