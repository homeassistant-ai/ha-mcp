"""Progress that long-running tools report through the FastMCP ``Context``.

Each tool runs on the server's own client against a real ``hass`` and is
handed a context that records what it reports. The tool must report its
progress at each boundary and must not send MCP log messages: protocol
logging is deprecated as of MCP 2026-07-28 (SEP-2577) and each call emits
``MCPDeprecationWarning`` (#2464). A client that hangs up on a progress
notification must not turn a finished tool call into an error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp._vendor.fastmcp import Context, FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.backup import register_backup_tools
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.tools_hacs import HacsTools
from ha_mcp.tools.tools_history import HistoryTools
from ha_mcp.tools.tools_traces import TraceTools

from ._deep_search_core import search_tools
from ._hacs_marketplace import CARD_ID, MAIN, MAIN_ID, set_up_marketplace, stored
from ._operations import operations_without_listener  # noqa: F401
from ._switched_states import SwitchedStates
from ._trace_fixtures import automation_run, store_runs

Step = tuple[float, float, str]


class ProgressLog:
    """A FastMCP context that records what a tool reports through it.

    With ``failure`` every progress report raises it, as a client that hung up.
    """

    def __init__(self, failure: Exception | None = None) -> None:
        self.reports: list[tuple[float, float | None, str | None]] = []
        self.logged: list[tuple[Any, ...]] = []
        self._failure = failure

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.reports.append((progress, total, message))
        if self._failure is not None:
            raise self._failure

    async def _log(self, *args: Any, **kwargs: Any) -> None:
        self.logged.append(args)

    debug = info = warning = error = log = _log

    @property
    def context(self) -> Context:
        return cast(Context, self)


def assert_reported(log: ProgressLog, expected: list[Step]) -> None:
    """``log`` holds exactly ``expected``'s progress/total pairs, each message
    containing the expected text, and no MCP log message."""
    assert [(p, t) for p, t, _ in log.reports] == [(p, t) for p, t, _ in expected]
    for (_, _, message), (_, _, part) in zip(log.reports, expected, strict=True):
        assert part in (message or ""), log.reports
    assert log.logged == []


# --- deep_search (the engine behind ha_search) -------------------------------


@pytest.mark.parametrize(
    ("search_type", "phase_message"),
    [
        ("automation", "automations searched"),
        ("script", "scripts searched"),
        ("scene", "scenes searched"),
        ("dashboard", "dashboards searched"),
        ("helper", "helpers searched"),
    ],
)
async def test_deep_search_reports_each_search_phase(
    core_client: HomeAssistantClient, search_type: str, phase_message: str
) -> None:
    """A search reports the state fetch and then its one search phase."""
    log = ProgressLog()

    result = await search_tools(core_client).deep_search(
        "anything", search_types=[search_type], limit=5, ctx=log.context
    )

    assert result["success"] is True
    assert_reported(
        log,
        [
            (0, 2, "fetching entity states"),
            (1, 2, "entity states"),
            (2, 2, phase_message),
        ],
    )


async def test_a_client_that_hangs_up_on_progress_still_gets_the_search(
    core_client: HomeAssistantClient,
) -> None:
    """A finished search is never turned into an error by a failed progress
    notification."""
    log = ProgressLog(ConnectionError("transport gone"))

    result = await search_tools(core_client).deep_search(
        "anything", search_types=["helper"], limit=5, ctx=log.context
    )

    assert result["success"] is True
    assert result["query"] == "anything"
    assert len(log.reports) == 3


# --- ha_get_history -----------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "extra"),
    [("history", {}), ("statistics", {"period": "day"})],
)
async def test_history_reports_connect_query_and_completion(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    source: str,
    extra: dict[str, Any],
) -> None:
    assert await async_setup_component(hass, "history", {})
    start = datetime(2024, 1, 1, tzinfo=UTC)
    log = ProgressLog()

    result = await HistoryTools(core_client).ha_get_history(
        entity_ids="sensor.test",
        source=source,
        start_time=start.isoformat(),
        end_time=(start + timedelta(days=1)).isoformat(),
        ctx=log.context,
        **extra,
    )

    assert result["data"]["success"] is True
    assert_reported(
        log,
        [
            (0, 3, "connecting"),
            (1, 3, f"querying recorder ({source})"),
            (3, 3, "recorder query complete"),
        ],
    )


# --- ha_get_automation_traces -------------------------------------------------


@pytest.mark.parametrize(
    ("runs", "selectors", "expected"),
    [
        pytest.param(
            1,
            {},
            [
                (0, 3, "resolving trace target"),
                (1, 3, "fetching trace list"),
                (3, 3, "listed 1 traces"),
            ],
            id="list",
        ),
        pytest.param(
            1,
            {"run_id": "r1"},
            [
                (0, 3, "resolving trace target"),
                (1, 3, "fetching trace detail"),
                (3, 3, "formatting trace"),
            ],
            id="detail",
        ),
        pytest.param(
            0,
            {},
            [
                (0, 3, "resolving trace target"),
                (1, 3, "fetching trace list"),
                (2, 3, "no traces; gathering diagnostics"),
                (3, 3, "diagnostics complete"),
            ],
            id="no-runs-diagnostics",
        ),
    ],
)
async def test_traces_report_each_step_of_their_branch(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    runs: int,
    selectors: dict[str, Any],
    expected: list[Step],
) -> None:
    assert await async_setup_component(hass, "trace", {})
    if runs:
        await store_runs(hass, automation_run("r1", context_id="c1", item_id="demo"))
    log = ProgressLog()

    result = await TraceTools(core_client).ha_get_automation_traces(
        entity_id="automation.demo", ctx=log.context, **selectors
    )

    assert result["success"] is True
    assert_reported(log, expected)


# --- ha_get_hacs_info ---------------------------------------------------------


async def test_hacs_search_reports_check_fetch_filter_and_match(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    entry = await set_up_marketplace(
        hass,
        aioclient_mock,
        hass_storage,
        {
            CARD_ID: stored("piitaya/lovelace-mushroom", "plugin"),
            MAIN_ID: stored(MAIN, "integration"),
        },
    )
    log = ProgressLog()
    try:
        result = await HacsTools(core_client).ha_get_hacs_info(
            action="search", query="mushroom", ctx=log.context
        )
    finally:
        await hass.config_entries.async_unload(entry.entry_id)

    matched = result["data"]["total_matches"]
    assert matched >= 1
    assert_reported(
        log,
        [
            (0, 3, "checking HACS availability"),
            (1, 3, "fetching HACS repository list"),
            (2, 3, "filtering"),
            (3, 3, f"matched {matched} repositories"),
        ],
    )


# --- bulk_device_control ------------------------------------------------------

TWO_OPS = [
    {"entity_id": "light.a", "action": "on"},
    {"entity_id": "light.b", "action": "off"},
]


@pytest.fixture
def devices(hass: HomeAssistant) -> SwitchedStates:
    hass.states.async_set("light.a", "off")
    hass.states.async_set("light.b", "on")
    return SwitchedStates(hass)


@pytest.mark.usefixtures("devices")
async def test_sequential_bulk_reports_each_dispatched_op(
    core_client: HomeAssistantClient,
) -> None:
    log = ProgressLog()

    result = await DeviceControlTools(core_client).bulk_device_control(
        operations=[dict(op) for op in TWO_OPS], parallel=False, ctx=log.context
    )

    assert result["successful_commands"] == 2
    assert_reported(
        log,
        [
            (0, 2, "dispatching operations"),
            (1, 2, "light.a on dispatched"),
            (2, 2, "light.b off dispatched"),
            (2, 2, "dispatched 2 op(s)"),
        ],
    )


@pytest.mark.usefixtures("devices")
async def test_parallel_bulk_reports_only_the_dispatch_and_its_end(
    core_client: HomeAssistantClient,
) -> None:
    log = ProgressLog()

    result = await DeviceControlTools(core_client).bulk_device_control(
        operations=[dict(op) for op in TWO_OPS], parallel=True, ctx=log.context
    )

    assert result["successful_commands"] == 2
    assert_reported(
        log,
        [(0, 2, "dispatching operations"), (2, 2, "dispatched 2 op(s)")],
    )


async def test_a_component_batch_with_an_unconfirmed_op_is_not_announced_confirmed(
    component_core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    """Progress counts only the dispatched ops and leaves each op's outcome to
    its result."""
    devices.behaviour["light.b"] = "stay"
    log = ProgressLog()

    result = await DeviceControlTools(component_core_client).bulk_device_control(
        operations=[
            *({**op, "timeout_seconds": 0} for op in TWO_OPS),
            {"action": "on"},
        ],
        ctx=log.context,
    )

    assert [op["confirmed"] for op in result["results"][:2]] == [True, False]
    assert_reported(
        log,
        [(2, 2, "dispatched via component; each op's result shows its outcome")],
    )
    (_, _, message) = log.reports[0]
    assert "confirmed" not in (message or "")


@pytest.mark.usefixtures("devices")
async def test_a_client_that_hangs_up_on_progress_still_gets_every_op_dispatched(
    core_client: HomeAssistantClient,
) -> None:
    """Each report is still attempted, and the dispatch result still returned."""
    log = ProgressLog(ConnectionError("transport gone"))

    result = await DeviceControlTools(core_client).bulk_device_control(
        operations=[dict(TWO_OPS[0])], parallel=False, ctx=log.context
    )

    assert result["successful_commands"] == 1
    assert len(result["operation_ids"]) == 1
    assert len(log.reports) == 3
    assert log.logged == []


# --- ha_manage_backup ---------------------------------------------------------


async def test_ha_manage_backup_takes_its_context_from_fastmcp_not_the_caller(
    core_client: HomeAssistantClient,
) -> None:
    """``ha_manage_backup`` is a closure, not a class method like the tools
    above. If FastMCP stopped claiming its ``ctx`` for injection, ``ctx`` would
    show in the tool's schema and stay ``None`` for a real client, so every
    backup progress heartbeat would silently never fire."""
    mcp = FastMCP("test")
    register_backup_tools(mcp, core_client)
    tool = await mcp.get_tool("ha_manage_backup")
    props = tool.parameters.get("properties", {})

    assert "ctx" not in props
    assert {"scope", "action", "confirm", "backup_id"} <= props.keys()
