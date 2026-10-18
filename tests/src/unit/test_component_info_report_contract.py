"""Cross-seam contract test for the ``info`` fields ``ha_report_issue`` reads.

Pipes the REAL component ``_do_info(hass)`` through the REAL server-side
``get_component_caps`` negotiation and the fresh ``info`` read into the
report's component version and server-entry line (#2584), so a name or shape
drift between the component's fields and the report's readers fails here.
Bridge pattern as in ``test_component_info_timezone_contract.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools import component_api
from ha_mcp.tools.tools_bug_report import BugReportTools

# Autouse: importing the fixture applies it to this module's tests too.
from .test_component_info_timezone_contract import _clear_caps_cache  # noqa: F401
from .test_component_ws_search import FakeConfigEntry, FakeHass, wsapi

DOMAIN = "ha_mcp_tools"
SERVER_ROW = {"entry_id": "a1", "title": "HA-MCP Server", "state": "loaded"}
UNKNOWN = HomeAssistantCommandError(
    "Command failed: Unknown command.", "unknown_command"
)


def _report(
    hass: FakeHass, edit_info: Callable[[dict[str, Any]], Any] | None = None
) -> BugReportTools:
    """``ha_report_issue``'s probes against a component serving ``hass``.

    ``edit_info`` changes the real ``info`` reply, standing in for another
    component version. ``info`` is served on the component WebSocket, which
    both the caps probe and the fresh read use; ``send_websocket_message``
    answers only ``config_entries/get``.
    """

    async def _info(command_type: str, **kwargs: Any) -> dict[str, Any]:
        assert command_type == "ha_mcp_tools/info"
        result = wsapi._do_info(hass)
        if edit_info is not None:
            edit_info(result)
        return {"success": True, "result": result}

    async def _send(message: dict[str, Any]) -> dict[str, Any]:
        assert message["type"] == "config_entries/get"
        return {"success": True, "result": [SERVER_ROW]}

    ws = AsyncMock()
    ws.send_command = AsyncMock(side_effect=_info)
    client = MagicMock(base_url="http://ha.local:8123", token="tok")
    client.send_websocket_message = AsyncMock(side_effect=_send)
    client.call_service = AsyncMock(side_effect=RuntimeError("no such service"))
    client.ws = ws
    return BugReportTools(client)


def _then(report: BugReportTools, *replies: Any) -> None:
    """Answer the first ``info`` call for real, then with ``replies`` in turn
    (an exception is raised, anything else returned)."""
    serve = report._client.ws.send_command.side_effect
    queue = list(replies)

    async def _send_command(command_type: str, **kwargs: Any) -> Any:
        if report._client.ws.send_command.await_count == 1 or not queue:
            return await serve(command_type, **kwargs)
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    report._client.ws.send_command.side_effect = _send_command


def _served(report: BugReportTools) -> Any:
    """Serve the caps probe and the fresh reads from the same component."""
    return patch.object(
        component_api, "get_websocket_client", AsyncMock(return_value=report._client.ws)
    )


async def _line(report: BugReportTools) -> str | None:
    with _served(report):
        info = await component_api.read_info(report._client)
        return await report._detect_server_entry_status(info)


async def _version(report: BugReportTools) -> str | None:
    with _served(report):
        info = await component_api.read_info(report._client)
        return await report._detect_component_version(info)


def _server(options: dict[str, Any] | None = None) -> FakeConfigEntry:
    return FakeConfigEntry(DOMAIN, data={"entry_type": "server"}, options=options)


# What bring-up stores once it has registered the webhook.
REGISTERED = {DOMAIN: {"webhook": {"auth_mode": "none"}}}


def _versioned(running: dict[str, str]) -> BugReportTools:
    return _report(
        FakeHass(config_entries=[_server()]),
        lambda r: r.update(component_version=running["version"]),
    )


@pytest.mark.asyncio
async def test_updated_component_version_reaches_the_next_report() -> None:
    """A component update keeps a long-lived server's client, and with it the
    caps snapshot; a later report must show the version now running."""
    running = {"version": "2.1.0"}
    report = _versioned(running)
    assert await _version(report) == "2.1.0"

    running["version"] = "2.2.0"

    assert await _version(report) == "2.2.0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [RuntimeError("socket closed"), {"success": True, "result": []}, "garbage"],
    ids=["raises", "result-not-a-dict", "reply-not-a-dict"],
)
async def test_failed_fresh_read_reports_the_snapshot_version(failure: Any) -> None:
    """The snapshot still shows the component is installed; a failed fresh
    read must not turn that into "not detected"."""
    report = _versioned({"version": "2.1.0"})
    _then(report, failure)

    assert await _version(report) == "2.1.0"
    report._client.call_service.assert_not_called()


@pytest.mark.asyncio
async def test_downgraded_component_reports_its_running_version() -> None:
    """A component downgraded below ``info`` answers ``unknown_command``: the
    stale snapshot is dropped, so the version comes from the bootstrap service
    rather than from the component that was running before."""
    report = _versioned({"version": "2.1.0"})
    _then(report, UNKNOWN, UNKNOWN)
    report._client.call_service.side_effect = None
    report._client.call_service.return_value = {
        "service_response": {"version": "1.0.5"}
    }

    assert await _version(report) == "1.0.5"


@pytest.mark.asyncio
async def test_component_without_a_version_field_reads_as_not_detected() -> None:
    report = _report(
        FakeHass(config_entries=[_server()]), lambda r: r.pop("component_version")
    )

    assert await _version(report) is None
    report._client.call_service.assert_not_called()


@pytest.mark.asyncio
async def test_configured_mode_reaches_the_report() -> None:
    tools = FakeConfigEntry(DOMAIN, data={"entry_type": "tools"})
    hass = FakeHass(
        config_entries=[tools, _server({"webhook_auth": "ha_auth"})], data=REGISTERED
    )

    assert await _line(_report(hass)) == "added (loaded), webhook auth: ha_auth"


@pytest.mark.asyncio
async def test_changed_mode_reaches_the_next_report() -> None:
    """The mode is an entry option the user can change while a long-lived
    server keeps its client; a later report must not repeat the first answer."""
    server = _server({"webhook_auth": "ha_auth"})
    report = _report(FakeHass(config_entries=[server], data=REGISTERED))
    await _line(report)

    server.options = {"webhook_auth": "legacy"}

    assert await _line(report) == "added (loaded), webhook auth: legacy"


@pytest.mark.asyncio
async def test_unset_option_reads_as_the_bring_up_default() -> None:
    hass = FakeHass(config_entries=[_server()], data=REGISTERED)

    assert await _line(_report(hass)) == "added (loaded), webhook auth: none"


@pytest.mark.asyncio
async def test_disabled_webhook_is_not_reported_as_open() -> None:
    """Local-only mode keeps the configured auth option, but no webhook exists;
    ``none`` would read as an unauthenticated public webhook."""
    options = {"webhook_auth": "none", "enable_webhook": False}
    hass = FakeHass(config_entries=[_server(options)], data=REGISTERED)

    assert await _line(_report(hass)) == "added (loaded), webhook auth: disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edit_info",
    [
        pytest.param(lambda r: r.pop("webhook_auth"), id="field-missing"),
        pytest.param(lambda r: r.update(webhook_auth=123), id="not-a-string"),
    ],
)
async def test_component_without_a_usable_mode_leaves_the_line_unchanged(
    edit_info: Callable[[dict[str, Any]], Any],
) -> None:
    """A component older than the field omits it; the report keeps the plain
    entry status rather than inventing a mode."""
    hass = FakeHass(
        config_entries=[_server({"webhook_auth": "ha_auth"})], data=REGISTERED
    )

    assert await _line(_report(hass, edit_info)) == "added (loaded)"


@pytest.mark.asyncio
async def test_component_without_info_is_not_asked_for_the_mode() -> None:
    """A component the caps probe found no ``info`` on is not sent the
    command a second time."""
    report = _report(FakeHass(config_entries=[_server()], data=REGISTERED))
    report._client.ws.send_command.side_effect = UNKNOWN

    assert await _line(report) == "added (loaded)"
    assert report._client.ws.send_command.await_count == 1


@pytest.mark.asyncio
async def test_failed_info_call_leaves_the_line_unchanged() -> None:
    """The entry status came from ``config_entries/get``; a failing ``info``
    call must not take it out of the report."""
    report = _report(FakeHass(config_entries=[_server()], data=REGISTERED))
    _then(report, RuntimeError("boom"))

    assert await _line(report) == "added (loaded)"


@pytest.mark.parametrize(
    "data",
    [{}, {DOMAIN: {"manager": SimpleNamespace(is_running=True)}}],
    ids=["nothing-registered", "server-up-webhook-not-yet"],
)
def test_no_webhook_registration_reports_no_mode(data: dict[str, Any]) -> None:
    """Bring-up registers the webhook after the server has started, and a
    failed bring-up leaves the entry loaded; the configured ``none`` would then
    read as an open webhook."""
    hass = FakeHass(config_entries=[_server({"webhook_auth": "none"})], data=data)

    assert wsapi._do_info(hass)["webhook_auth"] is None


def test_no_server_entry_reports_no_mode() -> None:
    """Only the tools entry is set up (an external server): there is no
    in-process webhook, so the field is None."""
    tools = FakeConfigEntry(DOMAIN, data={"entry_type": "tools"})

    assert wsapi._do_info(FakeHass(config_entries=[tools]))["webhook_auth"] is None
