"""The 2026.12 bridge reads a since-start app log through Core's own client."""

from unittest.mock import AsyncMock, patch

import aiohasupervisor.models
import pytest
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient

from ._error_log_core import call_logs
from ._tool_errors import error_body
from ._tools_addons_core import Supervisor
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app


@pytest.mark.skipif(
    not hasattr(aiohasupervisor.models, "LogsOptions"),
    reason="Core 2026.11's Supervisor client does not implement native journal reads",
)
async def test_latest_app_uses_core_supervisor_client(
    component_core_client: HomeAssistantClient, core_supervisor: Supervisor
) -> None:
    path = "/addons/core_ssh/logs/latest"
    core_supervisor.reply("GET", path, text="startup banner\nSSH started\n")
    with patch.object(
        component_core_client,
        "guarded_request",
        AsyncMock(wraps=component_core_client.guarded_request),
    ) as rest:
        result = await call_logs(
            component_core_client,
            source="supervisor",
            slug="core_ssh",
            latest=True,
            order="oldest",
            limit=2,
        )
    assert result["log"].splitlines() == ["startup banner", "SSH started"]
    assert not result.get("warnings")
    assert core_supervisor.requests == [("GET", path)]
    assert not any("/hassio/" in call.args[1] for call in rest.await_args_list)


@pytest.mark.skipif(
    not hasattr(aiohasupervisor.models, "LogsOptions"),
    reason="Core 2026.11's Supervisor client does not implement native journal reads",
)
async def test_large_journal_search_retries_a_smaller_native_range(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    core_supervisor: Supervisor,
) -> None:
    hass.data.pop(KEY_DATA_LOGGING, None)
    path = "/core/logs"
    recent = "2026-11-27 18:00:00.000+00:00 WARNING (MainThread) [logger] done"
    core_supervisor.reply("GET", path, text=("x" * 1024 + "\n") * 1000 + recent + "\n")
    core_supervisor.reply("GET", path, text=recent + "\n")
    with patch.object(
        component_core_client,
        "get_error_log",
        AsyncMock(wraps=component_core_client.get_error_log),
    ) as fallback:
        result = await call_logs(
            component_core_client, source="error_log", search="done", limit=1
        )
    fallback.assert_not_awaited()
    assert not any("Marketplace" in warning for warning in result["warnings"])
    assert result["log"] == recent
    assert core_supervisor.requests == [("GET", path), ("GET", path)]
    assert any("byte limit" in warning for warning in result["warnings"])


@pytest.mark.skipif(
    not hasattr(aiohasupervisor.models, "LogsOptions"),
    reason="Core 2026.11's Supervisor client does not implement native journal reads",
)
async def test_a_searched_window_over_the_byte_limit_is_read_in_pieces(
    component_core_client: HomeAssistantClient, core_supervisor: Supervisor
) -> None:
    """The second half is read from the first half's cursor."""
    path = "/host/logs"
    core_supervisor.reply("GET", path, text=("x" * 1024 + "\n") * 1100)
    core_supervisor.reply(
        "GET", path, text="kernel: first done\n", headers={"X-First-Cursor": "c1"}
    )
    core_supervisor.reply("GET", path, text="kernel: second done\n")
    result = await call_logs(
        component_core_client,
        source="system_service",
        slug="host",
        hours_back=1,
        search="done",
        order="oldest",
    )
    assert result["log"] == "kernel: first done\nkernel: second done"
    assert not result.get("warnings")
    assert core_supervisor.headers("GET", path)["Range"].startswith("entries=c1:")


@pytest.mark.skipif(
    not hasattr(aiohasupervisor.models, "LogsOptions"),
    reason="Core 2026.11's Supervisor client does not implement native journal reads",
)
async def test_a_one_line_tail_is_the_newest_line(
    component_core_client: HomeAssistantClient, core_supervisor: Supervisor
) -> None:
    """A one-entry range reads from the journal's head, so two are asked for."""
    path = "/host/logs"
    core_supervisor.reply("GET", path, text="older\nnewest\n")
    result = await call_logs(
        component_core_client, source="system_service", slug="host", limit=1
    )
    assert result["log"] == "newest"
    assert core_supervisor.headers("GET", path)["Range"] == "entries=:-1:2"


@pytest.mark.skipif(
    not hasattr(aiohasupervisor.models, "LogsOptions"),
    reason="Core 2026.11's Supervisor client does not implement native journal reads",
)
async def test_a_failure_supervisor_gives_no_reason_for_points_at_its_log(
    component_core_client: HomeAssistantClient, core_supervisor: Supervisor
) -> None:
    """Supervisor answers a log route's failure in plain text, which its client
    drops: an app that never started has no latest start to read."""
    core_supervisor.reply(
        "GET", "/addons/core_mosquitto/logs/latest", status=500, text="no epoch"
    )
    with pytest.raises(ToolError) as refused:
        await call_logs(
            component_core_client,
            source="supervisor",
            slug="core_mosquitto",
            latest=True,
        )
    message = error_body(refused)["error"]["message"]
    assert "slug supervisor" in message
    assert "SupervisorError" not in message


@pytest.mark.skipif(
    not hasattr(aiohasupervisor.models, "LogsOptions"),
    reason="Core 2026.11's Supervisor client does not implement native journal reads",
)
async def test_a_timeline_names_the_source_that_failed_and_keeps_the_rest(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    core_supervisor: Supervisor,
) -> None:
    hass.data.pop(KEY_DATA_LOGGING, None)
    core_supervisor.reply(
        "GET", "/core/logs", text="2026-11-27 18:00:00.000+00:00 WARNING done\n"
    )
    result = await call_logs(
        component_core_client,
        sources=["core", "app:not_installed"],
        start_time="2026-11-27T17:59:00+00:00",
        end_time="2026-11-27T18:05:00+00:00",
    )
    assert [row["source"] for row in result["records"]] == ["core"]
    assert list(result["errors"]) == ["app:not_installed"]
