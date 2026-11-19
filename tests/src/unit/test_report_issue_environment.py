"""What ha_report_issue states about the install it runs on.

Each report comes from ``ha_report_issue`` on the server's own client against a
real Core; the environment block is what triage reads first, so every line
must come from the install itself and a failed probe must stay visible.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.ha_mcp_tools.const import COMPONENT_VERSION, DOMAIN
from ha_mcp import __version__
from ha_mcp._vendor import websockets as vendored_websockets
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.config import get_global_settings
from ha_mcp.tools import server_version, tools_bug_report
from ha_mcp.tools.bug_report_config import collect_config_toggles
from ha_mcp.tools.component_api import INFO_COMMAND, get_component_caps
from ha_mcp.tools.tools_filesystem import CALLER_TOKEN_BOOTSTRAP_SERVICE
from ha_mcp.utils.data_paths import get_data_dir

from ._client_spies import sent_commands
from ._core_setup import set_up_tools_entry
from ._report_issue import report_issue, report_line
from ._server_entry import add_server_entry
from ._supervisor_core import load_supervisor_api, supervisor_requests


async def test_a_healthy_install_is_described_from_core_and_the_process(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Container install without a Supervisor: the lines come from Core's
    config and states and from the running server, and no Supervisor is
    asked (that would log two ERROR lines per report)."""
    monkeypatch.setattr(server_version, "get_version", lambda: __version__)
    await hass.config.async_update(
        location_name="Test Home", time_zone="America/New_York"
    )
    for number in range(3):
        hass.states.async_set(f"light.light_{number}", "on")

    report = await report_issue(core_client)

    assert report["diagnostic_info"]["home_assistant_version"] == HA_VERSION
    assert report_line(report, "Home Assistant Version") == HA_VERSION
    assert report_line(report, "Connection Status") == "Connected"
    assert report_line(report, "Entity Count") == str(len(hass.states.async_all()))
    assert report_line(report, "Location Name") == "Test Home"
    assert report_line(report, "Time Zone") == "America/New_York"
    assert report_line(report, "ha-mcp Version") == __version__
    assert report_line(report, "Supervisor").startswith("none")
    assert not [
        call
        for call in sent.await_args_list
        if call.args[0]["type"] == "supervisor/api"
    ]
    # The vendor sync strips the compiled extension, so the shipped copy is
    # pure Python; a "throughput dropped" report needs to see that.
    assert report_line(report, "websockets Dependency").startswith(
        f"vendored {vendored_websockets.__version__} (pure-Python) | shared env copy: "
    )
    assert report_line(report, "Tool Policy").startswith("0 rules")
    assert "=== ha-mcp Config Toggles ===" in report["formatted_report"]
    instance = report["diagnostic_info"]["instance"]
    assert instance["instance_id"]
    assert instance["uptime_seconds"] >= 0


