"""Unit tests for the component-side ``ha_mcp_tools/trace_context`` command.

The command reads Core's live trace store in process, matches a run's own
context or a triggering parent context, and returns Core's extended trace dict.
Every call goes through ``component_ws``, the WebSocket client of a real
``hass`` with the ``ha_mcp_tools/*`` commands registered, so the registration,
the request schema, the admin gate, the restore step and the encode are the
ones a caller reaches. The runs come from ``_trace_fixtures``, which builds
Core's own trace objects and stores them through ``async_store_trace``; a
frozen clock orders their starts because Core stamps ``timestamp.start`` in the
constructor.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.components.automation.trace import AutomationTrace
from homeassistant.components.script.trace import ScriptTrace
from homeassistant.components.trace.const import (
    DATA_TRACE,
    DATA_TRACE_STORE,
    DATA_TRACES_RESTORED,
)
from homeassistant.components.websocket_api.const import (
    ERR_INVALID_FORMAT,
    ERR_UNAUTHORIZED,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import MockHAClientWebSocket, WebSocketGenerator

from custom_components.ha_mcp_tools.websocket_api import trace_context

from ._component_core import ws_call, ws_result
from ._trace_fixtures import automation_run, script_run, store_runs

_COMMAND = "ha_mcp_tools/trace_context"
_FROZEN_START = "2026-11-08T16:00:00+00:00"

type Trace = AutomationTrace | ScriptTrace


@pytest.fixture
async def trace_store(hass: HomeAssistant) -> None:
    """Set up Core's trace integration, whose buckets the command reads."""
    assert await async_setup_component(hass, "trace", {})


async def test_command_is_advertised_as_a_capability(
    component_ws: MockHAClientWebSocket,
) -> None:
    """The server sends this lookup only to a component that advertises it."""
    info = await ws_result(component_ws, "ha_mcp_tools/info")

    assert "trace_context" in info["capabilities"]


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({"context_id": "ctx-1", "domain": "scene"}, id="unknown-domain"),
        pytest.param({"domain": "automation"}, id="missing-context-id"),
    ],
)
async def test_a_malformed_request_is_refused(
    component_ws: MockHAClientWebSocket, message: dict[str, str]
) -> None:
    """Core's schema refuses both the wrong domain and a missing context id."""
    reply = await ws_call(component_ws, _COMMAND, **message)

    assert reply["success"] is False
    assert reply["error"]["code"] == ERR_INVALID_FORMAT


@pytest.mark.usefixtures("trace_store")
async def test_a_non_admin_caller_is_refused(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    component_ws_read_only: MockHAClientWebSocket,
) -> None:
    """The lookup is admin-only; the same message answers for an admin."""
    await store_runs(hass, automation_run("r1", context_id="ctx-1"))

    refused = await ws_call(component_ws_read_only, _COMMAND, context_id="ctx-1")

    assert refused["success"] is False
    assert refused["error"]["code"] == ERR_UNAUTHORIZED
    admin = await ws_result(component_ws, _COMMAND, context_id="ctx-1")
    assert admin["run_id"] == "r1"


