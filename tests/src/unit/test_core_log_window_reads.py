"""Core log windows through the component: bursts, odd lines and filters."""

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiohasupervisor.models
import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient

from ._error_log_core import call_logs, serve_log
from ._error_log_core import log_file as log_file
from ._log_tool_calls import set_up_core_logs
from ._tool_errors import error_body
from ._tools_addons_core import Supervisor
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

_DAY = "2026-11-12"


def _line(second: int, logger: str, message: str) -> str:
    return f"{_DAY} 10:00:{second:02d}.000+00:00 INFO (MainThread) [{logger}] {message}"


async def test_a_record_longer_than_one_read_is_returned_and_the_timeline_moves_on(
    log_file: Path, component_core_client: HomeAssistantClient
) -> None:
    """Core reads 2,000 lines of a source at a time."""
    frames = "".join(f"frame {index}\n" for index in range(2100))
    await serve_log(
        log_file, f"{_line(1, 'a', 'burst')}\n{frames}{_line(5, 'a', 'after')}\n"
    )
    args = {
        "sources": ["core"],
        "start_time": f"{_DAY}T10:00:00+00:00",
        "end_time": f"{_DAY}T10:00:09+00:00",
        "limit": 5,
    }
    first = await call_logs(component_core_client, **args)
    assert [row["message"].split("\n")[0] for row in first["records"]] == [
        "INFO (MainThread) [a] burst"
    ]
    assert any("core" in warning for warning in first["warnings"])
    last = await call_logs(
        component_core_client, **(args | {"start_time": first["next_start_time"]})
    )
    assert [row["message"] for row in last["records"]] == [
        "INFO (MainThread) [a] after"
    ]
    assert not last["has_more"]


async def test_a_timeline_without_supervisor_keeps_core_and_names_the_journal(
    log_file: Path, component_core_client: HomeAssistantClient
) -> None:
    await serve_log(log_file, f"{_line(1, 'a', 'kept')}\n")
    page = await call_logs(
        component_core_client,
        sources=["core", "supervisor"],
        start_time=f"{_DAY}T10:00:00+00:00",
        end_time=f"{_DAY}T10:00:09+00:00",
    )
    assert [row["source"] for row in page["records"]] == ["core"]
    assert list(page["errors"]) == ["supervisor"]
    assert not page.get("warnings"), "no update is missing: there is no Supervisor"


@pytest.mark.skipif(
    hasattr(aiohasupervisor.models, "LogsOptions"),
    reason="Core 2026.12 reads the journal itself; this is the 2026.11 fallback",
)
async def test_a_supervisor_install_with_a_log_file_merges_it_with_the_journal(
    log_file: Path,
    component_core_client: HomeAssistantClient,
    core_supervisor: Supervisor,
) -> None:
    """Core writes a log file under Supervisor only when asked to."""
    await serve_log(log_file, f"{_line(1, 'a', 'from the file')}\n")
    core_supervisor.reply(
        "GET", "/host/logs", text=f"{_DAY} 10:00:02.000+00:00 host kernel: journal\n"
    )
    page = await call_logs(
        component_core_client,
        sources=["core", "host"],
        start_time=f"{_DAY}T10:00:00+00:00",
        end_time=f"{_DAY}T10:00:09+00:00",
    )
    assert [row["source"] for row in page["records"]] == ["core", "host"]


@pytest.mark.skipif(
    not hasattr(aiohasupervisor.models, "LogsOptions"),
    reason="Core 2026.11's Supervisor client does not implement native journal reads",
)
async def test_a_timeline_keeps_the_journal_when_cores_log_file_cannot_be_read(
    hass: HomeAssistant,
    log_file: Path,
    component_core_client: HomeAssistantClient,
    core_supervisor: Supervisor,
) -> None:
    await serve_log(log_file, f"{_line(1, 'a', 'from the file')}\n")
    await hass.async_add_executor_job(log_file.unlink)
    core_supervisor.reply(
        "GET", "/host/logs", text=f"{_DAY} 10:00:02.000+00:00 host kernel: journal\n"
    )
    page = await call_logs(
        component_core_client,
        sources=["core", "host"],
        start_time=f"{_DAY}T10:00:00+00:00",
        end_time=f"{_DAY}T10:00:09+00:00",
    )
    assert [row["source"] for row in page["records"]] == ["host"]
    assert list(page["errors"]) == ["core"]