async def test_unreachable_home_assistant_keeps_every_failed_probe_visible(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report is needed most when the install is broken: it must still be
    built, and each failed probe must read as failed, not as absent."""

    def unreadable_metadata() -> str:
        raise RuntimeError("metadata read failed")

    monkeypatch.setattr(server_version, "get_version", unreadable_metadata)
    (get_data_dir() / "tool_policy.json").write_text("{not json", encoding="utf-8")

    with (
        patch.object(
            core_client,
            "get_config",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
        ),
        patch.object(
            core_client,
            "get_states",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
        ),
    ):
        report = await report_issue(core_client)

    assert report["success"] is True
    assert report_line(report, "Connection Status").startswith("Connection Error")
    assert report_line(report, "Home Assistant Version") == "Unknown"
    assert report_line(report, "Entity Count") == "0"
    assert report_line(report, "Custom Component") == (
        "not detected (not installed, or probe failed)"
    )
    assert "could not be verified" in report_line(report, "ha-mcp Version")
    assert report_line(report, "Tool Policy") == "unreadable (invalid JSON)"


async def test_unanswered_state_read_keeps_the_rest_of_the_install(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    hass.states.async_set("light.kitchen", "on")

    with patch.object(
        core_client,
        "get_states",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
    ):
        report = await report_issue(core_client)

    assert report_line(report, "Connection Status") == "Connected"
    assert report_line(report, "Home Assistant Version") == HA_VERSION
    assert report_line(report, "Entity Count") == "0"


async def test_report_works_when_the_policy_package_cannot_load(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server keeps running when the policy package fails to import, and
    that is exactly when a report is needed."""
    monkeypatch.setitem(sys.modules, "ha_mcp.policy.persistence", None)

    report = await report_issue(core_client)

    assert report_line(report, "Tool Policy") == "unreadable (ModuleNotFoundError)"


def test_toggles_carry_the_beta_flags_and_count_the_tool_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A "missing tool" report (#1804) needs the beta master and the gated
    tool families, a flag that is off included; the tool lists are counted,
    not copied."""
    settings = get_global_settings()
    for name, value in {
        "enable_beta_features": True,
        "enable_filesystem_tools": False,
        "disabled_tools": "ha_foo,ha_bar",
        "pinned_tools": "",
    }.items():
        monkeypatch.setattr(settings, name, value)

    toggles = collect_config_toggles(settings)

    assert toggles["enable_beta_features"] is True
    assert toggles["enable_filesystem_tools"] is False
    assert toggles["disabled_tools_count"] == 2
    assert toggles["pinned_tools_count"] == 0


async def _tools_absent_server_added(hass: HomeAssistant) -> None:
    add_server_entry(hass).mock_state(hass, ConfigEntryState.LOADED)


async def _tools_set_up_server_not_added(hass: HomeAssistant) -> None:
    await set_up_tools_entry(hass)
    # The pre-#1853 tools-entry title an unloaded legacy entry may carry.
    MockConfigEntry(domain=DOMAIN, title="HA MCP Tools").add_to_hass(hass)


async def _old_tools_renamed_entry(hass: HomeAssistant) -> None:
    await set_up_tools_entry(hass)
    # A component before 0.5.0 registers its services but not the bootstrap.
    hass.services.async_remove(DOMAIN, CALLER_TOKEN_BOOTSTRAP_SERVICE)
    renamed = MockConfigEntry(domain=DOMAIN, title="My Renamed Entry")
    renamed.add_to_hass(hass)
    renamed.mock_state(hass, ConfigEntryState.LOADED)


@pytest.mark.parametrize(
    ("set_up", "tools_line", "server_line"),
    [
        pytest.param(
            _tools_absent_server_added,
            "not set up — no ha_mcp_tools services registered",
            "added (loaded)",
            id="tools-entry-never-added",
        ),
        pytest.param(
            _tools_set_up_server_not_added,
            "set up (ha_mcp_tools services registered)",
            "not added",
            id="server-entry-not-added",
        ),
        # A renamed entry cannot be classified by title: it is listed, not
        # wrongly reported as "not added".
        pytest.param(
            _old_tools_renamed_entry,
            "set up, but the component is pre-0.5.0",
            'not identified — unrecognized ha_mcp_tools entries: "My Renamed Entry" (loaded)',
            id="old-component-renamed-entry",
        ),
    ],
)
async def test_both_component_entries_are_named_next_to_each_other(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    set_up: Callable[[HomeAssistant], Awaitable[None]],
    tools_line: str,
    server_line: str,
) -> None:
    """Integration installed but the File & YAML entry never added (#1996),
    or an add-on install that also added the server entry, must each be
    readable from the report next to Installation Method."""
    assert await async_setup_component(hass, "config", {})
    await set_up(hass)

    report = await report_issue(core_client)

    assert report_line(report, "File & YAML Tools entry").startswith(tools_line)
    assert report_line(report, "In-process Server entry") == server_line
    assert f"- **In-process Server entry:** {server_line}\n" in report["issue_body"]


async def test_failed_entry_probes_read_unknown(
    core_client: HomeAssistantClient,
) -> None:
    """Without ``config`` Core rejects ``config_entries/get``; the services
    read gets no answer. Neither may break the report."""
    with patch.object(
        core_client,
        "get_services",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
    ):
        report = await report_issue(core_client)

    assert report["success"] is True
    assert report_line(report, "File & YAML Tools entry") == "unknown (probe failed)"
    assert report_line(report, "In-process Server entry") == "unknown (probe failed)"


async def test_component_version_and_entry_line_share_one_info_read(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A second ``info`` read could answer for a component updated in
    between, and the version and webhook mode would then disagree."""
    assert await async_setup_component(hass, "config", {})
    add_server_entry(hass).mock_state(hass, ConfigEntryState.LOADED)
    await get_component_caps(component_core_client)
    socket = await get_websocket_client(
        url=component_core_client.base_url,
        token=component_core_client.token,
        verify_ssl=False,
    )

    with sent_commands(socket) as send:
        report = await report_issue(component_core_client)

    sent = [call.args[0] for call in send.call_args_list if call.args]
    assert sent.count(INFO_COMMAND) == 1
    assert report_line(report, "Custom Component") == COMPONENT_VERSION
    assert report_line(report, "In-process Server entry") == "added (loaded)"


@pytest.fixture
def supervised(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> AiohttpClientMocker:
    """An embedded server on HAOS: Core loaded ``hassio``, and the Core
    container carries ``SUPERVISOR_TOKEN``."""
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    load_supervisor_api(hass, aioclient_mock, monkeypatch, {})
    hass.config.components.add("hassio")
    return aioclient_mock


async def test_supervised_install_reports_the_supervisor_release(
    core_client: HomeAssistantClient, supervised: AiohttpClientMocker
) -> None:
    """App tool failures depend on the Supervisor release (#2270, #2278).
    The embedded server must not read as the add-on or as plain Docker: the
    Core container carries both ``SUPERVISOR_TOKEN`` and ``/.dockerenv``."""
    supervised.get(
        "http://127.0.0.1/info",
        json={
            "result": "ok",
            "data": {
                "supervisor": "2026.09.2",
                "operating_system": "Home Assistant OS 18.3",
            },
        },
    )

    report = await report_issue(core_client)

    assert ("get", "/info") in supervisor_requests(supervised)
    assert (
        "- **Supervisor:** 2026.09.2 (host OS: Home Assistant OS 18.3)\n"
        in (report["issue_body"])
    )
    assert report_line(report, "Installation Method") == "embedded"


async def test_slow_supervisor_does_not_hold_up_the_report(
    core_client: HomeAssistantClient,
    supervised: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2279 was a report that hung for 15 minutes; a probe that gets no
    answer says why instead."""
    answer = asyncio.Event()

    async def answers_late(method: str, url: URL, data: Any) -> Any:
        await answer.wait()
        return AiohttpClientMockResponse(method, url, json={"result": "ok", "data": {}})

    supervised.get("http://127.0.0.1/info", side_effect=answers_late)
    monkeypatch.setattr(tools_bug_report, "SUPERVISOR_PROBE_TIMEOUT", 0.05)
    try:
        report = await report_issue(core_client)
    finally:
        answer.set()

    assert report_line(report, "Supervisor") == "probe failed (TimeoutError)"


@pytest.mark.parametrize(
    ("isatty", "transport"),
    [
        # Claude Desktop launching the server over a pipe.
        pytest.param(lambda: False, "stdio", id="stdio"),
        pytest.param(lambda: True, "http", id="http"),
    ],
)
async def test_transport_reaches_the_report_and_decides_the_process_tree_walk(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    isatty: Callable[[], bool],
    transport: str,
) -> None:
    """Only a stdio server has its host app as the parent process: walking
    the tree over HTTP would name an unrelated process."""
    for name in ("FASTMCP_TRANSPORT", "MCP_HTTP_PORT", "FASTMCP_PORT"):
        monkeypatch.delenv(name, raising=False)
    if transport == "http":
        monkeypatch.setenv("FASTMCP_TRANSPORT", "http")
    monkeypatch.setattr(sys, "argv", ["/usr/bin/ha-mcp"])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=isatty))
    walks: list[None] = []

    def walk() -> dict[str, str]:
        walks.append(None)
        return {}

    monkeypatch.setattr(tools_bug_report, "detect_client_host", walk)

    report = await report_issue(core_client)

    assert report_line(report, "MCP Transport") == transport
    assert bool(walks) is (transport == "stdio")