@pytest.mark.usefixtures("trace_store")
async def test_own_context_resolves_to_the_run(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A run's own context is a ``trace`` match; a lone match lists no others."""
    await store_runs(hass, automation_run("r1", context_id="ctx-1"))

    result = await ws_result(component_ws, _COMMAND, context_id="ctx-1")

    assert result["match"] == "trace"
    assert (result["domain"], result["item_id"], result["run_id"]) == (
        "automation",
        "uid_1",
        "r1",
    )
    assert result["trace"]["run_id"] == "r1"
    assert result["scanned"] == 1
    assert "other_matches" not in result


@pytest.mark.usefixtures("trace_store")
async def test_real_run_beats_a_newer_not_triggered_diagnostic(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A trigger that did not fire never displaces the run that did."""
    with freeze_time(_FROZEN_START) as clock:
        older = automation_run("older", context_id="own-older", parent_id="parent-1")
        clock.tick(timedelta(minutes=1))
        newer = automation_run("newer", context_id="own-newer", parent_id="parent-1")
        clock.tick(timedelta(minutes=1))
        diagnostic = automation_run(
            "diag",
            item_id="uid_2",
            context_id="own-diag",
            parent_id="parent-1",
            not_triggered=True,
        )
    await store_runs(hass, older, newer, diagnostic)

    result = await ws_result(component_ws, _COMMAND, context_id="parent-1")

    assert result["match"] == "parent"
    assert result["run_id"] == "older"
    assert result["scanned"] == 3
    assert result["other_matches"] == [
        {
            "domain": "automation",
            "item_id": "uid_1",
            "run_id": "newer",
            "match": "parent",
        },
        {
            "domain": "automation",
            "item_id": "uid_2",
            "run_id": "diag",
            "match": "parent",
        },
    ]


@pytest.mark.usefixtures("trace_store")
async def test_an_earlier_not_triggered_diagnostic_does_not_win(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A diagnostic that started first still loses to the run that fired."""
    with freeze_time(_FROZEN_START) as clock:
        diagnostic = automation_run(
            "diag",
            item_id="uid_2",
            context_id="own-diag",
            parent_id="parent-1",
            not_triggered=True,
        )
        clock.tick(timedelta(minutes=1))
        run = automation_run("run", context_id="own-run", parent_id="parent-1")
    await store_runs(hass, diagnostic, run)

    result = await ws_result(component_ws, _COMMAND, context_id="parent-1")

    assert result["match"] == "parent"
    assert result["run_id"] == "run"
    assert result["scanned"] == 2


@pytest.mark.parametrize(
    ("selector", "expected_item_id"),
    [
        pytest.param({"domain": "script"}, "uid_2", id="domain"),
        pytest.param({"item_id": "uid_a"}, "uid_a", id="item-id"),
    ],
)
@pytest.mark.usefixtures("trace_store")
async def test_each_selector_narrows_the_scan(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    selector: dict[str, str],
    expected_item_id: str,
) -> None:
    """Either selector alone leaves only the buckets it names in the scan."""
    await store_runs(
        hass,
        automation_run(
            "auto-a", item_id="uid_a", context_id="own-auto-a", parent_id="parent-1"
        ),
        automation_run(
            "auto-b", item_id="uid_b", context_id="own-auto-b", parent_id="parent-1"
        ),
        script_run("script", context_id="own-script", parent_id="parent-1"),
    )

    result = await ws_result(component_ws, _COMMAND, context_id="parent-1", **selector)

    # Every run above has a distinct run id, so the winner names the bucket the
    # selector kept; the scan count proves the others were left out.
    assert result["item_id"] == expected_item_id
    assert result["scanned"] == 1


@pytest.mark.usefixtures("trace_store")
async def test_a_missed_context_reports_the_scan_and_no_match(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A context no stored run carries is a miss that still counts the scan."""
    await store_runs(
        hass,
        automation_run("r1", context_id="own-1", parent_id="parent-x"),
    )

    result = await ws_result(component_ws, _COMMAND, context_id="missing")

    assert result["match"] is None
    assert result["trace"] is None
    assert result["scanned"] == 1


async def test_command_reports_unavailable_without_the_trace_store(
    component_ws: MockHAClientWebSocket,
) -> None:
    """A Home Assistant with tracing off answers that it is unavailable."""
    result = await ws_result(component_ws, _COMMAND, context_id="ctx-1")

    assert result["available"] is False
    assert result["match"] is None
    assert result["scanned"] == 0


@pytest.mark.usefixtures("trace_store")
async def test_extended_trace_keeps_cores_json_types(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A timedelta in a step's variables survives, as Core's trace view sends it."""
    await store_runs(
        hass,
        automation_run(
            "r1",
            context_id="ctx-1",
            changed_variables={"trigger": {"for": timedelta(minutes=5)}},
        ),
    )

    result = await ws_result(component_ws, _COMMAND, context_id="ctx-1")

    step = result["trace"]["trace"]["action/0"][0]
    assert step["changed_variables"]["trigger"]["for"]["total_seconds"] == 300.0


@pytest.mark.usefixtures("trace_store")
async def test_saved_traces_are_restored_before_the_match(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A restart leaves traces on disk only; the command restores them first."""
    saved = automation_run("r1", context_id="ctx-1")
    await store_runs(hass, saved)
    store = hass.data[DATA_TRACE_STORE]
    await store.async_save(
        {saved.key: list(hass.data[DATA_TRACE][saved.key].all_traces())}
    )
    # Core's setup after a restart leaves the in-memory buckets empty and the
    # restore marker unset, so the saved trace must come back from the store.
    hass.data[DATA_TRACE] = {}
    hass.data.pop(DATA_TRACES_RESTORED, None)

    result = await ws_result(component_ws, _COMMAND, context_id="ctx-1")

    assert result["match"] == "trace"
    assert result["run_id"] == "r1"


@pytest.mark.usefixtures("trace_store")
async def test_the_command_replies_with_the_encoded_result(
    component_ws: MockHAClientWebSocket,
) -> None:
    """The registered command sends Core's result message; the client decodes it."""
    result = await ws_result(component_ws, _COMMAND, context_id="ctx-1")

    assert result == {
        "available": True,
        "match": None,
        "run_id": None,
        "domain": None,
        "item_id": None,
        "trace": None,
        "scanned": 0,
    }


@pytest.mark.usefixtures("trace_store")
async def test_a_first_request_waits_for_a_restore_in_flight(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second first request waits; Core marks traces restored before loading.

    Core sets ``DATA_TRACES_RESTORED`` before it awaits the store load, so a
    request that only checked the marker would scan the empty store the first
    request is still filling. The lock makes it wait for that restore instead.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_restore(inner_hass: HomeAssistant) -> None:
        """Core's restore: mark traces restored before awaiting the load.

        The marker is set first, so a later call returns without loading even
        while the first load is still running.
        """
        if DATA_TRACES_RESTORED in inner_hass.data:
            return
        inner_hass.data[DATA_TRACES_RESTORED] = True
        started.set()
        await release.wait()

    monkeypatch.setattr(trace_context, "async_restore_traces", slow_restore)
    # The commands are registered on hass, so a second connection reaches the
    # same handlers. The trace setup left the store empty and the marker unset,
    # like a restart, so both requests are first requests.
    second_ws = await hass_ws_client(hass)

    first = asyncio.create_task(ws_result(component_ws, _COMMAND, context_id="ctx-1"))
    await started.wait()
    second = asyncio.create_task(ws_result(second_ws, _COMMAND, context_id="ctx-1"))
    # The second request has every chance to finish; a restore is still in
    # flight, so it must not return before that restore does. Without the lock
    # it returns as soon as it sees the marker, well inside this window.
    try:
        done, _pending = await asyncio.wait({second}, timeout=0.3)
        assert done == set()
    finally:
        # Release and drain both requests even when the assertion fails, so a
        # lock regression reports the assertion, not a dangling task.
        release.set()
        first_result, second_result = await asyncio.gather(first, second)

    assert first_result["match"] is None
    assert second_result["match"] is None


@pytest.mark.usefixtures("trace_store")
async def test_the_own_context_wins_and_the_rest_are_listed(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The run that owns the context wins; a newer parent run does not displace it."""
    shared = "shared-context"
    with freeze_time(_FROZEN_START) as clock:
        own = automation_run("own-match", item_id="uid_a", context_id=shared)
        clock.tick(timedelta(minutes=1))
        older_parent = automation_run(
            "older-parent",
            item_id="uid_b",
            context_id="own-older-parent",
            parent_id=shared,
        )
        clock.tick(timedelta(minutes=1))
        newest = script_run(
            "newest-script",
            item_id="uid_c",
            context_id="own-newest-script",
            parent_id=shared,
        )
    await store_runs(hass, own, older_parent, newest)

    result = await ws_result(component_ws, _COMMAND, context_id=shared)

    assert result["match"] == "trace"
    assert (result["domain"], result["item_id"], result["run_id"]) == (
        "automation",
        "uid_a",
        "own-match",
    )
    assert result["scanned"] == 3
    assert result["other_matches"] == [
        {
            "domain": "automation",
            "item_id": "uid_b",
            "run_id": "older-parent",
            "match": "parent",
        },
        {
            "domain": "script",
            "item_id": "uid_c",
            "run_id": "newest-script",
            "match": "parent",
        },
    ]


@pytest.mark.parametrize(
    ("runs", "expected_run_id"),
    [
        pytest.param(
            [
                ("script", "own-script", "uid_a", False),
                ("automation", "parent-auto", "uid_b", True),
            ],
            "own-script",
            id="own-context-before-parent",
        ),
        pytest.param(
            [
                ("script", "own-script", "uid_a", False),
                ("automation", "own-auto", "uid_b", False),
            ],
            "own-auto",
            id="automation-before-script",
        ),
        pytest.param(
            [
                ("automation", "own-auto-b", "uid_b", False),
                ("automation", "own-auto-a", "uid_a", False),
            ],
            "own-auto-a",
            id="item-id-ascending",
        ),
    ],
)
@pytest.mark.usefixtures("trace_store")
async def test_ties_break_own_then_automation_then_item_id(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    runs: list[tuple[str, str, str, bool]],
    expected_run_id: str,
) -> None:
    """Same-start matches order own over parent, automation over script, id asc."""
    shared = "shared-context"
    with freeze_time(_FROZEN_START):
        for domain, run_id, item_id, is_parent in runs:
            context_id = "own-" + run_id if is_parent else shared
            parent_id = shared if is_parent else None
            trace: Trace
            if domain == "automation":
                trace = automation_run(
                    run_id,
                    item_id=item_id,
                    context_id=context_id,
                    parent_id=parent_id,
                )
            else:
                trace = script_run(
                    run_id,
                    item_id=item_id,
                    context_id=context_id,
                    parent_id=parent_id,
                )
            await store_runs(hass, trace)

    result = await ws_result(component_ws, _COMMAND, context_id=shared)

    assert result["run_id"] == expected_run_id


@pytest.mark.usefixtures("trace_store")
async def test_a_tie_inside_one_run_bucket_still_selects_one_run(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Two runs of one item that start together differ only by run id."""
    shared = "shared-context"
    with freeze_time(_FROZEN_START):
        await store_runs(
            hass,
            automation_run(
                "run-b", item_id="uid_a", context_id="own-b", parent_id=shared
            ),
        )
        await store_runs(
            hass,
            automation_run(
                "run-a", item_id="uid_a", context_id="own-a", parent_id=shared
            ),
        )

    result = await ws_result(component_ws, _COMMAND, context_id=shared)

    assert result["run_id"] == "run-a"
    assert result["other_matches"] == [
        {
            "domain": "automation",
            "item_id": "uid_a",
            "run_id": "run-b",
            "match": "parent",
        }
    ]