async def test_a_timeline_whose_sources_all_lack_a_journal_is_not_found(
    component_core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as refused:
        await call_logs(
            component_core_client, sources=["supervisor", "host"], **_WINDOW
        )
    assert error_body(refused)["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_lines_holding_a_carriage_return_page_once_each(
    log_file: Path, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await set_up_core_logs(hass)
    lines = [_line(index % 60, "a", f"got OK\rtail {index}") for index in range(450)]
    await serve_log(log_file, "".join(f"{line}\n" for line in lines))
    seen: list[str] = []
    arguments: dict[str, Any] = {"source": "error_log", "limit": 100}
    for _ in range(12):
        page = await call_logs(component_core_client, **arguments)
        assert page["returned_lines"] == page["returned_records"]
        seen += page["log"].split("\n")
        if not page["has_more"]:
            break
        arguments["offset"] = page["next_offset"]
    assert seen == lines[::-1]


async def test_an_integration_page_names_the_integration_in_its_next_call(
    log_file: Path, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await set_up_core_logs(hass)
    wanted = "homeassistant.components.logger"
    lines = [
        _line(index % 60, wanted if index % 2 else "a.b", f"issue {index}")
        for index in range(600)
    ]
    await serve_log(log_file, "".join(f"{line}\n" for line in lines))
    arguments = {"source": "error_log", "integration": "logger", "limit": 2}
    first = await call_logs(component_core_client, **arguments)
    assert first["has_more"]
    assert "integration='logger'" in first["pagination_hint"]
    second = await call_logs(
        component_core_client, **(arguments | {"offset": first["next_offset"]})
    )
    read = first["log"].split("\n") + second["log"].split("\n")
    assert [line.rsplit(" ", 1)[1] for line in read] == ["599", "597", "595", "593"]


async def test_an_integration_with_no_lines_is_not_called_an_empty_fetch(
    log_file: Path, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await set_up_core_logs(hass)
    await serve_log(log_file, f"{_line(1, 'a.b', 'other')}\n")
    result = await call_logs(
        component_core_client, source="error_log", integration="logger"
    )
    assert result["returned_lines"] == 0
    (warning,) = result["warnings"]
    assert "'logger'" in warning


async def test_an_exact_window_takes_a_stringified_offset_and_names_unused_filters(
    log_file: Path, component_core_client: HomeAssistantClient
) -> None:
    await serve_log(
        log_file, "".join(f"{_line(i, 'a', f'issue {i}')}\n" for i in range(5))
    )
    page = await call_logs(
        component_core_client,
        source="error_log",
        start_time=f"{_DAY}T10:00:00+00:00",
        end_time=f"{_DAY}T10:00:09+00:00",
        limit=1,
        offset="2",
        entity_id="sun.sun",
    )
    assert page["log"].endswith("issue 2")
    assert page["next_offset"] == 3
    assert any("entity_id" in warning for warning in page["warnings"])


_WINDOW = {
    "start_time": f"{_DAY}T10:00:00+00:00",
    "end_time": f"{_DAY}T10:00:09+00:00",
}


@pytest.mark.parametrize(
    "arguments",
    [
        {"source": "error_log", **_WINDOW, "hours_back": 1},
        {"source": "error_log", **_WINDOW, "level": "ERROR"},
        {"sources": ["core"], **_WINDOW, "offset": 2},
        {"sources": ["core"], "start_time": _WINDOW["start_time"]},
        {"source": "error_log", "latest": True, "hours_back": 1},
        {"source": "error_log", "latest": True, "boot": -1},
        {"source": "supervisor", "slug": "core_ssh", "identifier": "kernel"},
        {"source": "error_log", "level": "NOTSET"},
        {"sources": [], **_WINDOW},
        {"sources": ["not a name"], **_WINDOW},
        {"sources": [f"app:{index}" for index in range(13)], **_WINDOW},
    ],
)
async def test_parameters_that_do_not_combine_are_refused(
    component_core_client: HomeAssistantClient, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ToolError) as refused:
        await call_logs(component_core_client, **arguments)
    assert error_body(refused)["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


@pytest.mark.parametrize(
    "window",
    [
        {"start_time": _WINDOW["end_time"], "end_time": _WINDOW["start_time"]},
        {"start_time": f"{_DAY}T10:00:00", "end_time": f"{_DAY}T10:00:09"},
    ],
)
async def test_a_window_that_is_reversed_or_has_no_offset_is_refused(
    component_core_client: HomeAssistantClient, window: dict[str, str]
) -> None:
    with pytest.raises(ToolError) as refused:
        await call_logs(component_core_client, source="error_log", **window)
    assert error_body(refused)["error"]["code"] == "VALIDATION_FAILED"


async def test_system_entries_name_an_unloaded_integration_and_survive_a_missing_one(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await set_up_core_logs(hass)
    logging.getLogger("homeassistant.components.zha.core").error("setup failed")
    logging.getLogger("custom_components.not_installed.api").error("stray")
    result = await call_logs(component_core_client, source="system")
    owners = {entry["name"]: entry["integrations"] for entry in result["entries"]}
    assert owners["homeassistant.components.zha.core"] == ["zha"]
    assert owners["custom_components.not_installed.api"] == []


@pytest.mark.parametrize("source", ["error_log", "system"])
async def test_an_unknown_integration_is_not_found(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, source: str
) -> None:
    await set_up_core_logs(hass)
    with pytest.raises(ToolError) as refused:
        await call_logs(
            component_core_client, source=source, integration="no_such_integration"
        )
    assert error_body(refused)["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_a_cursor_from_a_replaced_log_file_is_refused(
    log_file: Path, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Core starts a new log file at each start."""
    await set_up_core_logs(hass)
    lines = "".join(f"{_line(i % 60, 'a', f'issue {i}')}\n" for i in range(450))
    await serve_log(log_file, lines)
    first = await call_logs(component_core_client, source="error_log", limit=1)
    replacement = log_file.with_name("replacement.log")
    await serve_log(replacement, lines)
    await asyncio.to_thread(replacement.replace, log_file)
    with pytest.raises(ToolError) as refused:
        await call_logs(
            component_core_client,
            source="error_log",
            limit=1,
            offset=first["next_offset"],
        )
    assert error_body(refused)["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_a_window_past_cores_backlog_reads_the_whole_file_without_the_component(
    log_file: Path, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_core_logs(hass)
    lines = [_line(index % 60, "a", f"issue {index}") for index in range(300)]
    await serve_log(log_file, "".join(f"{line}\n" for line in lines))
    page = await call_logs(core_client, source="error_log", limit=150)
    assert page["log"].split("\n") == lines[:149:-1]
